"""Native text extraction: words, boxes in displayed orientation, unusable text layers."""

from __future__ import annotations

import io

import pypdfium2 as pdfium
import pytest

from docintel.processing.native import extract_native_words, unusable_char_ratio
from tests.factories.files import text_pdf_bytes


def _ink_box(page: pdfium.PdfPage) -> tuple[int, int, int, int]:
    """Bounding box of dark pixels when the page is rendered at 72 DPI (1 px = 1 pt)."""
    image = page.render(scale=1).to_pil().convert("L")
    box: tuple[int, int, int, int] | None = image.point(
        lambda value: 255 if value < 128 else 0
    ).getbbox()
    assert box is not None
    return box


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_word_boxes_match_the_rendered_page(rotation: int) -> None:
    document = pdfium.PdfDocument(
        io.BytesIO(text_pdf_bytes(["Kestrel M10x40 1840"], rotation=rotation))
    )
    page = document[0]
    words, width, height = extract_native_words(page)
    assert [word.text for word in words] == ["Kestrel", "M10x40", "1840"]
    rendered_width, rendered_height = page.render(scale=1).to_pil().size
    assert abs(width - rendered_width) <= 1
    assert abs(height - rendered_height) <= 1
    left, top, right, bottom = _ink_box(page)
    x0 = min(word.bbox.x0 for word in words)
    y0 = min(word.bbox.y0 for word in words)
    x1 = max(word.bbox.x1 for word in words)
    y1 = max(word.bbox.y1 for word in words)
    # Word boxes (advance width, font ascent/descent) enclose the ink, within a few points.
    assert x0 - 4 <= left
    assert right <= x1 + 4
    assert y0 - 4 <= top
    assert bottom <= y1 + 4
    assert all(word.size == pytest.approx(14) for word in words)
    assert all(word.confidence is None for word in words)
    document.close()


def test_unusable_char_ratio() -> None:
    assert unusable_char_ratio("Invoice 1001 Grüße 請求書") == 0.0
    assert unusable_char_ratio("") == 0.0
    assert unusable_char_ratio(" ab") == pytest.approx(0.6)
    assert unusable_char_ratio("��ok") == pytest.approx(0.5)
