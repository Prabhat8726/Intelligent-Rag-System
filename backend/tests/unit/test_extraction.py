"""Per-page extraction: native vs OCR, coordinates, orientation, fallbacks, concurrency."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from PIL import Image

from docintel.documents.validation import FileKind
from docintel.processing import extraction
from docintel.processing.content import PageContent
from docintel.processing.extraction import ExtractionOptions, extract_document
from docintel.processing.inspection import PageMethod, inspect_file
from docintel.processing.ocr import (
    OCRFailedError,
    OCRProvider,
    OCRResult,
    OCRWord,
    Orientation,
    TesseractOCRProvider,
)
from tests.factories.files import (
    image_file_bytes,
    mixed_pdf_bytes,
    scanned_pdf_bytes,
    text_image,
    text_pdf_bytes,
)

LINES = ["Invoice INV-2026-0042", "Total Due 1,250.00 USD"]
OPTIONS = ExtractionOptions(preview_width=400)


def write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


async def run(
    path: Path,
    kind: FileKind,
    ocr: OCRProvider,
    tmp_path: Path,
    options: ExtractionOptions = OPTIONS,
) -> list[PageContent]:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    return await extract_document(
        path, kind, inspect_file(path, kind), ocr=ocr, options=options, workdir=workdir
    )


async def test_native_pdf_uses_the_text_layer(
    tmp_path: Path, tesseract: TesseractOCRProvider
) -> None:
    path = write(tmp_path, "native.pdf", text_pdf_bytes(LINES))
    (page,) = await run(path, FileKind.PDF, tesseract, tmp_path)
    assert page.method == PageMethod.NATIVE
    assert page.ocr_confidence is None
    assert page.text == "Invoice INV-2026-0042\nTotal Due 1,250.00 USD"
    assert (page.unit, round(page.width), round(page.height)) == ("pt", 595, 842)
    assert page.preview_file is not None
    assert page.preview_file.is_file()
    assert page.preview_size == (400, 566)
    with Image.open(page.preview_file) as preview:
        assert preview.size == (400, 566)


async def test_scanned_pdf_is_ocrd_in_page_points(
    tmp_path: Path, tesseract: TesseractOCRProvider
) -> None:
    path = write(tmp_path, "scan.pdf", scanned_pdf_bytes([text_image(LINES, dpi=150)], dpi=150))
    (page,) = await run(path, FileKind.PDF, tesseract, tmp_path)
    assert page.method == PageMethod.OCR
    assert page.unit == "pt"
    assert page.width == pytest.approx(595.3, abs=1)
    assert page.height == pytest.approx(841.9, abs=1)
    assert page.ocr_confidence is not None
    assert page.ocr_confidence > 85
    assert "INV-2026-0042" in page.text
    assert "1,250.00" in page.text
    invoice = next(word for word in page.words if word.text == "Invoice")
    assert invoice.bbox.x0 == pytest.approx(72, abs=3)  # drawn at x = 72 pt
    assert invoice.confidence is not None


async def test_low_dpi_image_is_upscaled_but_reported_in_its_own_pixels(
    tmp_path: Path, tesseract: TesseractOCRProvider
) -> None:
    image = text_image(LINES, dpi=100)
    path = write(tmp_path, "scan.png", image_file_bytes(image, "PNG", dpi=100))
    (page,) = await run(path, FileKind.PNG, tesseract, tmp_path)
    assert (page.unit, page.width, page.height) == ("px", image.width, image.height)
    invoice = next(word for word in page.words if word.text == "Invoice")
    assert invoice.bbox.x0 == pytest.approx(100, abs=4)  # 72 pt at 100 DPI


async def test_sideways_scan_is_turned_upright(
    tmp_path: Path, tesseract: TesseractOCRProvider
) -> None:
    lines = [f"{line} with enough words to detect the orientation" for line in LINES * 5]
    sideways = text_image(lines, dpi=150).rotate(90, expand=True)
    path = write(tmp_path, "sideways.pdf", scanned_pdf_bytes([sideways], dpi=150))
    (page,) = await run(path, FileKind.PDF, tesseract, tmp_path)
    assert page.rotation_applied == 90
    assert page.width < page.height  # portrait again after correction
    assert "INV-2026-0042" in page.text
    assert page.preview_size is not None
    assert page.preview_size[0] < page.preview_size[1]


async def test_exif_orientation_is_honoured(
    tmp_path: Path, tesseract: TesseractOCRProvider
) -> None:
    stored = text_image(LINES, dpi=200).rotate(90, expand=True)  # camera stored it sideways
    exif = Image.Exif()
    exif[0x0112] = 6  # "rotate 90 CW to display"
    buffer = io.BytesIO()
    stored.save(buffer, format="JPEG", exif=exif, quality=95, dpi=(200, 200))
    path = write(tmp_path, "photo.jpg", buffer.getvalue())
    (page,) = await run(path, FileKind.JPEG, tesseract, tmp_path)
    assert page.rotation_applied == 0  # upright from EXIF alone, no OSD retry needed
    assert "INV-2026-0042" in page.text


async def test_mixed_pdf_decides_per_page(tmp_path: Path, tesseract: TesseractOCRProvider) -> None:
    path = write(tmp_path, "mixed.pdf", mixed_pdf_bytes(["Native page with a text layer", None]))
    first, second = await run(path, FileKind.PDF, tesseract, tmp_path)
    assert first.method == PageMethod.NATIVE
    assert first.text == "Native page with a text layer"
    assert second.method == PageMethod.OCR
    assert second.words == []


async def test_unusable_text_layer_falls_back_to_ocr(
    tmp_path: Path, tesseract: TesseractOCRProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extraction, "MAX_UNUSABLE_CHAR_RATIO", -1.0)  # treat any layer as broken
    path = write(tmp_path, "native.pdf", text_pdf_bytes(LINES))
    (page,) = await run(path, FileKind.PDF, tesseract, tmp_path)
    assert page.method == PageMethod.OCR
    assert page.warnings == ["native_text_unusable"]
    assert "INV-2026-0042" in page.text


class FakeOCR:
    """Records concurrency; fails on chosen page sizes."""

    def __init__(self, *, fail_on_width: int | None = None) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.fail_on_width = fail_on_width

    @property
    def name(self) -> str:
        return "fake"

    async def recognize(self, image: Image.Image, *, dpi: int) -> OCRResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.05)
            if image.width == self.fail_on_width:
                raise OCRFailedError("engine crashed")
            word = OCRWord("Total", 10, 10, 50, 12, 91.0, (1, 1, 1))
            return OCRResult([word, word, word], image.width, image.height, 1.0)
        finally:
            self.active -= 1

    async def detect_orientation(self, image: Image.Image, *, dpi: int) -> Orientation | None:
        return None


async def test_ocr_concurrency_is_bounded(tmp_path: Path) -> None:
    pages = [Image.new("L", (300, 400), 255) for _ in range(6)]
    path = write(tmp_path, "scan.pdf", scanned_pdf_bytes(pages, dpi=72))
    fake = FakeOCR()
    result = await run(path, FileKind.PDF, fake, tmp_path, ExtractionOptions(ocr_concurrency=2))
    assert [page.page_number for page in result] == [1, 2, 3, 4, 5, 6]
    assert fake.calls == 6
    assert fake.max_active == 2


async def test_a_failing_page_does_not_fail_the_document(tmp_path: Path) -> None:
    pages = [Image.new("L", (300, 400), 255), Image.new("L", (360, 400), 255)]
    path = write(tmp_path, "scan.tiff", b"")
    buffer = io.BytesIO()
    pages[0].save(buffer, format="TIFF", save_all=True, append_images=pages[1:], dpi=(300, 300))
    path.write_bytes(buffer.getvalue())
    first, second = await run(path, FileKind.TIFF, FakeOCR(fail_on_width=360), tmp_path)
    assert first.warnings == []
    assert first.words
    assert second.warnings == ["ocr_failed"]
    assert second.words == []
    assert second.preview_file is not None
    assert second.preview_file.is_file()


async def test_oversized_pdf_page_is_rendered_within_the_pixel_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io as _io

    from reportlab.pdfgen import canvas

    buffer = _io.BytesIO()
    huge = canvas.Canvas(buffer, pagesize=(14400, 14400), invariant=1)  # 200 x 200 inches
    huge.showPage()
    huge.save()
    path = write(tmp_path, "huge.pdf", buffer.getvalue())
    seen: list[int] = []

    class SizeRecorder(FakeOCR):
        async def recognize(self, image: Image.Image, *, dpi: int) -> OCRResult:
            seen.append(image.width * image.height)
            return await super().recognize(image, dpi=dpi)

    monkeypatch.setattr(extraction, "MAX_RENDER_PIXELS", 2_000_000)
    (page,) = await run(path, FileKind.PDF, SizeRecorder(), tmp_path)
    assert seen
    assert max(seen) <= 2_000_000 * 1.01
    assert page.width == pytest.approx(14400, rel=0.01)  # still reported in page points
    assert page.preview_size is not None
    assert page.preview_size[0] * page.preview_size[1] <= extraction.MAX_PREVIEW_PIXELS * 1.01


def test_bounded_scale() -> None:
    assert extraction.bounded_scale(1000, 1000, 2.0, 10_000_000) == 2.0
    assert extraction.bounded_scale(1000, 1000, 2.0, 1_000_000) == pytest.approx(1.0)
