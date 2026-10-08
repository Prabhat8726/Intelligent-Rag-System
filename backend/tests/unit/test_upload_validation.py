from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from docintel.core.errors import (
    PayloadTooLargeError,
    UnprocessableContentError,
    UnsupportedMediaTypeError,
)
from docintel.documents.validation import (
    FileKind,
    UploadLimits,
    sanitize_filename,
    sniff_kind,
    validate_file,
)
from tests.factories.files import (
    image_bytes,
    image_only_pdf_bytes,
    pdf_bytes,
    png_header_claiming,
)

LIMITS = UploadLimits(max_bytes=2 * 1024 * 1024, max_pages=5, max_image_pixels=1_000_000)


def _write(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "upload.bin"
    path.write_bytes(content)
    return path


def _validate(tmp_path: Path, content: bytes, filename: str, content_type: str | None = None):  # type: ignore[no-untyped-def]
    return validate_file(
        _write(tmp_path, content), filename=filename, content_type=content_type, limits=LIMITS
    )


# ------------------------------------------------------------------------------ accepted
@pytest.mark.parametrize(
    ("content", "filename", "content_type", "kind", "pages"),
    [
        (pdf_bytes(pages=3), "invoice.pdf", "application/pdf", FileKind.PDF, 3),
        (image_only_pdf_bytes(2), "scan.PDF", "application/octet-stream", FileKind.PDF, 2),
        (pdf_bytes(owner_password="owner-only"), "restricted.pdf", None, FileKind.PDF, 1),
        (image_bytes("PNG"), "receipt.png", "image/png", FileKind.PNG, 1),
        (image_bytes("JPEG"), "photo.jpeg", "image/jpeg", FileKind.JPEG, 1),
        (image_bytes("JPEG"), "photo.jpg", "image/jpg", FileKind.JPEG, 1),
        (image_bytes("TIFF", frames=3), "fax.tiff", "image/tiff", FileKind.TIFF, 3),
    ],
)
def test_valid_files_are_accepted(
    tmp_path: Path,
    content: bytes,
    filename: str,
    content_type: str | None,
    kind: FileKind,
    pages: int,
) -> None:
    result = _validate(tmp_path, content, filename, content_type)
    assert result.kind == kind
    assert result.page_count == pages
    assert result.size_bytes == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()


# ------------------------------------------------------------------------------ rejected
@pytest.mark.parametrize(
    ("content", "filename", "content_type"),
    [
        (b"<html><script>alert(1)</script></html>", "invoice.pdf", "application/pdf"),
        (image_bytes("PNG"), "invoice.pdf", "application/pdf"),  # PNG renamed to .pdf
        (pdf_bytes(), "invoice.png", "image/png"),  # PDF renamed to .png
        (pdf_bytes(), "invoice.pdf", "text/html"),  # declared type contradicts content
        (b"MZ\x90\x00 fake executable", "tool.exe", None),
        (pdf_bytes(), "no_extension", None),
        (b"%PDX-not-really", "doc.pdf", None),
    ],
)
def test_type_mismatches_and_unsupported_types_are_rejected(
    tmp_path: Path, content: bytes, filename: str, content_type: str | None
) -> None:
    with pytest.raises(UnsupportedMediaTypeError):
        _validate(tmp_path, content, filename, content_type)


@pytest.mark.parametrize(
    ("content", "filename", "message"),
    [
        (pdf_bytes(user_password="secret"), "locked.pdf", "Password-protected"),
        (pdf_bytes(pages=6), "long.pdf", "maximum is 5"),
        (b"%PDF-1.7\n garbage that is not a pdf", "broken.pdf", "corrupt"),
        (image_bytes("PNG", size=(1200, 1000)), "huge.png", "pixels"),
        (png_header_claiming(100_000, 100_000), "bomb.png", "pixels"),
        (image_bytes("TIFF", frames=6), "fax.tif", "maximum is 5"),
        (image_bytes("PNG")[:40], "truncated.png", "corrupt"),
        (b"", "empty.pdf", "empty"),
    ],
)
def test_unprocessable_content_is_rejected(
    tmp_path: Path, content: bytes, filename: str, message: str
) -> None:
    with pytest.raises(UnprocessableContentError, match=message):
        _validate(tmp_path, content, filename)


def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    content = pdf_bytes() + b"\n%" + b"x" * (LIMITS.max_bytes + 10)
    with pytest.raises(PayloadTooLargeError):
        _validate(tmp_path, content, "big.pdf")


# ------------------------------------------------------------------------------ helpers
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("invoice.pdf", "invoice.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\me\\Desktop\\scan 01.pdf", "scan 01.pdf"),
        ("invoice\u202efdp.exe", "invoicefdp.exe"),  # right-to-left override removed
        ("re\x00port\n.pdf", "report.pdf"),
        ("   .hidden.pdf  ", "hidden.pdf"),
        ('a<b>c:"d|e?f*.pdf', "abcdef.pdf"),
        ("\uff46\uff55\uff4c\uff4c\uff57\uff49\uff44\uff54\uff48.pdf", "fullwidth.pdf"),  # NFKC
        ("", "document"),
        (None, "document"),
        ("../", "document"),
    ],
)
def test_sanitize_filename(raw: str | None, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_bounds_length_and_keeps_extension() -> None:
    result = sanitize_filename("é" * 300 + ".pdf")
    assert result.endswith(".pdf")
    assert len(result.encode()) <= 200


@pytest.mark.parametrize(
    ("header", "kind"),
    [
        (b"%PDF-1.4", FileKind.PDF),
        (b"\x89PNG\r\n\x1a\n", FileKind.PNG),
        (b"\xff\xd8\xff\xe0", FileKind.JPEG),
        (b"II*\x00", FileKind.TIFF),
        (b"MM\x00*", FileKind.TIFF),
        (b"GIF89a", None),
        (b"  %PDF-1.4", None),  # strict: header must be at offset 0
    ],
)
def test_sniff_kind(header: bytes, kind: FileKind | None) -> None:
    assert sniff_kind(header) == kind
