"""Scan simulation: render PDF pages to images and apply seeded, deterministic degradations."""

from __future__ import annotations

import io
import random
import time
from dataclasses import dataclass

from PIL import Image, ImageFilter

from docintel.processing.pdf import PDFIUM_LOCK

# Pillow stamps PDFs with time.gmtime() by default; a fixed struct_time keeps output
# byte-identical for the same seed.
_FIXED_PDF_DATE = time.gmtime(1767225600)  # 2026-01-01T00:00:00Z


@dataclass(frozen=True, slots=True)
class ScanProfile:
    max_rotation_degrees: float
    blur_radius: float
    noise_strength: float  # 0..1 blend of random noise
    jpeg_quality: int


SCAN_PROFILES: dict[str, ScanProfile] = {
    "light": ScanProfile(
        max_rotation_degrees=0.8, blur_radius=0.4, noise_strength=0.04, jpeg_quality=80
    ),
    "heavy": ScanProfile(
        max_rotation_degrees=2.0, blur_radius=1.0, noise_strength=0.10, jpeg_quality=45
    ),
}


def render_pdf_pages(pdf: bytes, dpi: int) -> list[Image.Image]:
    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(pdf)
        try:
            images = []
            for index in range(len(document)):
                page = document[index]
                try:
                    images.append(page.render(scale=dpi / 72).to_pil().convert("L"))
                finally:
                    page.close()
            return images
        finally:
            document.close()


def degrade(image: Image.Image, profile: ScanProfile, rng: random.Random) -> Image.Image:
    angle = rng.uniform(-profile.max_rotation_degrees, profile.max_rotation_degrees)
    result = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
    result = result.filter(ImageFilter.GaussianBlur(profile.blur_radius))
    noise = Image.frombytes("L", result.size, rng.randbytes(result.width * result.height))
    result = Image.blend(result, noise, profile.noise_strength)
    buffer = io.BytesIO()
    result.save(buffer, format="JPEG", quality=profile.jpeg_quality)
    buffer.seek(0)
    with Image.open(buffer) as compressed:
        return compressed.convert("L")


def encode_scan(pages: list[Image.Image], file_format: str, dpi: int) -> bytes:
    buffer = io.BytesIO()
    first, rest = pages[0], pages[1:]
    if file_format == "pdf":
        first.save(
            buffer,
            format="PDF",
            save_all=True,
            append_images=rest,
            resolution=dpi,
            creationDate=_FIXED_PDF_DATE,
            modDate=_FIXED_PDF_DATE,
        )
    elif file_format == "tiff":
        first.save(
            buffer,
            format="TIFF",
            save_all=True,
            append_images=rest,
            compression="tiff_deflate",
            dpi=(dpi, dpi),
        )
    elif file_format == "png":
        first.save(buffer, format="PNG", dpi=(dpi, dpi), optimize=True)
    else:
        msg = f"unsupported scan format: {file_format}"
        raise ValueError(msg)
    return buffer.getvalue()
