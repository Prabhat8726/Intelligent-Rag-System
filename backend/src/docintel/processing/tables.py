"""Table detection on word geometry (native and OCR pages alike), plus multi-page stitching.

Approach (deterministic, no ML):
1. A header is a line with >= 3 column segments of short, digit-free labels.
2. Following lines belong to the table while they stay close (row gap) and start inside it.
3. Column boundaries lie between adjacent header labels, at the position that cuts the fewest
   words and leaves the most rows with both neighbouring cells filled. This handles the usual
   mix of left-aligned headers with right-aligned numbers.
4. A line with one filled cell directly below a row continues that row (wrapped text).
5. Tables on consecutive pages with the same header are one table (repeated header rows).
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING

from docintel.processing.content import BBox, DocumentTable, PageTable, TableRow, Word
from docintel.processing.inspection import PageMethod

if TYPE_CHECKING:
    from docintel.processing.content import PageContent

MIN_COLUMNS = 3
MAX_HEADER_WORDS = 4  # per header cell
# Max centre-to-centre distance between consecutive rows / for a wrapped-cell continuation
# line, x text size (pitch is comparable for native and OCR boxes; edge gaps are not).
ROW_PITCH = 2.6
CONTINUATION_PITCH = 1.6
# Once rows exist, a line further below than this multiple of the table's median row pitch is
# no longer part of it (the gap before totals or a footer is larger than the row rhythm).
ROW_PITCH_JUMP = 1.6
# Rows may start this far (x text size) left of the first header label: OCR often loses a
# narrow leading header such as "#" while the row numbers below it survive.
LEFT_OVERHANG = 6.0
# Consecutive-page tables continue each other when this share of header cells agree.
STITCH_HEADER_AGREEMENT = 0.75
_DIGIT = re.compile(r"\d")
_LETTER = re.compile(r"[^\W\d_]")


@dataclass(slots=True)
class _Row:
    cells: list[list[str]]
    box: BBox
    last_line: int


def _segment_words(page: PageContent, line_index: int, segment_index: int) -> list[Word]:
    return [page.words[i] for i in page.lines[line_index].segments[segment_index]]


def _line_words(page: PageContent, line_index: int) -> list[Word]:
    return [page.words[i] for i in page.lines[line_index].word_indices]


def _line_size(words: list[Word]) -> float:
    return float(median(word.size for word in words)) if words else 0.0


def _is_header(page: PageContent, line_index: int) -> bool:
    segments = page.lines[line_index].segments
    if len(segments) < MIN_COLUMNS:
        return False
    with_letters = 0
    for segment_index in range(len(segments)):
        words = _segment_words(page, line_index, segment_index)
        text = " ".join(word.text for word in words)
        if len(words) > MAX_HEADER_WORDS or _DIGIT.search(text):
            return False
        if _LETTER.search(text):
            with_letters += 1
    return with_letters * 2 >= len(segments)


def _column_bounds(header: list[BBox], body: list[list[Word]]) -> list[float]:
    """For each pair of adjacent header cells, the x position that best separates the columns.

    Candidates lie between the left header's start and the right header's start. The winner
    cuts the fewest words and, among those, lies furthest right: cells normally start where
    their header starts, so the boundary hugs the right header unless data reaches past it.
    """
    bounds: list[float] = []
    for left, right in itertools.pairwise(header):
        window = [w for line in body for w in line if w.bbox.x1 > left.x0 and w.bbox.x0 < right.x1]
        candidates = {(left.x1 + right.x0) / 2, right.x0 - 0.01}
        edges = sorted({edge for w in window for edge in (w.bbox.x0, w.bbox.x1)})
        for a, b in itertools.pairwise(edges):
            middle = (a + b) / 2
            if left.x1 <= middle < right.x0:
                candidates.add(middle)

        def score(x: float, window: list[Word] = window) -> tuple[int, float]:
            crossings = sum(1 for w in window if w.bbox.x0 < x < w.bbox.x1)
            return (crossings, -x)

        bounds.append(min(candidates, key=score))
    return bounds


def _assign(words: list[Word], bounds: list[float]) -> list[list[str]]:
    cells: list[list[str]] = [[] for _ in range(len(bounds) + 1)]
    for word in sorted(words, key=lambda w: w.bbox.x0):
        column = sum(1 for bound in bounds if word.bbox.center_x >= bound)
        cells[column].append(word.text)
    return cells


def _detect_one(
    page: PageContent, header_line: int, used: set[int]
) -> tuple[PageTable, set[int]] | None:
    header_boxes = [
        BBox.enclosing([w.bbox for w in _segment_words(page, header_line, s)])
        for s in range(len(page.lines[header_line].segments))
    ]
    header_words = _line_words(page, header_line)
    size = _line_size(header_words)
    left_edge = header_boxes[0].x0 - LEFT_OVERHANG * size

    # Candidate body: lines below the header that stay close and start inside the table.
    body_lines: list[int] = []
    previous_center = page.lines[header_line].bbox.center_y
    for index in range(header_line + 1, len(page.lines)):
        line = page.lines[index]
        if index in used or line.bbox.center_y - previous_center > ROW_PITCH * size:
            break
        if line.bbox.x0 < left_edge or _is_header(page, index):
            break
        body_lines.append(index)
        previous_center = line.bbox.center_y
    if not body_lines:
        return None

    # Most rows carry a segment left of the first header label: its header was lost (e.g. an
    # unreadable "#"). Add an unlabelled column for it instead of merging it into the next.
    first_segments = [
        BBox.enclosing([w.bbox for w in _segment_words(page, index, 0)]) for index in body_lines
    ]
    overhang = [box for box in first_segments if box.x1 < header_boxes[0].x0 - 0.25 * size]
    if len(overhang) * 2 > len(body_lines):
        header_boxes = [BBox.enclosing(overhang), *header_boxes]

    bounds = _column_bounds(header_boxes, [_line_words(page, i) for i in body_lines])
    header = [" ".join(cell) for cell in _assign(header_words, bounds)]

    rows: list[_Row] = []
    pitches: list[float] = []
    table_lines = {header_line}
    previous_center = page.lines[header_line].bbox.center_y
    for index in body_lines:
        words = _line_words(page, index)
        cells = _assign(words, bounds)
        filled = [column for column, cell in enumerate(cells) if cell]
        box = page.lines[index].bbox
        pitch = box.center_y - previous_center
        if pitches and pitch > ROW_PITCH_JUMP * median(pitches):
            break
        if len(filled) >= 2 and filled[0] <= 1:
            rows.append(_Row(cells, box, index))
            pitches.append(pitch)
        elif len(filled) == 1 and rows and pitch <= CONTINUATION_PITCH * size:
            row = rows[-1]
            row.cells[filled[0]].extend(cells[filled[0]])
            row.box = row.box.union(box)
            row.last_line = index
        else:
            break
        table_lines.add(index)
        previous_center = box.center_y
    if not rows:
        return None

    words_in_table = [w for i in table_lines for w in _line_words(page, i)]
    crossing = sum(1 for w in words_in_table if any(w.bbox.x0 < b < w.bbox.x1 for b in bounds))
    confidence = 1 - crossing / max(1, len(words_in_table))
    if page.method == PageMethod.OCR and page.ocr_confidence is not None:
        confidence *= page.ocr_confidence / 100
    bbox = page.lines[header_line].bbox
    for row in rows:
        bbox = bbox.union(row.box)
    table = PageTable(
        page_number=page.page_number,
        bbox=bbox,
        header=header,
        rows=[
            TableRow([" ".join(cell) for cell in row.cells], row.box, page.page_number)
            for row in rows
        ],
        column_bounds=bounds,
        confidence=max(0.0, confidence),
    )
    return table, table_lines


def detect_tables(page: PageContent) -> tuple[list[PageTable], set[int]]:
    tables: list[PageTable] = []
    used: set[int] = set()
    for index in range(len(page.lines)):
        if index in used or not _is_header(page, index):
            continue
        found = _detect_one(page, index, used)
        if found is not None:
            table, lines = found
            tables.append(table)
            used |= lines
    return tables, used


def _normalized_header(header: list[str]) -> list[str]:
    return [" ".join(cell.casefold().split()) for cell in header]


def header_offset(first: list[str], second: list[str]) -> int | None:
    """How many leading cells `second` lacks relative to `first` if the headers match, else None.

    Matching = most labels equal (OCR may garble a short label). One missing leading column is
    tolerated: OCR often merges an unreadable "#" column into the next one on a later page.
    """
    a, b = _normalized_header(first), _normalized_header(second)
    for offset in (0, 1):
        candidate = a[offset:]
        if offset and len(a[0]) > 1:  # only a short or empty leading label may go missing
            continue
        if len(candidate) != len(b) or not b:
            continue
        same = sum(x == y for x, y in zip(candidate, b, strict=True))
        if same >= STITCH_HEADER_AGREEMENT * len(b):
            return offset
    return None


def stitch_tables(pages: list[PageContent]) -> list[DocumentTable]:
    """Merge tables continued on the next page under the same header."""
    result: list[DocumentTable] = []
    methods: list[set[str]] = []
    for page in pages:
        for index, table in enumerate(page.tables):
            previous = result[-1] if result else None
            offset = (
                header_offset(previous.header, table.header)
                if previous is not None and index == 0 and previous.page_end == page.page_number - 1
                else None
            )
            if offset is not None and previous is not None:
                continued = [
                    TableRow([""] * offset + row.cells, row.bbox, row.page_number)
                    for row in table.rows
                ]
                rows = len(previous.rows) + len(table.rows)
                merged_confidence = (
                    previous.confidence * len(previous.rows) + table.confidence * len(table.rows)
                ) / rows
                result[-1] = DocumentTable(
                    page_start=previous.page_start,
                    page_end=page.page_number,
                    bbox=previous.bbox,
                    header=previous.header,
                    rows=[*previous.rows, *continued],
                    method=previous.method,
                    confidence=merged_confidence,
                )
                methods[-1].add(page.method.value)
            else:
                result.append(
                    DocumentTable(
                        page_start=page.page_number,
                        page_end=page.page_number,
                        bbox=table.bbox,
                        header=table.header,
                        rows=list(table.rows),
                        method=page.method.value,
                        confidence=table.confidence,
                    )
                )
                methods.append({page.method.value})
    return [
        DocumentTable(
            page_start=table.page_start,
            page_end=table.page_end,
            bbox=table.bbox,
            header=table.header,
            rows=table.rows,
            method=next(iter(used)) if len(used) == 1 else "MIXED",
            confidence=table.confidence,
        )
        for table, used in zip(result, methods, strict=True)
    ]
