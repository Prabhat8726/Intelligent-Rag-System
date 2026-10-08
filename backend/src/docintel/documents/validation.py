"""Upload validation: the gate every file passes before it is stored.

Checks, in order: size, non-empty, file type from magic bytes (the declared extension and
MIME type must agree with it), then a structural parse (PDF opens without a password and has
1..N pages; images decode headers within pixel and frame limits). Nothing here trusts the
client-supplied filename or content type.
"""

from __future__ import annotations

import asyncio
import hashlib
import unicodedata
import warnings
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError

from docintel.core.errors import (
    PayloadTooLargeError,
    UnprocessableContentError,
    UnsupportedMediaTypeError,
)
from docintel.processing.pdf import PdfOpenError, pdf_page_count

_HASH_CHUNK = 1024 * 1024
_MAX_DISPLAY_NAME_BYTES = 200
_FORBIDDEN_NAME_CHARS = frozenset('<>:"|?*/\\')


class FileKind(StrEnum):
    PDF = "pdf"
    PNG = "png"
    JPEG = "jpeg"
    TIFF = "tiff"


MIME_TYPES: dict[FileKind, str] = {
    FileKind.PDF: "application/pdf",
    FileKind.PNG: "image/png",
    FileKind.JPEG: "image/jpeg",
    FileKind.TIFF: "image/tiff",
}
STORAGE_EXTENSIONS: dict[FileKind, str] = {
    FileKind.PDF: "pdf",
    FileKind.PNG: "png",
    FileKind.JPEG: "jpg",
    FileKind.TIFF: "tiff",
}
_EXTENSION_KINDS: dict[str, FileKind] = {
    ".pdf": FileKind.PDF,
    ".png": FileKind.PNG,
    ".jpg": FileKind.JPEG,
    ".jpeg": FileKind.JPEG,
    ".tif": FileKind.TIFF,
    ".tiff": FileKind.TIFF,
}
_DECLARED_ALIASES: dict[FileKind, frozenset[str]] = {
    FileKind.PDF: frozenset({"application/pdf", "application/x-pdf"}),
    FileKind.PNG: frozenset({"image/png"}),
    FileKind.JPEG: frozenset({"image/jpeg", "image/jpg", "image/pjpeg"}),
    FileKind.TIFF: frozenset({"image/tiff", "image/tif"}),
}
# Browsers and HTTP clients send these when they don't know better; they carry no claim.
_GENERIC_DECLARED = frozenset({"", "application/octet-stream", "binary/octet-stream"})
_PIL_FORMATS: dict[FileKind, str] = {
    FileKind.PNG: "PNG",
    FileKind.JPEG: "JPEG",
    FileKind.TIFF: "TIFF",
}
SUPPORTED_EXTENSIONS = tuple(sorted(_EXTENSION_KINDS))


@dataclass(frozen=True, slots=True)
class UploadLimits:
    max_bytes: int
    max_pages: int
    max_image_pixels: int


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    kind: FileKind
    mime_type: str
    storage_extension: str
    display_filename: str
    size_bytes: int
    sha256: str
    page_count: int


# ------------------------------------------------------------------------------ filenames
def sanitize_filename(raw: str | None, *, default: str = "document") -> str:
    """Display-only filename: basename, NFKC, no control/bidi/reserved characters, bounded."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFKC", name)
    name = "".join(
        ch
        for ch in name
        if ch not in _FORBIDDEN_NAME_CHARS and not unicodedata.category(ch).startswith("C")
    )
    name = " ".join(name.split()).strip(" .")
    if len(name.encode()) > _MAX_DISPLAY_NAME_BYTES:
        stem, dot, extension = name.rpartition(".")
        if not dot or len(extension) > 10:
            stem, extension = name, ""
        budget = _MAX_DISPLAY_NAME_BYTES - len(extension.encode()) - (1 if extension else 0)
        stem = stem.encode()[:budget].decode(errors="ignore").rstrip(" .")
        name = f"{stem}.{extension}" if extension else stem
    return name or default


# ------------------------------------------------------------------------------ file type
def sniff_kind(header: bytes) -> FileKind | None:
    """Identify the file type from its leading bytes (never from the client's claims)."""
    if header.startswith(b"%PDF-"):
        return FileKind.PDF
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileKind.PNG
    if header.startswith(b"\xff\xd8\xff"):
        return FileKind.JPEG
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return FileKind.TIFF
    return None


def resolve_kind(filename: str, declared_content_type: str | None, header: bytes) -> FileKind:
    supported = ", ".join(SUPPORTED_EXTENSIONS)
    extension = Path(filename.lower()).suffix
    claimed = _EXTENSION_KINDS.get(extension)
    if claimed is None:
        msg = f"Unsupported file extension. Supported: {supported}."
        raise UnsupportedMediaTypeError(msg)
    detected = sniff_kind(header)
    if detected is None:
        msg = "The file content is not a supported document type (PDF, PNG, JPEG or TIFF)."
        raise UnsupportedMediaTypeError(msg)
    if detected != claimed:
        msg = "The file extension does not match the file content."
        raise UnsupportedMediaTypeError(msg)
    declared = (declared_content_type or "").split(";", 1)[0].strip().lower()
    if declared not in _GENERIC_DECLARED and declared not in _DECLARED_ALIASES[detected]:
        msg = "The declared content type does not match the file content."
        raise UnsupportedMediaTypeError(msg)
    return detected


# ------------------------------------------------------------------------------ structure
def _image_page_count(path: Path, kind: FileKind, limits: UploadLimits) -> int:
    too_large = UnprocessableContentError(
        f"Image exceeds the maximum of {limits.max_image_pixels:,} pixels."
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format != _PIL_FORMATS[kind]:
                    msg = "The image content does not match its declared format."
                    raise UnprocessableContentError(msg)
                width, height = image.size
                if width < 1 or height < 1:
                    msg = "The image has no pixels."
                    raise UnprocessableContentError(msg)
                if width * height > limits.max_image_pixels:
                    raise too_large
                frames = int(getattr(image, "n_frames", 1))
                if frames > limits.max_pages:
                    msg = f"The image has {frames} pages; the maximum is {limits.max_pages}."
                    raise UnprocessableContentError(msg)
                image.verify()  # structural integrity (e.g. PNG chunk CRCs)
                return frames
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise too_large from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as exc:
        msg = "The image is corrupt or unreadable."
        raise UnprocessableContentError(msg) from exc


def _pdf_page_count(path: Path, limits: UploadLimits) -> int:
    try:
        pages = pdf_page_count(path)
    except PdfOpenError as exc:
        if exc.password_protected:
            msg = "Password-protected PDFs are not supported. Remove the password and retry."
            raise UnprocessableContentError(msg) from exc
        msg = "The PDF is corrupt or unreadable."
        raise UnprocessableContentError(msg) from exc
    if pages < 1:
        msg = "The PDF has no pages."
        raise UnprocessableContentError(msg)
    if pages > limits.max_pages:
        msg = f"The PDF has {pages} pages; the maximum is {limits.max_pages}."
        raise UnprocessableContentError(msg)
    return pages


def count_pages(path: Path, kind: FileKind, limits: UploadLimits) -> int:
    if kind == FileKind.PDF:
        return _pdf_page_count(path, limits)
    return _image_page_count(path, kind, limits)


# ------------------------------------------------------------------------------ entrypoint
def _digest(stream: BinaryIO, max_bytes: int) -> tuple[str, int, bytes]:
    digest = hashlib.sha256()
    size = 0
    header = b""
    while chunk := stream.read(_HASH_CHUNK):
        if not header:
            header = chunk[:16]
        size += len(chunk)
        if size > max_bytes:
            msg = f"The file exceeds the maximum upload size of {max_bytes // (1024 * 1024)} MB."
            raise PayloadTooLargeError(msg)
        digest.update(chunk)
    return digest.hexdigest(), size, header


def validate_file(
    path: Path, *, filename: str | None, content_type: str | None, limits: UploadLimits
) -> ValidatedUpload:
    with path.open("rb") as stream:
        sha256, size, header = _digest(stream, limits.max_bytes)
    if size == 0:
        msg = "The file is empty."
        raise UnprocessableContentError(msg)
    display_name = sanitize_filename(filename)
    kind = resolve_kind(display_name, content_type, header)
    pages = count_pages(path, kind, limits)
    return ValidatedUpload(
        kind=kind,
        mime_type=MIME_TYPES[kind],
        storage_extension=STORAGE_EXTENSIONS[kind],
        display_filename=display_name,
        size_bytes=size,
        sha256=sha256,
        page_count=pages,
    )


async def validate_upload(
    path: Path, *, filename: str | None, content_type: str | None, limits: UploadLimits
) -> ValidatedUpload:
    """Run validation off the event loop (hashing and parsing are CPU/IO bound)."""
    return await asyncio.to_thread(
        validate_file, path, filename=filename, content_type=content_type, limits=limits
    )
