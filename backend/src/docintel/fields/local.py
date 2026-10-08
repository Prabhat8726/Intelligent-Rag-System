"""Deterministic layout extractor: labelled values, letterhead names, line-item tables and
section lists, read from the Phase 3 page representation. No model, no network.

It is the zero-cost first pass, the only extractor for content that may not leave the platform,
and an independent second opinion when an LLM also runs (agreement is a confidence signal).
Every candidate it returns is read off the page, so its location and OCR confidence are known.

Rules, in order of strength:
1. label segment followed by its value on the same line ("Invoice No." | "INV-001");
2. label and value in one segment ("Tax ID: US-47-2917735", "VAT (20%)");
3. label with the value on the next line in the same column ("Bill To" / "Meridian Co.");
4. letterhead: the largest text at the top of page 1 (issuer names, titles), weaker;
5. derived: currency from the symbols of the amounts.
Label matching is exact after normalization; a near-exact match (OCR noise) is weaker.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from statistics import median

from rapidfuzz import fuzz

from docintel.fields.candidates import Candidate, ExtractorOutput, Origin, RowCandidate
from docintel.fields.normalize import (
    NormalizationContext,
    comparable,
    detect_currency,
    find_dates,
    find_numbers,
    normalize_label,
    normalize_value,
    squash,
)
from docintel.fields.schemas import (
    SCHEMA_INFO,
    ColumnField,
    ListField,
    ScalarField,
    SchemaInfo,
    TableField,
    ValueType,
)
from docintel.processing.content import BBox, DocumentTable, PageContent, Word

FUZZY_LABEL_RATIO = 90.0  # OCR-damaged labels ("lnvoice No.") still match
_FUZZY_LABEL_MIN_LENGTH = 6
_BELOW_MAX_GAP = 2.5  # value line at most this many text sizes below the label
_BELOW_MAX_LINES = 3
_PAIR_GAP = 6.0  # max gap (x text size) between a label and its value (layout.PAIR_GAP)
_SKEW_LEVEL = 0.9  # max centre offset (x text size) for a value on the neighbouring line
_LETTERHEAD_REGION = 0.3  # top share of page 1 searched for the issuer name
_LETTERHEAD_MIN_RATIO = 1.15  # letterhead text must stand out from the body text
_LETTERHEAD_MIN_LETTERS = 4

ANCHOR_SAME_LINE = 1.0
ANCHOR_IN_SEGMENT = 1.0
ANCHOR_BELOW = 0.95
ANCHOR_FUZZY_LABEL = 0.95
ANCHOR_HEADER_EXACT = 1.0
ANCHOR_HEADER_INFERRED = 0.9
ANCHOR_SECTION_LIST = 0.9
ANCHOR_DERIVED_EXPLICIT = 0.9
ANCHOR_DERIVED_ASSUMED = 0.7

TITLE_KEYS = frozenset(
    squash(title)
    for title in (
        "invoice",
        "tax invoice",
        "commercial invoice",
        "purchase order",
        "delivery note",
        "packing slip",
        "dispatch note",
        "receipt",
        "sales receipt",
        "statement",
        "bank statement",
        "account statement",
        "credit note",
        "quotation",
        "contract",
        "agreement",
        "policy",
        "curriculum vitae",
        "resume",
    )
)
# Labels of every schema: none of them is ever a company or person name.
ANY_SCHEMA_LABELS = frozenset(
    normalize_label(label)
    for info in SCHEMA_INFO.values()
    for field in info.scalars
    for label in field.meta.labels
)
_VALUE_START = re.compile(r"^[\d$€£¥₹(+-]|^[A-Z]{3}\s?\d")
_SEPARATORS = ":#- " + chr(0x2013)  # colon, hash, hyphen, space, en dash
_LIST_SPLIT = re.compile(r"\s*[,;•·|]\s*|\s+-\s+")


@dataclass(frozen=True, slots=True)
class _Segment:
    page: int
    line: int
    index: int
    words: tuple[Word, ...]
    text: str
    label: str
    bbox: BBox
    size: float

    @property
    def ocr_confidence(self) -> float | None:
        return _mean_confidence(self.words)


def _mean_confidence(words: Sequence[Word]) -> float | None:
    weighted = [(w.confidence, len(w.text)) for w in words if w.confidence is not None]
    total = sum(weight for _, weight in weighted)
    if not total:
        return None
    return round(sum(c * weight for c, weight in weighted) / total, 2)


class _Layout:
    def __init__(self, page: PageContent) -> None:
        self.page = page
        self.lines: list[list[_Segment]] = []
        for line_index, line in enumerate(page.lines):
            segments: list[_Segment] = []
            for segment_index, indices in enumerate(line.segments):
                words = tuple(page.words[i] for i in indices)
                if not words:
                    continue
                text = " ".join(word.text for word in words)
                segments.append(
                    _Segment(
                        page=page.page_number,
                        line=line_index,
                        index=segment_index,
                        words=words,
                        text=text,
                        label=normalize_label(text),
                        bbox=BBox.enclosing([word.bbox for word in words]),
                        size=float(median(word.size for word in words)),
                    )
                )
            self.lines.append(segments)
        sizes = [segment.size for line in self.lines for segment in line]
        self.body_size = float(median(sizes)) if sizes else 0.0

    def segments(self) -> Iterator[_Segment]:
        for line in self.lines:
            yield from line


@dataclass(frozen=True, slots=True)
class _LabelMatch:
    rest: str  # raw text after the label inside the segment ("" = label only)
    colon: bool
    fuzzy: bool


def _match_label(segment: _Segment, label: str) -> _LabelMatch | None:
    if segment.label == label:
        return _LabelMatch("", segment.text.rstrip().endswith(":"), fuzzy=False)
    words = segment.text.split()
    for count in range(1, len(words)):
        head = normalize_label(" ".join(words[:count]))
        if head == label:
            rest = " ".join(words[count:]).lstrip(_SEPARATORS).strip()
            colon = words[count - 1].endswith(":") or words[count].startswith(":")
            # "Tax ID: ..." must not count as the label "tax": after a one-word label the
            # rest has to look like a value or be introduced by a colon.
            if not colon and " " not in label and not _VALUE_START.match(rest):
                return None
            return _LabelMatch(rest, colon, fuzzy=False)
        if not label.startswith(head):
            break
    if (
        len(label) >= _FUZZY_LABEL_MIN_LENGTH
        and fuzz.ratio(segment.label, label) >= FUZZY_LABEL_RATIO
    ):
        return _LabelMatch("", colon=False, fuzzy=True)
    return None


@dataclass(slots=True)
class _Found:
    rank: int
    position: tuple[int, int, int]
    candidate: Candidate
    key: object


class LayoutExtractor:
    """Extracts candidates for one schema from a document's pages and stitched tables."""

    def __init__(self, schema: SchemaInfo) -> None:
        self._schema = schema
        self._all_labels = frozenset(
            normalize_label(label) for field in schema.scalars for label in field.meta.labels
        )
        # "Subtotal", "Total Due", "VAT (20%)" rows are summaries, not line items.
        self._summary_labels = frozenset(
            normalize_label(label)
            for field in schema.scalars
            if field.meta.type == ValueType.MONEY
            for label in field.meta.labels
        )

    # ------------------------------------------------------------------ public
    def extract(
        self,
        pages: Sequence[PageContent],
        tables: Sequence[DocumentTable],
        context: NormalizationContext | None = None,
    ) -> ExtractorOutput:
        context = context or NormalizationContext()
        layouts = [_Layout(page) for page in pages]
        output = ExtractorOutput()
        for field in self._schema.scalars:
            candidate = self._scalar(field, layouts, context)
            if candidate is not None:
                output.scalars[field.name] = candidate
        self._derive_currency(output)
        if self._schema.table is not None:
            output.rows = self._rows(self._schema.table, tables, pages)
        for list_field in self._schema.lists:
            output.lists[list_field.name] = self._list(list_field, layouts)
        return output

    # ------------------------------------------------------------------ scalars
    def _is_label(self, segment: _Segment) -> bool:
        if segment.label in self._all_labels:
            return True
        return any(segment.label.startswith(f"{label} ") for label in self._all_labels) and (
            ":" in segment.text
        )

    def _below(self, layout: _Layout, segment: _Segment) -> _Segment | None:
        reach = max(segment.size, 1.0)
        left, right = segment.bbox.x0 - 2 * reach, segment.bbox.x1 + 2 * reach
        for line in layout.lines[segment.line + 1 : segment.line + 1 + _BELOW_MAX_LINES]:
            if not line:
                continue
            if line[0].bbox.y0 - segment.bbox.y1 > _BELOW_MAX_GAP * reach:
                return None
            for other in line:
                if other.bbox.x1 >= left and other.bbox.x0 <= right:
                    return None if self._is_label(other) else other
        return None

    def _right_on_adjacent_line(self, layout: _Layout, segment: _Segment) -> _Segment | None:
        """A value right of its label that line grouping put on the neighbouring line (a
        slightly rotated scan splits a label/value pair into two lines)."""
        reach = max(segment.size, 1.0)
        for index in (segment.line - 1, segment.line + 1):
            if not 0 <= index < len(layout.lines):
                continue
            for other in layout.lines[index]:
                beside = 0 <= other.bbox.x0 - segment.bbox.x1 <= _PAIR_GAP * reach
                level = abs(other.bbox.center_y - segment.bbox.center_y) <= _SKEW_LEVEL * reach
                if beside and level:
                    return other
        return None

    def _typed_value(
        self, field: ScalarField, text: str, context: NormalizationContext
    ) -> str | None:
        """The part of `text` that is the value, if it parses as the field's type."""
        text = text.strip()
        if not text:
            return None
        if field.meta.type == ValueType.DATE:
            readings = [r for r in find_dates(text, context) if r.alternatives]
            if not readings:
                return None
            reading = readings[-1] if field.meta.range_part == "end" else readings[0]
            return text[reading.span[0] : reading.span[1]]
        is_text = field.meta.type in (ValueType.ORGANIZATION, ValueType.PERSON, ValueType.TEXT)
        if is_text and normalize_label(text) in self._all_labels:
            return None
        if not normalize_value(field.meta.type, text, context).valid:
            return None
        return text

    def _candidates_for_segment(
        self,
        field: ScalarField,
        layout: _Layout,
        line: list[_Segment],
        segment: _Segment,
        match: _LabelMatch,
        context: NormalizationContext,
    ) -> Candidate | None:
        meta = field.meta
        label_anchor = ANCHOR_FUZZY_LABEL if match.fuzzy else 1.0
        if meta.value_in_label:
            percent = re.search(r"\d+(?:[.,]\d+)?\s*%", segment.text)
            if percent is not None:
                return Candidate(
                    percent.group(0),
                    segment.page,
                    segment.text,
                    Origin.LOCAL,
                    anchor=ANCHOR_IN_SEGMENT * label_anchor,
                    bbox=segment.bbox,
                    ocr_confidence=segment.ocr_confidence,
                    method=f"in label '{segment.text}'",
                )
        if match.rest:
            value = self._typed_value(field, match.rest, context)
            if value is not None:
                return Candidate(
                    value,
                    segment.page,
                    segment.text,
                    Origin.LOCAL,
                    anchor=ANCHOR_IN_SEGMENT * label_anchor,
                    bbox=segment.bbox,
                    ocr_confidence=segment.ocr_confidence,
                    method=f"label '{segment.text[: len(segment.text) - len(match.rest)].strip()}'",
                )
        options: list[tuple[_Segment, float, str]] = []
        position = line.index(segment)
        if position + 1 < len(line):
            options.append((line[position + 1], ANCHOR_SAME_LINE, "right of"))
        else:
            skewed = self._right_on_adjacent_line(layout, segment)
            if skewed is not None:
                options.append((skewed, ANCHOR_BELOW, "right of (adjacent line)"))
        below = self._below(layout, segment)
        if below is not None:
            options.append((below, ANCHOR_BELOW, "below"))
        for value_segment, anchor, where in options:
            if self._is_label(value_segment):
                continue
            value = self._typed_value(field, value_segment.text, context)
            if value is None:
                continue
            words = (*segment.words, *value_segment.words)
            return Candidate(
                value,
                segment.page,
                f"{segment.text} {value_segment.text}",
                Origin.LOCAL,
                anchor=anchor * label_anchor,
                bbox=segment.bbox.union(value_segment.bbox),
                ocr_confidence=_mean_confidence(words),
                method=f"{where} label '{segment.text}'",
            )
        return None

    def _scalar(
        self, field: ScalarField, layouts: list[_Layout], context: NormalizationContext
    ) -> Candidate | None:
        labels = [normalize_label(label) for label in field.meta.labels]
        found: list[_Found] = []
        for layout in layouts:
            for line in layout.lines:
                for segment in line:
                    for rank, label in enumerate(labels):
                        match = _match_label(segment, label)
                        if match is None:
                            continue
                        candidate = self._candidates_for_segment(
                            field, layout, line, segment, match, context
                        )
                        if candidate is not None:
                            normalized = normalize_value(
                                field.meta.type, candidate.raw_value, context
                            )
                            found.append(
                                _Found(
                                    rank,
                                    (segment.page, segment.line, segment.index),
                                    candidate,
                                    comparable(field.meta.type, normalized.value)
                                    or candidate.raw_value,
                                )
                            )
                        break  # labels are ordered most specific first
        if found:
            best_rank = min(item.rank for item in found)
            same_rank = [item for item in found if item.rank == best_rank]
            same_rank.sort(key=lambda item: item.position, reverse=field.meta.occurrence == "last")
            chosen = same_rank[0]
            chosen.candidate.conflicts = len({repr(item.key) for item in same_rank}) - 1
            return chosen.candidate
        if field.meta.letterhead > 0 and layouts:
            return self._letterhead(field, layouts[0])
        return None

    def _letterhead(self, field: ScalarField, layout: _Layout) -> Candidate | None:
        """Largest text at the top of page 1 - or, when nothing stands out (OCR size estimates
        are noisy), the topmost substantial line. Titles, labels, dates and table headers are
        never names."""
        limit = layout.page.height * _LETTERHEAD_REGION
        is_name = field.meta.type in (ValueType.ORGANIZATION, ValueType.PERSON)
        table_areas = [table.bbox for table in layout.page.tables]
        options = []
        for segment in layout.segments():
            if segment.bbox.y0 > limit or self._is_label(segment):
                continue
            if sum(ch.isalpha() for ch in segment.text) < _LETTERHEAD_MIN_LETTERS:
                continue
            if any(_inside(segment.bbox, area) for area in table_areas):
                continue
            if is_name and (squash(segment.text) in TITLE_KEYS or ":" in segment.text):
                continue  # squashed: OCR splits "DELIVERY NOTE" into "DELIVERY N OTE"
            if is_name and segment.label in ANY_SCHEMA_LABELS:
                continue  # e.g. "Currency" on a delivery note, whose schema has no currency
            if is_name and normalize_value(ValueType.DATE, segment.text).valid:
                continue
            options.append(segment)
        if not options:
            return None
        largest = max(options, key=lambda segment: (segment.size, -segment.bbox.y0))
        anchor = field.meta.letterhead
        if layout.body_size and largest.size >= _LETTERHEAD_MIN_RATIO * layout.body_size:
            best, how = largest, "letterhead (largest text at the top of page 1)"
        else:
            best = min(options, key=lambda segment: (segment.bbox.y0, segment.bbox.x0))
            how = "letterhead (first line at the top of page 1; no text stands out)"
            anchor *= 0.8  # a weak guess
        return Candidate(
            best.text,
            best.page,
            best.text,
            Origin.LOCAL,
            anchor=round(anchor, 4),
            bbox=best.bbox,
            ocr_confidence=best.ocr_confidence,
            method=how,
        )

    def _derive_currency(self, output: ExtractorOutput) -> None:
        field = self._schema.scalar("currency")
        if field is None or "currency" in output.scalars:
            return
        money = [
            (name, candidate)
            for name, candidate in output.scalars.items()
            if (scalar := self._schema.scalar(name)) is not None
            and scalar.meta.type == ValueType.MONEY
        ]
        for _, candidate in money:
            detected = detect_currency(candidate.raw_value)
            if detected is None:
                continue
            code, explicit = detected
            symbol = _currency_token(candidate.raw_value) or code
            output.scalars["currency"] = Candidate(
                symbol,
                candidate.page,
                candidate.source_text,
                Origin.DERIVED,
                anchor=ANCHOR_DERIVED_EXPLICIT if explicit else ANCHOR_DERIVED_ASSUMED,
                bbox=candidate.bbox,
                ocr_confidence=candidate.ocr_confidence,
                method=f"currency of the printed amount '{candidate.raw_value}'",
            )
            return

    # ------------------------------------------------------------------ tables
    def _rows(
        self, table_field: TableField, tables: Sequence[DocumentTable], pages: Sequence[PageContent]
    ) -> list[RowCandidate]:
        mapped = [
            (table, mapping, score)
            for table in tables
            if (result := map_columns(table, table_field.columns)) is not None
            for mapping, score in [result]
        ]
        if not mapped:
            return []
        best_columns = max(mapped, key=lambda item: item[2])[1]
        # Tables a page break split under the same header are all line items.
        chosen = [
            (table, mapping)
            for table, mapping, _ in mapped
            if {name for name, _ in mapping.values()} == {name for name, _ in best_columns.values()}
        ]
        words_by_page = {page.page_number: page.words for page in pages}
        rows: list[RowCandidate] = []
        for table, mapping in chosen:
            for row in table.rows:
                cells: dict[str, Candidate] = {}
                page_words = words_by_page.get(row.page_number, [])
                for index, (name, anchor) in mapping.items():
                    if index >= len(row.cells) or not row.cells[index].strip():
                        continue
                    text = row.cells[index].strip()
                    words = _words_in(page_words, row.bbox, text)
                    cells[name] = Candidate(
                        text,
                        row.page_number,
                        text,
                        Origin.LOCAL,
                        anchor=anchor,
                        bbox=BBox.enclosing([w.bbox for w in words]) if words else row.bbox,
                        ocr_confidence=_mean_confidence(words),
                        method=f"table column '{_header_text(table, index)}'",
                    )
                _split_quantity_unit(cells, table_field)
                _split_code_description(cells, table_field)
                if _plausible_row(cells, table_field) and not self._summary_row(cells):
                    rows.append(
                        RowCandidate(
                            row.page_number,
                            " ".join(cell for cell in row.cells if cell.strip()),
                            cells,
                            row.bbox,
                        )
                    )
        return rows

    def _summary_row(self, cells: dict[str, Candidate]) -> bool:
        description = cells.get("description")
        if description is None:
            return False
        label = normalize_label(description.raw_value)
        return any(
            label == summary or label.startswith(f"{summary} ") for summary in self._summary_labels
        )

    # ------------------------------------------------------------------ lists
    def _list(self, list_field: ListField, layouts: list[_Layout]) -> list[Candidate]:
        sections = {normalize_label(section) for section in list_field.meta.sections}
        for layout in layouts:
            for line in layout.lines:
                for segment in line:
                    head = segment.label.split(" ")
                    if segment.label not in sections and not (
                        ":" in segment.text and " ".join(head[:2]) in sections
                    ):
                        continue
                    return self._list_items(layout, segment)
        return []

    def _list_items(self, layout: _Layout, heading: _Segment) -> list[Candidate]:
        items: list[Candidate] = []
        texts: list[_Segment] = []
        _, _, inline = heading.text.partition(":")
        if inline.strip():
            texts.append(heading)
        previous = heading
        for line in layout.lines[heading.line + 1 :]:
            if not line:
                continue
            first = line[0]
            if first.bbox.y0 - previous.bbox.y1 > _BELOW_MAX_GAP * max(previous.size, 1.0):
                break
            if first.size >= heading.size * 1.1 or (first.text.isupper() and len(line) == 1):
                break  # next section heading
            texts.extend(line)
            previous = first
        for segment in texts:
            source = segment.text.partition(":")[2] if segment is heading else segment.text
            for part in _LIST_SPLIT.split(source):
                value = part.strip(" .")
                if len(value) >= 2 and any(ch.isalpha() for ch in value):
                    items.append(
                        Candidate(
                            value,
                            segment.page,
                            segment.text,
                            Origin.LOCAL,
                            anchor=ANCHOR_SECTION_LIST,
                            bbox=segment.bbox,
                            ocr_confidence=segment.ocr_confidence,
                            method=f"listed under '{heading.text.partition(':')[0].strip()}'",
                        )
                    )
        return items


_NUMERIC_TYPES = frozenset({ValueType.MONEY, ValueType.QUANTITY, ValueType.INTEGER})
_CURRENCY_NOISE = re.compile(r"US\$|Rs\.?|[A-Z]{3}|[$€£¥₹%]")


def numeric_cell(text: str) -> bool:
    """A numeric table cell holds one number (and at most a currency mark), nothing else."""
    if len(find_numbers(text)) != 1:
        return False
    rest = _CURRENCY_NOISE.sub("", text)
    return not any(ch.isalpha() for ch in rest)


def _plausible_row(cells: dict[str, Candidate], table_field: TableField) -> bool:
    """Drop rows that are not line items (totals, footers, wrapped prose the table detector
    swept in): numeric columns must hold numbers, required columns must be present."""
    types = {column.name: column.meta.type for column in table_field.columns}
    for name in [name for name in cells if types.get(name) in _NUMERIC_TYPES]:
        if not numeric_cell(cells[name].raw_value):
            del cells[name]
    required = [column.name for column in table_field.columns if column.meta.required]
    if any(name not in cells for name in required):
        return False
    numeric_columns = [name for name, kind in types.items() if kind in _NUMERIC_TYPES]
    return not numeric_columns or any(name in cells for name in numeric_columns)


def _inside(box: BBox, area: BBox) -> bool:
    return area.x0 <= box.center_x <= area.x1 and area.y0 <= box.center_y <= area.y1


_QUANTITY_WITH_UNIT = re.compile(r"^(\d[\d.,]*)\s+([A-Za-z]{1,6}\.?)$")


def _split_quantity_unit(cells: dict[str, Candidate], table_field: TableField) -> None:
    """'10 pcs' in the quantity column when the unit column was lost (OCR merged the header)."""
    quantity = cells.get("quantity")
    if (
        quantity is None
        or "unit" in cells
        or not any(column.name == "unit" for column in table_field.columns)
    ):
        return
    match = _QUANTITY_WITH_UNIT.match(quantity.raw_value)
    if match is None:
        return
    cells["quantity"] = Candidate(
        match.group(1),
        quantity.page,
        quantity.source_text,
        Origin.LOCAL,
        anchor=ANCHOR_HEADER_INFERRED,
        bbox=quantity.bbox,
        ocr_confidence=quantity.ocr_confidence,
        method=f"{quantity.method}, split from '{quantity.raw_value}'",
    )
    cells["unit"] = Candidate(
        match.group(2),
        quantity.page,
        quantity.source_text,
        Origin.LOCAL,
        anchor=ANCHOR_HEADER_INFERRED,
        bbox=quantity.bbox,
        ocr_confidence=quantity.ocr_confidence,
        method=f"{quantity.method}, split from '{quantity.raw_value}'",
    )


_LEADING_CODE = re.compile(r"^([A-Z0-9]+(?:[-/.][A-Z0-9]+)+|[A-Z]{2,}\d[A-Z0-9]*)\s+(\S.*)$")


def _split_code_description(cells: dict[str, Candidate], table_field: TableField) -> None:
    """'BRG-6204 Deep groove ball bearing' in the description column when the item-code column
    was lost (OCR merged the header): the leading code becomes the SKU."""
    description = cells.get("description")
    if description is None or "sku" in cells:
        return
    if not any(column.name == "sku" for column in table_field.columns):
        return
    match = _LEADING_CODE.match(description.raw_value)
    if match is None or not any(ch.isdigit() for ch in match.group(1)):
        return
    note = f"{description.method}, split from '{description.raw_value}'"
    cells["sku"] = Candidate(
        match.group(1),
        description.page,
        description.source_text,
        Origin.LOCAL,
        anchor=ANCHOR_HEADER_INFERRED,
        bbox=description.bbox,
        ocr_confidence=description.ocr_confidence,
        method=note,
    )
    cells["description"] = Candidate(
        match.group(2),
        description.page,
        description.source_text,
        Origin.LOCAL,
        anchor=ANCHOR_HEADER_INFERRED,
        bbox=description.bbox,
        ocr_confidence=description.ocr_confidence,
        method=note,
    )


def _currency_token(text: str) -> str | None:
    match = re.search(r"[A-Z]{3}|US\$|Rs\.?|[$€£¥₹]", text)
    return match.group(0) if match else None


def _header_text(table: DocumentTable, index: int) -> str:
    return table.header[index] if index < len(table.header) else ""


def _words_in(words: Sequence[Word], area: BBox, text: str) -> list[Word]:
    """Words inside `area` that make up `text` (a table cell)."""
    wanted = text.split()
    inside = [
        word
        for word in words
        if word.bbox.center_y >= area.y0
        and word.bbox.center_y <= area.y1
        and word.bbox.center_x >= area.x0 - 1
        and word.bbox.center_x <= area.x1 + 1
    ]
    for start in range(len(inside)):
        window = inside[start : start + len(wanted)]
        if [word.text for word in window] == wanted:
            return window
    return []


def _clean_header(text: str) -> str:
    return normalize_label(re.sub(r"^[^\w#]+", "", text))


def map_columns(
    table: DocumentTable, columns: Sequence[ColumnField]
) -> tuple[dict[int, tuple[str, float]], float] | None:
    """Map table columns to row-model columns by header text (content decides the rest).

    Returns {column index: (field name, anchor)} and a score, or None if the table does not
    carry the model's required columns.
    """
    headers = [_clean_header(text) for text in table.header]
    taken: dict[int, tuple[str, float]] = {}
    used: set[str] = set()
    # Pass 1: exact header synonyms, most specific synonym first.
    for column in columns:
        synonyms = [normalize_label(s) for s in column.meta.headers]
        for rank, synonym in enumerate(synonyms):
            index = next(
                (i for i, header in enumerate(headers) if header == synonym and i not in taken),
                None,
            )
            if index is not None:
                taken[index] = (column.name, ANCHOR_HEADER_EXACT if rank == 0 else 0.98)
                used.add(column.name)
                break
    # Pass 2: header starts with a synonym ("qty delivered" -> quantity).
    for column in columns:
        if column.name in used:
            continue
        synonyms = [normalize_label(s) for s in column.meta.headers]
        for index, header in enumerate(headers):
            if index in taken or not header:
                continue
            if any(header.startswith(f"{s} ") or header.endswith(f" {s}") for s in synonyms):
                taken[index] = (column.name, ANCHOR_HEADER_INFERRED)
                used.add(column.name)
                break
    # Pass 3: content. An unlabelled column of 1, 2, 3, ... is the line number; a generic
    # "item" column holding prose is the description.
    names = {column.name for column in columns}
    for index, header in enumerate(headers):
        values = [row.cells[index] for row in table.rows if index < len(row.cells)]
        if (
            index not in taken
            and "line_number" in names
            and "line_number" not in used
            and values
            and all(v.strip().isdigit() for v in values)
        ):
            taken[index] = ("line_number", ANCHOR_HEADER_INFERRED)
            used.add("line_number")
        if (
            taken.get(index, ("",))[0] == "sku"
            and header == "item"
            and "description" in names
            and "description" not in used
            and values
            and sum(" " in v.strip() for v in values) > len(values) / 2
        ):
            taken[index] = ("description", ANCHOR_HEADER_INFERRED)
            used.discard("sku")
            used.add("description")
    required = {column.name for column in columns if column.meta.required}
    if not required <= used or len(used) < 2:
        return None
    return taken, float(len(used))
