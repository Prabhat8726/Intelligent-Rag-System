"""File inspection - first stage of the understanding pipeline (Module 3).

Decides per page whether a usable text layer exists (NATIVE) or OCR is needed (OCR), and records
geometry needed later for bounding boxes. Mixed PDFs (some pages scanned) are common, so the
decision is per page, never per document.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import pypdfium2.raw as pdfium_c
from PIL import Image, ImageSequence

from docintel.documents.validation import FileKind
from docintel.processing.pdf import PdfOpenError, open_pdf

# A page with at least this many non-whitespace characters has a usable text layer. Pages with
# less text are ambiguous (see decide_page_method). To be calibrated on labelled pages in Phase 10.
MIN_NATIVE_TEXT_CHARS = 20
INSPECTION_VERSION = 1


class PageMethod(StrEnum):
    NATIVE = "NATIVE"
    OCR = "OCR"


class InspectionKind(StrEnum):
    NATIVE_PDF = "native_pdf"
    SCANNED_PDF = "scanned_pdf"
    MIXED_PDF = "mixed_pdf"
    IMAGE = "image"


class InspectionError(Exception):
    """The stored file cannot be read (corrupt or unsupported). Not retryable."""


@dataclass(frozen=True, slots=True)
class PageInspection:
    page_number: int
    width: float
    height: float
    unit: str  # "pt" for PDF pages, "px" for images
    rotation: int
    text_chars: int
    image_objects: int
    method: PageMethod
    dpi: tuple[float, float] | None = None


@dataclass(frozen=True, slots=True)
class DocumentInspection:
    kind: InspectionKind
    page_count: int
    pages: list[PageInspection] = field(default_factory=list)
    version: int = INSPECTION_VERSION

    @property
    def pages_needing_ocr(self) -> list[int]:
        return [page.page_number for page in self.pages if page.method == PageMethod.OCR]

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["pages_needing_ocr"] = self.pages_needing_ocr
        return data


def decide_page_method(text_chars: int, image_objects: int) -> PageMethod:
    """Decide whether a PDF page's text layer is trustworthy or the page needs OCR.

    * no text at all -> OCR (scans, and "outlined" text drawn as vector paths)
    * plenty of text -> NATIVE
    * a little text: NATIVE when the page has no images (the text layer is all there is);
      OCR when it also has images - the typical shape of a scan with a printed stamp/footer.
    """
    if text_chars == 0:
        return PageMethod.OCR
    if text_chars >= MIN_NATIVE_TEXT_CHARS or image_objects == 0:
        return PageMethod.NATIVE
    return PageMethod.OCR


def _meaningful_chars(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def inspect_pdf(path: Path) -> DocumentInspection:
    pages: list[PageInspection] = []
    try:
        with open_pdf(path) as document:
            for index in range(len(document)):
                page = document[index]
                try:
                    width, height = page.get_size()
                    text_page = page.get_textpage()
                    try:
                        text_chars = _meaningful_chars(text_page.get_text_range())
                    finally:
                        text_page.close()
                    images = sum(1 for _ in page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,)))
                    pages.append(
                        PageInspection(
                            page_number=index + 1,
                            width=round(float(width), 2),
                            height=round(float(height), 2),
                            unit="pt",
                            rotation=int(page.get_rotation()),
                            text_chars=text_chars,
                            image_objects=images,
                            method=decide_page_method(text_chars, images),
                        )
                    )
                finally:
                    page.close()
    except PdfOpenError as exc:
        raise InspectionError(f"PDF cannot be opened: {exc}") from exc

    native = sum(1 for page in pages if page.method == PageMethod.NATIVE)
    if native == len(pages):
        kind = InspectionKind.NATIVE_PDF
    elif native == 0:
        kind = InspectionKind.SCANNED_PDF
    else:
        kind = InspectionKind.MIXED_PDF
    return DocumentInspection(kind=kind, page_count=len(pages), pages=pages)


def _dpi(image: Image.Image) -> tuple[float, float] | None:
    dpi = image.info.get("dpi")
    if isinstance(dpi, tuple) and len(dpi) == 2:
        return (round(float(dpi[0]), 2), round(float(dpi[1]), 2))
    return None


def inspect_image(path: Path) -> DocumentInspection:
    pages: list[PageInspection] = []
    try:
        with Image.open(path) as image:
            for index, frame in enumerate(ImageSequence.Iterator(image)):
                frame.load()  # full decode: detects truncated or corrupt pixel data
                width, height = frame.size
                pages.append(
                    PageInspection(
                        page_number=index + 1,
                        width=float(width),
                        height=float(height),
                        unit="px",
                        rotation=0,
                        text_chars=0,
                        image_objects=1,
                        method=PageMethod.OCR,
                        dpi=_dpi(frame),
                    )
                )
    except (OSError, SyntaxError, ValueError, EOFError) as exc:
        raise InspectionError(f"image cannot be decoded: {type(exc).__name__}") from exc
    return DocumentInspection(kind=InspectionKind.IMAGE, page_count=len(pages), pages=pages)


def inspect_file(path: Path, kind: FileKind) -> DocumentInspection:
    if kind == FileKind.PDF:
        return inspect_pdf(path)
    return inspect_image(path)
