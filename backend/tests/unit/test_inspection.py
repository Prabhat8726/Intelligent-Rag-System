from __future__ import annotations

from pathlib import Path

import pytest

from docintel.documents.validation import FileKind
from docintel.processing.inspection import (
    InspectionError,
    InspectionKind,
    PageMethod,
    decide_page_method,
    inspect_file,
)
from tests.factories.files import image_bytes, image_only_pdf_bytes, mixed_pdf_bytes, pdf_bytes


@pytest.mark.parametrize(
    ("text_chars", "images", "method"),
    [
        (0, 0, PageMethod.OCR),  # blank or outlined text: let OCR look
        (0, 1, PageMethod.OCR),  # classic scan
        (16, 0, PageMethod.NATIVE),  # short native page ("Terms: net 30 days.")
        (12, 1, PageMethod.OCR),  # scan with a printed footer/stamp
        (500, 3, PageMethod.NATIVE),  # native page with logos
    ],
)
def test_decide_page_method(text_chars: int, images: int, method: PageMethod) -> None:
    assert decide_page_method(text_chars, images) == method


def _file(tmp_path: Path, content: bytes, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_native_pdf(tmp_path: Path) -> None:
    result = inspect_file(_file(tmp_path, pdf_bytes(pages=3), "a.pdf"), FileKind.PDF)
    assert result.kind == InspectionKind.NATIVE_PDF
    assert result.page_count == 3
    assert result.pages_needing_ocr == []
    assert result.pages[0].width == pytest.approx(595.28, abs=0.01)
    assert result.pages[0].text_chars > 20


def test_mixed_pdf_is_decided_per_page(tmp_path: Path) -> None:
    content = mixed_pdf_bytes(["A full page of native invoice text here", None, "Net 30."])
    result = inspect_file(_file(tmp_path, content, "m.pdf"), FileKind.PDF)
    assert result.kind == InspectionKind.MIXED_PDF
    assert result.pages_needing_ocr == [2]
    assert result.pages[1].image_objects == 1


def test_scanned_pdf_and_images(tmp_path: Path) -> None:
    scanned = inspect_file(_file(tmp_path, image_only_pdf_bytes(2), "s.pdf"), FileKind.PDF)
    assert scanned.kind == InspectionKind.SCANNED_PDF
    assert scanned.pages_needing_ocr == [1, 2]

    tiff = inspect_file(_file(tmp_path, image_bytes("TIFF", (80, 60), 3), "f.tiff"), FileKind.TIFF)
    assert tiff.kind == InspectionKind.IMAGE
    assert [page.page_number for page in tiff.pages] == [1, 2, 3]
    assert (tiff.pages[0].width, tiff.pages[0].height, tiff.pages[0].unit) == (80, 60, "px")

    json_payload = tiff.to_json()
    assert json_payload["pages_needing_ocr"] == [1, 2, 3]
    assert json_payload["kind"] == "image"


def test_corrupt_files_raise_inspection_error(tmp_path: Path) -> None:
    with pytest.raises(InspectionError):
        inspect_file(_file(tmp_path, b"%PDF-1.7 broken", "x.pdf"), FileKind.PDF)
    with pytest.raises(InspectionError):
        inspect_file(_file(tmp_path, image_bytes("PNG")[:60], "x.png"), FileKind.PNG)
