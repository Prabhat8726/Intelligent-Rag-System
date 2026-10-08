"""Words with bounding boxes from a PDF text layer (pypdfium2).

PDFium reports character boxes in unrotated PDF user space (origin bottom-left). They are
converted to the page's displayed orientation with a top-left origin, matching rendered page
images, so the same boxes work for previews, layout analysis and evidence highlighting.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from docintel.processing.content import BBox, Word

# A horizontal jump larger than this fraction of the font size inside a run of non-space
# characters starts a new word (PDFs often position words without emitting space characters).
_WORD_BREAK_GAP = 0.2
# Text layers where more than this share of characters is unusable (private-use glyphs,
# replacement characters, control codes) come from broken font encodings: OCR the page instead.
MAX_UNUSABLE_CHAR_RATIO = 0.2

Transform = Callable[[float, float], tuple[float, float]]


def _display_transform(page: pdfium.PdfPage) -> tuple[Transform, float, float]:
    """Map unrotated PDF coordinates to displayed, top-left-origin coordinates."""
    left, bottom, right, top = page.get_cropbox()
    width, height = right - left, top - bottom
    rotation = page.get_rotation() % 360

    def transform(x: float, y: float) -> tuple[float, float]:
        ux, uy = x - left, y - bottom
        if rotation == 90:
            return uy, ux
        if rotation == 180:
            return width - ux, uy
        if rotation == 270:
            return height - uy, width - ux
        return ux, height - uy

    if rotation in (90, 270):
        return transform, height, width
    return transform, width, height


def _box(transform: Transform, left: float, bottom: float, right: float, top: float) -> BBox:
    xa, ya = transform(left, bottom)
    xb, yb = transform(right, top)
    return BBox(min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb))


def unusable_char_ratio(text: str) -> float:
    """Share of characters that cannot be real text (script-neutral)."""
    characters = [ch for ch in text if not ch.isspace()]
    if not characters:
        return 0.0
    bad = sum(
        1 for ch in characters if ch == "�" or unicodedata.category(ch) in {"Co", "Cn", "Cc", "Cs"}
    )
    return bad / len(characters)


def extract_native_words(page: pdfium.PdfPage) -> tuple[list[Word], float, float]:
    """Return (words, display width, display height) for one page. Caller holds PDFIUM_LOCK."""
    transform, width, height = _display_transform(page)
    text_page = page.get_textpage()
    try:
        words: list[Word] = []
        chars: list[str] = []
        box: BBox | None = None
        sizes: list[float] = []
        last_right = 0.0

        def flush() -> None:
            nonlocal box
            if chars and box is not None:
                text = unicodedata.normalize("NFKC", "".join(chars)).strip()
                if text:
                    sizes.sort()
                    words.append(Word(text, box, None, sizes[len(sizes) // 2]))
            chars.clear()
            sizes.clear()
            box = None

        for index in range(text_page.count_chars()):
            char = text_page.get_text_range(index, 1)
            if not char or char.isspace():
                flush()
                continue
            # Loose boxes span the glyph's advance width and the font's ascent/descent: tight
            # boxes make narrow glyphs ("1", "l") look like word breaks and vary in height.
            left, bottom, right, top = text_page.get_charbox(index, loose=True)
            size = float(pdfium_c.FPDFText_GetFontSize(text_page.raw, index)) or (top - bottom)
            if chars and (left - last_right > size * _WORD_BREAK_GAP or left < last_right - size):
                flush()
            char_box = _box(transform, left, bottom, right, top)
            box = char_box if box is None else box.union(char_box)
            chars.append(char)
            sizes.append(size)
            last_right = right
        flush()
        return words, width, height
    finally:
        text_page.close()
