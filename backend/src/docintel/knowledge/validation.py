"""Knowledge uploads: file validation and document metadata (Module 12).

Markdown and plain text are validated here (UTF-8, no binary content, size cap, optional
front matter); PDFs and images go through the same gate as business documents. Metadata comes
from the form first and the front matter second; anything missing gets a documented default.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from docintel.core.errors import (
    PayloadTooLargeError,
    UnprocessableContentError,
    UnsupportedMediaTypeError,
)
from docintel.db.models import KnowledgeCategory, KnowledgeFormat, Sensitivity
from docintel.documents.validation import (
    FileKind,
    UploadLimits,
    sanitize_filename,
    sniff_kind,
    validate_file,
)
from docintel.knowledge.sources import parse_markdown, parse_text

TEXT_FORMATS: dict[str, KnowledgeFormat] = {
    ".md": KnowledgeFormat.MARKDOWN,
    ".markdown": KnowledgeFormat.MARKDOWN,
    ".txt": KnowledgeFormat.TEXT,
}
TEXT_MIME_TYPES: dict[KnowledgeFormat, str] = {
    KnowledgeFormat.MARKDOWN: "text/markdown",
    KnowledgeFormat.TEXT: "text/plain",
}
TEXT_EXTENSIONS: dict[KnowledgeFormat, str] = {
    KnowledgeFormat.MARKDOWN: "md",
    KnowledgeFormat.TEXT: "txt",
}
_FILE_FORMATS: dict[FileKind, KnowledgeFormat] = {
    FileKind.PDF: KnowledgeFormat.PDF,
    FileKind.PNG: KnowledgeFormat.PNG,
    FileKind.JPEG: KnowledgeFormat.JPEG,
    FileKind.TIFF: KnowledgeFormat.TIFF,
}
_DECLARED_TEXT = frozenset(
    {
        "",
        "application/octet-stream",
        "binary/octet-stream",
        "text/markdown",
        "text/x-markdown",
        "text/plain",
    }
)
# Control characters other than tab, newline, carriage return and form feed mean binary data.
_BINARY = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_MAX_TITLE = 300
_MAX_VERSION = 50


@dataclass(frozen=True, slots=True)
class ValidatedKnowledgeFile:
    format: KnowledgeFormat
    mime_type: str
    storage_extension: str
    display_filename: str
    size_bytes: int
    sha256: str
    page_count: int | None  # PDFs and images; None for text
    text: str | None  # decoded Markdown / plain text


def validate_text_file(
    path: Path, *, filename: str | None, content_type: str | None, max_bytes: int
) -> ValidatedKnowledgeFile:
    display_name = sanitize_filename(filename, default="knowledge.md")
    knowledge_format = TEXT_FORMATS.get(Path(display_name.lower()).suffix)
    if knowledge_format is None:
        msg = "Unsupported file extension for a text document. Supported: .md, .markdown, .txt."
        raise UnsupportedMediaTypeError(msg)
    size = path.stat().st_size
    if size > max_bytes:
        msg = f"Text documents may be at most {max_bytes // 1024} KB."
        raise PayloadTooLargeError(msg)
    data = path.read_bytes()
    if sniff_kind(data[:16]) is not None:
        msg = "The file extension does not match the file content."
        raise UnsupportedMediaTypeError(msg)
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared not in _DECLARED_TEXT:
        msg = "The declared content type does not match a text document."
        raise UnsupportedMediaTypeError(msg)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        msg = "Text documents must be UTF-8 encoded."
        raise UnprocessableContentError(msg) from exc
    if _BINARY.search(text):
        msg = "The file contains binary data, not text."
        raise UnprocessableContentError(msg)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        msg = "The file is empty."
        raise UnprocessableContentError(msg)
    return ValidatedKnowledgeFile(
        format=knowledge_format,
        mime_type=TEXT_MIME_TYPES[knowledge_format],
        storage_extension=TEXT_EXTENSIONS[knowledge_format],
        display_filename=display_name,
        size_bytes=size,
        sha256=hashlib.sha256(data).hexdigest(),
        page_count=None,
        text=text,
    )


def validate_knowledge_file(
    path: Path,
    *,
    filename: str | None,
    content_type: str | None,
    limits: UploadLimits,
    max_text_bytes: int,
) -> ValidatedKnowledgeFile:
    suffix = Path(sanitize_filename(filename).lower()).suffix
    if suffix in TEXT_FORMATS:
        return validate_text_file(
            path, filename=filename, content_type=content_type, max_bytes=max_text_bytes
        )
    validated = validate_file(path, filename=filename, content_type=content_type, limits=limits)
    return ValidatedKnowledgeFile(
        format=_FILE_FORMATS[validated.kind],
        mime_type=validated.mime_type,
        storage_extension=validated.storage_extension,
        display_filename=validated.display_filename,
        size_bytes=validated.size_bytes,
        sha256=validated.sha256,
        page_count=validated.page_count,
        text=None,
    )


def front_matter(validated: ValidatedKnowledgeFile) -> tuple[dict[str, str], str | None]:
    """(front matter, first heading) of a text document; empty for PDFs and images."""
    if validated.text is None:
        return {}, None
    parser = parse_markdown if validated.format == KnowledgeFormat.MARKDOWN else parse_text
    try:
        source = parser(validated.text)
    except ValueError as exc:
        msg = f"Invalid front matter: {exc}."
        raise UnprocessableContentError(msg) from exc
    heading = next((unit.text for unit in source.units if unit.kind == "heading"), None)
    return source.metadata, heading


# ------------------------------------------------------------------------------ metadata
@dataclass(frozen=True, slots=True)
class MetadataInput:
    """Form fields of an upload (all optional; they win over the front matter)."""

    title: str | None = None
    document_key: str | None = None
    category: KnowledgeCategory | None = None
    version_label: str | None = None
    sensitivity: Sensitivity | None = None
    effective_from: date | None = None
    effective_to: date | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeMetadata:
    title: str
    title_from_content: bool  # PDFs/images without a given title: the first heading wins later
    document_key: str
    category: KnowledgeCategory
    version_label: str | None
    sensitivity: Sensitivity
    effective_from: date | None
    effective_to: date | None
    department_name: str | None  # front matter `department:` (resolved by the service)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:100].rstrip("-")


def _date(value: str | None, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        msg = f"{field} must be a date in YYYY-MM-DD format."
        raise UnprocessableContentError(msg) from exc


def _choice[E: (KnowledgeCategory, Sensitivity)](
    enum_cls: type[E], value: str | None, field: str
) -> E | None:
    if not value:
        return None
    try:
        return enum_cls(value.strip().upper())
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_cls)
        msg = f"{field} must be one of: {allowed}."
        raise UnprocessableContentError(msg) from exc


def resolve_metadata(
    form: MetadataInput,
    front: dict[str, str],
    *,
    first_heading: str | None,
    filename: str,
) -> KnowledgeMetadata:
    title = (form.title or front.get("title") or first_heading or "").strip()
    title_from_content = not title
    if not title:
        title = Path(filename).stem.replace("_", " ").replace("-", " ").strip() or "Untitled"
    if len(title) > _MAX_TITLE:
        msg = f"title may be at most {_MAX_TITLE} characters."
        raise UnprocessableContentError(msg)

    key = (form.document_key or front.get("document_key") or "").strip() or slugify(title)
    if not _KEY.fullmatch(key):
        msg = (
            "document_key must be 1-100 lowercase letters, digits or hyphens "
            "(the same key for every version of a document)."
        )
        raise UnprocessableContentError(msg)

    category = form.category or _choice(KnowledgeCategory, front.get("category"), "category")
    if category is None:
        allowed = ", ".join(member.value for member in KnowledgeCategory)
        msg = f"category is required ({allowed})."
        raise UnprocessableContentError(msg)

    version = (form.version_label or front.get("version") or "").strip() or None
    if version is not None and len(version) > _MAX_VERSION:
        msg = f"version may be at most {_MAX_VERSION} characters."
        raise UnprocessableContentError(msg)

    sensitivity = (
        form.sensitivity
        or _choice(Sensitivity, front.get("sensitivity"), "sensitivity")
        or Sensitivity.INTERNAL
    )
    effective_from = form.effective_from or _date(front.get("effective_from"), "effective_from")
    effective_to = form.effective_to or _date(front.get("effective_to"), "effective_to")
    if effective_from and effective_to and effective_to < effective_from:
        msg = "effective_to must not be before effective_from."
        raise UnprocessableContentError(msg)
    return KnowledgeMetadata(
        title=title,
        title_from_content=title_from_content,
        document_key=key,
        category=category,
        version_label=version,
        sensitivity=sensitivity,
        effective_from=effective_from,
        effective_to=effective_to,
        department_name=(front.get("department") or "").strip() or None,
    )
