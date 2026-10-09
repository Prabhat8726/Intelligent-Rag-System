"""Layout analysis on word boxes: lines, column segments, text blocks, reading order, page text.

Works identically on native and OCR words. Thresholds are expressed relative to the text size,
so they hold for any DPI or page unit. Measured on the synthetic corpus: spaces between words
are 0.2-0.5x the font size, gaps between table columns 0.9x or more (SEGMENT_GAP sits between).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from statistics import median

from docintel.processing.content import BBox, Block, BlockKind, Line, PageContent, Word
from docintel.processing.tables import detect_tables

SEGMENT_GAP = 0.75  # gap (x text size) that separates two columns on one line
LINE_OVERLAP = 0.5  # vertical overlap (share of the smaller height) to share a line
# Max distance between the centres of consecutive lines of one block (x text size). Measured
# centre-to-centre ("pitch") rather than edge-to-edge: OCR boxes are tighter than native boxes,
# but the pitch is the same. Body text ~1.2, key/value grids ~1.7, paragraph breaks > 2.5.
BLOCK_PITCH = 2.0
HEADING_SIZE_RATIO = 1.25  # heading = single line at least this much larger than body text
PAIR_GAP = 6.0  # max gap (x text size) between a label column and its value column
PAIR_LINE_OVERLAP = 0.6  # share of lines two side-by-side blocks must have in common
# A wide space inside a sentence (OCR word boxes are tight, so gaps look wider) can split a
# line into two segments. A segment this close (x text size) to a sentence of at least
# PROSE_WORDS words on its left continues that sentence instead of opening a column block,
# which would otherwise be read after the whole paragraph. Column gutters are wider.
CONTINUATION_GAP = 1.5
PROSE_WORDS = 4
_NUMERIC_CHARS = set("0123456789.,-+/%()$€£¥₹")


def _median(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


def build_lines(words: list[Word], skew: float = 0.0) -> list[Line]:
    """Group words into lines (top to bottom) and split each line into column segments.

    `skew` is the text slope (dy/dx) measured by OCR; vertical positions are de-skewed before
    grouping so that slightly rotated scans still produce whole lines.
    """
    if not words:
        return []

    def band(index: int) -> tuple[float, float]:
        box = words[index].bbox
        center = box.center_y - skew * box.center_x
        return center - box.height / 2, center + box.height / 2

    order = sorted(range(len(words)), key=lambda i: sum(band(i)) / 2)
    groups: list[list[int]] = []
    tops: list[float] = []
    bottoms: list[float] = []
    for index in order:
        top, bottom = band(index)
        if groups:
            line_top, line_bottom = _median(tops), _median(bottoms)
            overlap = min(bottom, line_bottom) - max(top, line_top)
            if overlap >= LINE_OVERLAP * min(bottom - top, line_bottom - line_top):
                groups[-1].append(index)
                tops.append(top)
                bottoms.append(bottom)
                continue
        groups.append([index])
        tops, bottoms = [top], [bottom]

    lines: list[Line] = []
    for group in groups:
        group.sort(key=lambda i: words[i].bbox.x0)
        size = _median([words[i].size for i in group])
        segments: list[list[int]] = [[group[0]]]
        for previous, current in itertools.pairwise(group):
            if words[current].bbox.x0 - words[previous].bbox.x1 > SEGMENT_GAP * size:
                segments.append([current])
            else:
                segments[-1].append(current)
        lines.append(Line(segments, BBox.enclosing([words[i].bbox for i in group])))
    return lines


@dataclass(slots=True)
class _OpenBlock:
    bbox: BBox
    size: float
    last_center: float
    segments: list[tuple[int, int]] = field(default_factory=list)


def _segment_box(page: PageContent, line_index: int, segment_index: int) -> tuple[BBox, float]:
    indices = page.lines[line_index].segments[segment_index]
    boxes = [page.words[i].bbox for i in indices]
    return BBox.enclosing(boxes), _median([page.words[i].size for i in indices])


def _continued(
    page: PageContent, blocks: list[_OpenBlock], line_index: int, segment_index: int, box: BBox
) -> _OpenBlock | None:
    """The block of the sentence this segment continues on the same line, if any."""
    if segment_index == 0:
        return None
    previous = page.lines[line_index].segments[segment_index - 1]
    if len(previous) < PROSE_WORDS:
        return None  # a label beside its value
    left, size = _segment_box(page, line_index, segment_index - 1)
    if box.x0 - left.x1 > CONTINUATION_GAP * size:
        return None
    return next((b for b in blocks if (line_index, segment_index - 1) in b.segments), None)


def _above(blocks: list[_OpenBlock], box: BBox, size: float) -> _OpenBlock | None:
    """The block this segment extends downwards: close below it, same size, same column."""
    for block in reversed(blocks):
        pitch = box.center_y - block.last_center
        if not 0 < pitch <= BLOCK_PITCH * size or box.y0 < block.bbox.y1 - size:
            continue
        if not 0.7 <= size / block.size <= 1.4:
            continue
        overlap = min(box.x1, block.bbox.x1) - max(box.x0, block.bbox.x0)
        aligned = abs(box.x0 - block.bbox.x0) <= size
        if aligned or overlap >= 0.3 * min(box.width, block.bbox.width):
            return block
    return None


def build_blocks(page: PageContent, table_lines: set[int]) -> list[Block]:
    """Group segments of non-table lines into blocks; add table blocks; sort in reading order."""
    blocks: list[_OpenBlock] = []
    for line_index, line in enumerate(page.lines):
        if line_index in table_lines:
            continue
        for segment_index in range(len(line.segments)):
            box, size = _segment_box(page, line_index, segment_index)
            target = _continued(page, blocks, line_index, segment_index, box) or _above(
                blocks, box, size
            )
            if target is None:
                blocks.append(_OpenBlock(box, size, box.center_y, [(line_index, segment_index)]))
            else:
                target.bbox = target.bbox.union(box)
                target.last_center = max(target.last_center, box.center_y)
                target.segments.append((line_index, segment_index))

    blocks = _merge_label_value_blocks(page, blocks)
    body_size = _median([word.size for word in page.words])
    result: list[Block] = []
    for block in blocks:
        single_line = len({line for line, _ in block.segments}) == 1
        heading = single_line and body_size > 0 and block.size >= HEADING_SIZE_RATIO * body_size
        kind = BlockKind.HEADING if heading else BlockKind.TEXT
        result.append(Block(kind, block.bbox, sorted(block.segments)))
    for table_index, table in enumerate(page.tables):
        result.append(Block(BlockKind.TABLE, table.bbox, [], table_index))
    tolerance = body_size / 2 if body_size else 1.0
    result.sort(key=lambda block: (round(block.bbox.y0 / tolerance), block.bbox.x0))
    return result


def _numeric(text: str) -> bool:
    characters = [ch for ch in text if not ch.isspace()]
    if not characters or not any(ch.isdigit() for ch in characters):
        return False
    return sum(1 for ch in characters if ch in _NUMERIC_CHARS) * 2 >= len(characters)


def _merge_label_value_blocks(page: PageContent, blocks: list[_OpenBlock]) -> list[_OpenBlock]:
    """Join a column of labels with the column of values beside it, so they read row by row.

    Key/value grids (invoice number, dates, totals) would otherwise read as all labels followed
    by all values. Two side-by-side blocks are merged when they share most of their lines and
    are either close together or the right one holds numbers (right-aligned amounts).
    """
    merged = True
    while merged:
        merged = False
        for left in blocks:
            left_lines = {line for line, _ in left.segments}
            for right in blocks:
                if right is left or right.bbox.x0 <= left.bbox.x1:
                    continue
                right_lines = {line for line, _ in right.segments}
                shared = len(left_lines & right_lines)
                if shared == 0 or (shared == 1 and left_lines != right_lines):
                    continue
                if shared < PAIR_LINE_OVERLAP * max(len(left_lines), len(right_lines)):
                    continue
                gap = right.bbox.x0 - left.bbox.x1
                values = [page.segment_text(line, segment) for line, segment in right.segments]
                if gap > PAIR_GAP * left.size and not all(_numeric(value) for value in values):
                    continue
                left.segments.extend(right.segments)
                left.bbox = left.bbox.union(right.bbox)
                blocks.remove(right)
                merged = True
                break
            if merged:
                break
    return blocks


def page_text(page: PageContent) -> str:
    """Plain text in reading order: blocks separated by blank lines, table cells by ' | '."""
    parts: list[str] = []
    for block in page.blocks:
        if block.kind == BlockKind.TABLE and block.table is not None:
            table = page.tables[block.table]
            rows = [table.header, *[row.cells for row in table.rows]]
            parts.append("\n".join(" | ".join(cell for cell in row) for row in rows))
            continue
        lines: dict[int, list[str]] = {}
        for line_index, segment_index in block.segments:
            lines.setdefault(line_index, []).append(page.segment_text(line_index, segment_index))
        parts.append("\n".join("  ".join(texts) for _, texts in sorted(lines.items())))
    return "\n\n".join(part for part in parts if part)


def analyze_layout(page: PageContent, skew: float = 0.0) -> PageContent:
    """Fill lines, tables, blocks and text of a page whose words are already set."""
    page.lines = build_lines(page.words, skew)
    page.tables, table_lines = detect_tables(page)
    page.blocks = build_blocks(page, table_lines)
    page.text = page_text(page)
    return page
