"""Document understanding results (Phase 3): pages, tables, classifications."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docintel.db.base import Base, UUIDPrimaryKeyMixin
from docintel.db.models.documents import DocumentType
from docintel.db.models.identity import User
from docintel.db.models.types import str_enum


class ExtractionMethod(StrEnum):
    NATIVE = "NATIVE"  # PDF text layer
    OCR = "OCR"


class TableMethod(StrEnum):
    NATIVE = "NATIVE"
    OCR = "OCR"
    MIXED = "MIXED"  # stitched across native and OCR pages


class ClassificationMethod(StrEnum):
    LOCAL_MODEL = "LOCAL_MODEL"
    LLM = "LLM"
    ENSEMBLE = "ENSEMBLE"  # LLM confirmed the local model's label
    HUMAN = "HUMAN"


class ReviewReason(StrEnum):
    """Why a processed document needs a human (documents.review_reasons)."""

    NO_TEXT_FOUND = "NO_TEXT_FOUND"
    CLASSIFICATION_UNCERTAIN = "CLASSIFICATION_UNCERTAIN"
    LOW_OCR_CONFIDENCE = "LOW_OCR_CONFIDENCE"
    OCR_FAILED = "OCR_FAILED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    MISSING_REQUIRED_FIELDS = "MISSING_REQUIRED_FIELDS"
    EXTRACTION_UNCERTAIN = "EXTRACTION_UNCERTAIN"
    EXTRACTION_INCONSISTENT = "EXTRACTION_INCONSISTENT"


class DocumentPage(UUIDPrimaryKeyMixin, Base):
    """Normalized content of one page of one document version."""

    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("document_version_id", "page_number", name="uq_document_pages_number"),
        CheckConstraint("page_number >= 1", name="page_number_positive"),
        CheckConstraint("unit IN ('pt', 'px')", name="unit"),
        CheckConstraint("rotation_applied IN (0, 90, 180, 270)", name="rotation_applied"),
        CheckConstraint(
            "ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 100)",
            name="ocr_confidence_range",
        ),
    )

    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    page_number: Mapped[int]
    width: Mapped[float]
    height: Mapped[float]
    unit: Mapped[str] = mapped_column(String(2))
    rotation_applied: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    extraction_method: Mapped[ExtractionMethod] = mapped_column(
        str_enum(ExtractionMethod, "extraction_method", length=10)
    )
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    word_count: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    # [[text, x0, y0, x1, y1, ocr_confidence | null, size], ...] in page units, top-left origin.
    words: Mapped[list[Any]] = mapped_column(JSONB)
    # {"version", "lines": [...], "blocks": [...], "warnings": [...]}
    layout: Mapped[dict[str, Any]] = mapped_column(JSONB)
    preview_storage_key: Mapped[str | None] = mapped_column(String(512))
    preview_width: Mapped[int | None]
    preview_height: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class DocumentTable(UUIDPrimaryKeyMixin, Base):
    """A detected table, stitched across pages when it continues under the same header."""

    __tablename__ = "document_tables"
    __table_args__ = (
        UniqueConstraint("document_version_id", "table_index", name="uq_document_tables_index"),
        CheckConstraint("page_end >= page_start", name="page_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    table_index: Mapped[int]
    page_start: Mapped[int]
    page_end: Mapped[int]
    header: Mapped[list[str]] = mapped_column(JSONB)
    bbox: Mapped[list[float]] = mapped_column(JSONB)  # on page_start
    extraction_method: Mapped[TableMethod] = mapped_column(
        str_enum(TableMethod, "extraction_method", length=10)
    )
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    row_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    rows: Mapped[list[TableRow]] = relationship(
        order_by="TableRow.row_index",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )


class TableRow(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "table_rows"
    __table_args__ = (UniqueConstraint("table_id", "row_index", name="uq_table_rows_index"),)

    table_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_tables.id", ondelete="CASCADE")
    )
    row_index: Mapped[int]
    page_number: Mapped[int]
    cells: Mapped[list[str]] = mapped_column(JSONB)
    bbox: Mapped[list[float]] = mapped_column(JSONB)


class DocumentClassification(UUIDPrimaryKeyMixin, Base):
    """Classification history. Exactly one row per document is current; human corrections are
    rows with method HUMAN and become training data for the local model."""

    __tablename__ = "document_classifications"
    __table_args__ = (
        Index(
            "uq_document_classifications_current",
            "document_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_document_classifications_document_created", "document_id", "created_at"),
        Index("ix_document_classifications_version", "document_version_id"),
        Index("ix_document_classifications_created_by", "created_by_id"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    label: Mapped[DocumentType] = mapped_column(str_enum(DocumentType, "label", length=30))
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    method: Mapped[ClassificationMethod] = mapped_column(
        str_enum(ClassificationMethod, "method", length=20)
    )
    model_version: Mapped[str | None] = mapped_column(String(100))
    signals: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    note: Mapped[str | None] = mapped_column(String(500))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    is_current: Mapped[bool]
    # clock_timestamp(): several rows can be written in one transaction (history order).
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )

    created_by: Mapped[User | None] = relationship(lazy="joined")
