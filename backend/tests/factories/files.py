"""Builders for test files (real PDFs and images, generated deterministically)."""

from __future__ import annotations

import io
import struct
import zlib

from PIL import Image
from reportlab.lib import pdfencrypt
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def pdf_bytes(
    *,
    pages: int = 1,
    text: str | None = "Invoice INV-1001 Total 1,250.00 USD",
    user_password: str | None = None,
    owner_password: str | None = None,
) -> bytes:
    """A real PDF. Pages carry a text layer unless text=None (simulates a scanned page)."""
    encrypt = None
    if user_password is not None or owner_password is not None:
        encrypt = pdfencrypt.StandardEncryption(
            user_password or "", ownerPassword=owner_password or "owner", canPrint=0
        )
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, encrypt=encrypt, invariant=1)
    for number in range(pages):
        if text is not None:
            pdf.drawString(72, 760, f"{text} (page {number + 1})")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def image_only_pdf_bytes(pages: int = 1) -> bytes:
    """A PDF whose pages contain only a raster image (like a scanner output)."""
    image = Image.new("L", (400, 300), color=255)
    buffer = io.BytesIO()
    frames = [image.copy() for _ in range(pages)]
    frames[0].save(buffer, format="PDF", save_all=True, append_images=frames[1:], resolution=72)
    return buffer.getvalue()


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (64, 48), frames: int = 1) -> bytes:
    buffer = io.BytesIO()
    images = [Image.new("RGB", size, color=(255, 255, 255)) for _ in range(frames)]
    if frames > 1:
        images[0].save(buffer, format=fmt, save_all=True, append_images=images[1:])
    else:
        images[0].save(buffer, format=fmt)
    return buffer.getvalue()


def png_header_claiming(width: int, height: int) -> bytes:
    """A tiny PNG whose IHDR claims huge dimensions (decompression-bomb shape)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 64)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def mixed_pdf_bytes(page_texts: list[str | None]) -> bytes:
    """One page per entry: text pages get a text layer; None pages are image-only (scanned)."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=1)
    scan = Image.new("L", (300, 200), color=230)
    for text in page_texts:
        if text is None:
            pdf.drawInlineImage(scan, 72, 400, width=300, height=200)
        else:
            pdf.drawString(72, 760, text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()
