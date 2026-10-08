"""Build PageContent objects from text lines for extraction tests (no PDF, no OCR).

Each line is a list of (x, text) segments; words are laid out left to right from x with a
fixed advance, so the Phase 3 layout analysis (lines, column segments, tables) runs for real.
"""

from __future__ import annotations

from collections.abc import Sequence

from docintel.processing.content import BBox, PageContent, Word
from docintel.processing.inspection import PageMethod
from docintel.processing.layout import analyze_layout

Segment = tuple[float, str]
CHAR_WIDTH = 0.5  # x font size
LINE_PITCH = 1.6  # x font size


def make_page(
    lines: Sequence[Sequence[Segment]],
    *,
    page_number: int = 1,
    size: float = 9.0,
    top: float = 60.0,
    width: float = 595.0,
    height: float = 842.0,
    ocr_confidence: float | None = None,
    sizes: dict[str, float] | None = None,
) -> PageContent:
    """`sizes` overrides the font size of segments by text (e.g. a large letterhead)."""
    words: list[Word] = []
    y = top
    for line in lines:
        line_size = max((sizes or {}).get(text, size) for _, text in line) if line else size
        for x, text in line:
            seg_size = (sizes or {}).get(text, size)
            cursor = x
            for token in text.split():
                advance = len(token) * seg_size * CHAR_WIDTH
                words.append(
                    Word(
                        token,
                        BBox(cursor, y, cursor + advance, y + seg_size),
                        ocr_confidence,
                        seg_size,
                    )
                )
                cursor += advance + seg_size * 0.3
        y += line_size * LINE_PITCH
    page = PageContent(
        page_number=page_number,
        width=width,
        height=height,
        unit="pt",
        method=PageMethod.NATIVE if ocr_confidence is None else PageMethod.OCR,
        words=words,
        ocr_confidence=ocr_confidence,
    )
    return analyze_layout(page)


INVOICE_PAGE_LINES: list[list[Segment]] = [
    [(50, "Kestrel Industrial Supply Inc."), (460, "INVOICE")],
    [(50, "1840 Foundry Road"), (320, "Invoice No."), (420, "INV-2026-0042")],
    [(50, "Dayton, OH 45402"), (320, "Invoice Date"), (420, "03/14/2026")],
    [(50, "Tax ID: US-47-2917735"), (320, "PO Reference"), (420, "PO-2026-10001")],
    [(320, "Due Date"), (420, "04/13/2026")],
    [(320, "Payment Terms"), (420, "Net 30 days")],
    [(50, "Bill To")],
    [(50, "Meridian Manufacturing Co.")],
    [],
    [
        (52, "#"),
        (75, "Item"),
        (140, "Description"),
        (330, "Qty"),
        (370, "Unit"),
        (410, "Unit Price"),
        (490, "Amount"),
    ],
    [
        (52, "1"),
        (75, "BRG-6204"),
        (140, "Deep groove ball bearing"),
        (338, "10"),
        (372, "pcs"),
        (420, "4.85"),
        (495, "48.50"),
    ],
    [
        (52, "2"),
        (75, "VLV-BL050"),
        (140, "Brass ball valve DN50"),
        (338, "2"),
        (372, "pcs"),
        (420, "38.40"),
        (495, "76.80"),
    ],
    [],
    [(320, "Subtotal"), (480, "$ 125.30")],
    [(320, "Sales tax (8.25%)"), (480, "$ 10.34")],
    [(320, "Total Due"), (480, "$ 135.64")],
]
LETTERHEAD_SIZES = {"Kestrel Industrial Supply Inc.": 13.0, "INVOICE": 20.0}


def invoice_page(**overrides: object) -> PageContent:
    lines = overrides.pop("lines", INVOICE_PAGE_LINES)
    return make_page(lines, sizes=LETTERHEAD_SIZES, **overrides)  # type: ignore[arg-type]
