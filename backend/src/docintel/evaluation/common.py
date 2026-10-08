"""Shared helpers for evaluation suites: run the production extraction path on files."""

from __future__ import annotations

import asyncio
import io
import random
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from PIL import Image

from docintel.documents.validation import FileKind
from docintel.processing.content import Line, PageContent, Word
from docintel.processing.extraction import ExtractionOptions, extract_document
from docintel.processing.inspection import inspect_file
from docintel.processing.ocr import OCRProvider
from docintel.synthetic.degrade import SCAN_PROFILES, degrade, encode_scan, render_pdf_pages

DEFAULT_PARALLELISM = 4


async def extract_file(
    path: Path, kind: FileKind, ocr: OCRProvider, options: ExtractionOptions
) -> list[PageContent]:
    """Exactly what the worker does to a stored file (minus storage and database)."""
    inspection = await asyncio.to_thread(inspect_file, path, kind)
    with tempfile.TemporaryDirectory(prefix="docintel-eval-") as workdir:
        return await extract_document(
            path, kind, inspection, ocr=ocr, options=options, workdir=Path(workdir)
        )


def lines_text(words: list[Word], lines: list[Line]) -> str:
    """Serialize lines top-to-bottom, words left-to-right: the same function for reference and
    OCR output, so differences measure recognition, not reading order."""
    return "\n".join(" ".join(words[i].text for i in line.word_indices) for line in lines)


def scan_pdf(pdf: bytes, *, profile: str = "light", dpi: int = 150, seed: int = 0) -> bytes:
    """Render a native PDF to degraded page images and wrap them in an image-only PDF."""
    rng = random.Random(seed)  # noqa: S311  (reproducible degradation, not security)
    pages = [degrade(page, SCAN_PROFILES[profile], rng) for page in render_pdf_pages(pdf, dpi)]
    return encode_scan(pages, "pdf", dpi)


def png_bytes(image: Image.Image, dpi: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", dpi=(dpi, dpi))
    return buffer.getvalue()


async def run_bounded[T, R](
    items: Iterable[T], worker: Callable[[T], Awaitable[R]], parallelism: int
) -> list[R]:
    limiter = asyncio.Semaphore(parallelism)

    async def guarded(item: T) -> R:
        async with limiter:
            return await worker(item)

    return list(await asyncio.gather(*(guarded(item) for item in items)))
