"""Per-page text extraction: native text layer or OCR, then layout analysis and previews.

Decisions per page (Module 3):
* NATIVE pages (per inspection) use the PDF text layer - unless it is unusable (broken font
  encodings), in which case the page is OCR'd and a warning recorded.
* OCR pages are rendered at OCR_DPI (PDF) or taken as-is (images, upscaled when their DPI is
  low: measured to improve word F1 on synthetic scans, see evaluation reports).
* A poor OCR reading triggers orientation detection; the rotated reading is kept only if it
  is better, so a wrong orientation guess can never make a page worse.
Pages are processed concurrently (OCR bounded by a semaphore); PDFium calls stay serialized.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

from docintel.documents.validation import FileKind
from docintel.processing.content import BBox, PageContent, Word, mean_confidence
from docintel.processing.inspection import DocumentInspection, PageInspection, PageMethod
from docintel.processing.layout import analyze_layout
from docintel.processing.native import (
    MAX_UNUSABLE_CHAR_RATIO,
    extract_native_words,
    unusable_char_ratio,
)
from docintel.processing.ocr import HEIGHT_TO_SIZE, OCRFailedError, OCRProvider, OCRResult
from docintel.processing.pdf import open_pdf
from docintel.processing.preprocess import clean_ocr_words, deskew, remove_ruling_lines

# Assumed page width when an image carries no DPI metadata (A4 portrait, inches).
_ASSUMED_PAGE_WIDTH_INCHES = 8.27
# Pixel budgets for anything rendered or upscaled. The upload validator limits page count and
# image pixels but not the size of a PDF page (up to 200 x 200 inches), so a single page could
# otherwise render to gigapixels. A4 at 600 DPI is ~35 MP.
MAX_RENDER_PIXELS = 40_000_000
MAX_PREVIEW_PIXELS = 4_000_000
_MIN_DPI, _MAX_DPI = 72, 1200


@dataclass(frozen=True, slots=True)
class ExtractionOptions:
    ocr_dpi: int = 300
    upscale_below_dpi: int = 250
    preview_width: int = 1000
    ocr_concurrency: int = 2
    # Readings below this mean confidence (or with almost no words) get orientation detection.
    retry_orientation_below: float = 60.0
    remove_ruling_lines: bool = False
    deskew: bool = True


@dataclass(frozen=True, slots=True)
class _OCRPage:
    words: list[Word]
    width: float
    height: float
    rotation: int
    skew: float
    deskew_degrees: float
    confidence: float | None
    image: Image.Image


def bounded_scale(width: float, height: float, scale: float, max_pixels: int) -> float:
    """`scale`, reduced if needed so that the scaled width x height stays within the budget."""
    pixels = width * height * scale * scale
    if pixels <= max_pixels or pixels <= 0:
        return scale
    return scale * math.sqrt(max_pixels / pixels)


def _save_preview(image: Image.Image, width: int, path: Path) -> tuple[int, int]:
    if image.width > width:
        height = max(1, round(image.height * width / image.width))
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    image.save(path, format="PNG", optimize=False)
    return image.width, image.height


def _native_page(
    path: Path, index: int, preview_width: int, preview_path: Path
) -> tuple[list[Word], float, float, tuple[int, int], float]:
    with open_pdf(path) as document:
        page = document[index]
        try:
            words, width, height = extract_native_words(page)
            scale = bounded_scale(
                width, height, preview_width / width if width else 1.0, MAX_PREVIEW_PIXELS
            )
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil().convert("RGB")
        finally:
            page.close()
    ratio = unusable_char_ratio("".join(word.text for word in words))
    return words, width, height, _save_preview(image, preview_width, preview_path), ratio


def _render_pdf_page(path: Path, index: int, dpi: int) -> tuple[Image.Image, float]:
    """Render for OCR at `dpi` (less for oversized pages); returns the image and its DPI."""
    with open_pdf(path) as document:
        page = document[index]
        try:
            width, height = page.get_size()
            scale = bounded_scale(width, height, dpi / 72, MAX_RENDER_PIXELS)
            image: Image.Image = page.render(scale=scale).to_pil().convert("L")
            return image, scale * 72
        finally:
            page.close()


def _image_frame(path: Path, index: int) -> tuple[Image.Image, float]:
    with Image.open(path) as image:
        for position, frame in enumerate(ImageSequence.Iterator(image)):
            if position == index:
                upright = ImageOps.exif_transpose(frame.copy()) or frame.copy()
                dpi_info = frame.info.get("dpi")
                gray = upright.convert("L")
                break
        else:
            msg = f"image has no frame {index}"
            raise OCRFailedError(msg)
    if isinstance(dpi_info, tuple) and dpi_info and float(dpi_info[0]) > 1:
        dpi = float(dpi_info[0])
    else:
        dpi = gray.width / _ASSUMED_PAGE_WIDTH_INCHES
    return gray, min(max(dpi, _MIN_DPI), _MAX_DPI)


def _words_from_ocr(result: OCRResult, to_units: float) -> list[Word]:
    words: list[Word] = []
    for word in clean_ocr_words(result.words):
        text = word.text
        box = BBox(
            word.left * to_units,
            word.top * to_units,
            (word.left + word.width) * to_units,
            (word.top + word.height) * to_units,
        )
        words.append(Word(text, box, word.confidence, word.height * HEIGHT_TO_SIZE * to_units))
    return words


async def _ocr_image(
    image: Image.Image, dpi: int, to_units: float, ocr: OCRProvider, options: ExtractionOptions
) -> _OCRPage:
    if options.remove_ruling_lines:
        image = await asyncio.to_thread(remove_ruling_lines, image, dpi)
    deskewed = 0.0
    if options.deskew:
        image, deskewed = await asyncio.to_thread(deskew, image)
    result = await ocr.recognize(image, dpi=dpi)
    rotation = 0
    confidence = result.mean_confidence
    if len(result.words) < 3 or confidence is None or confidence < options.retry_orientation_below:
        orientation = await ocr.detect_orientation(image, dpi=dpi)
        if orientation is not None and orientation.rotate:
            rotated = image.rotate(-orientation.rotate, expand=True)
            second = await ocr.recognize(rotated, dpi=dpi)
            if second.quality > result.quality:
                result, image, rotation = second, rotated, orientation.rotate
    return _OCRPage(
        words=_words_from_ocr(result, to_units),
        width=image.width * to_units,
        height=image.height * to_units,
        rotation=rotation,
        # After the image deskew the page is level; the engine's line slopes are noisy in
        # tables (one cell per "line"), so they are only used when deskewing is off.
        skew=0.0 if options.deskew else result.skew,
        deskew_degrees=deskewed,
        confidence=result.mean_confidence,
        image=image,
    )


async def _ocr_page(
    path: Path,
    kind: FileKind,
    info: PageInspection,
    ocr: OCRProvider,
    options: ExtractionOptions,
    preview_path: Path,
) -> tuple[PageContent, float]:
    index = info.page_number - 1
    if kind == FileKind.PDF:
        image, render_dpi = await asyncio.to_thread(_render_pdf_page, path, index, options.ocr_dpi)
        dpi, to_units, unit = round(render_dpi), 72 / render_dpi, "pt"
    else:
        image, source_dpi = await asyncio.to_thread(_image_frame, path, index)
        factor = 1.0
        if source_dpi < options.upscale_below_dpi:
            wanted = min(2.0, options.ocr_dpi / source_dpi)
            factor = max(1.0, bounded_scale(image.width, image.height, wanted, MAX_RENDER_PIXELS))
        if factor > 1.0:
            size = (round(image.width * factor), round(image.height * factor))
            image = await asyncio.to_thread(image.resize, size, Image.Resampling.LANCZOS)
        dpi, to_units, unit = round(source_dpi * factor), 1 / factor, "px"

    page_content = PageContent(info.page_number, 0, 0, unit, PageMethod.OCR, [])
    try:
        reading = await _ocr_image(image, dpi, to_units, ocr, options)
    except OCRFailedError:
        page_content.width, page_content.height = image.width * to_units, image.height * to_units
        page_content.warnings.append("ocr_failed")
        page_content.preview_size = await asyncio.to_thread(
            _save_preview, image, options.preview_width, preview_path
        )
        return page_content, 0.0
    page_content.width, page_content.height = reading.width, reading.height
    page_content.words = reading.words
    page_content.rotation_applied = reading.rotation
    page_content.deskew_degrees = reading.deskew_degrees
    page_content.ocr_confidence = reading.confidence
    page_content.preview_size = await asyncio.to_thread(
        _save_preview, reading.image, options.preview_width, preview_path
    )
    return page_content, reading.skew


async def extract_document(
    path: Path,
    kind: FileKind,
    inspection: DocumentInspection,
    *,
    ocr: OCRProvider,
    options: ExtractionOptions,
    workdir: Path,
) -> list[PageContent]:
    """Extract every page; previews are written to `workdir` (PageContent.preview_file)."""
    limiter = asyncio.Semaphore(options.ocr_concurrency)

    async def process(info: PageInspection) -> PageContent:
        preview_path = workdir / f"page-{info.page_number:04d}.png"
        warnings: list[str] = []
        if kind == FileKind.PDF and info.method == PageMethod.NATIVE:
            words, width, height, preview_size, unusable = await asyncio.to_thread(
                _native_page, path, info.page_number - 1, options.preview_width, preview_path
            )
            if unusable <= MAX_UNUSABLE_CHAR_RATIO:
                page = PageContent(info.page_number, width, height, "pt", PageMethod.NATIVE, words)
                page.preview_size = preview_size
                page.preview_file = preview_path
                return await asyncio.to_thread(analyze_layout, page)
            warnings.append("native_text_unusable")
        async with limiter:
            page, skew = await _ocr_page(path, kind, info, ocr, options, preview_path)
        page.warnings = warnings + page.warnings
        page.preview_file = preview_path
        if page.ocr_confidence is None:
            page.ocr_confidence = mean_confidence(page.words)
        return await asyncio.to_thread(analyze_layout, page, skew)

    pages = await asyncio.gather(*(process(info) for info in inspection.pages))
    return sorted(pages, key=lambda page: page.page_number)
