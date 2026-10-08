"""Structured extraction results (Phase 4): vendors, extractions, field provenance, LLM calls."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ARRAY,
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

from docintel.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from docintel.db.models.identity import User
from docintel.db.models.types import str_enum


class ExtractionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"  # every required field found
    PARTIAL = "PARTIAL"  # some required fields missing
    FAILED = "FAILED"  # nothing usable extracted


class ExtractionMethodUsed(StrEnum):
    LOCAL = "LOCAL"  # layout extractor only
    LLM = "LLM"  # model only (layout extractor found nothing)
    COMBINED = "COMBINED"  # both, merged field by field


class ReviewLevelValue(StrEnum):
    AUTO = "AUTO"
    ANALYST_REVIEW = "ANALYST_REVIEW"
    MANDATORY_REVIEW = "MANDATORY_REVIEW"


class EvidenceStatusValue(StrEnum):
    VERIFIED = "VERIFIED"
    FUZZY = "FUZZY"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_FOUND = "NOT_FOUND"
    HUMAN = "HUMAN"


class FieldOrigin(StrEnum):
    LOCAL = "LOCAL"
    LLM = "LLM"
    BOTH = "BOTH"
    DERIVED = "DERIVED"
    HUMAN = "HUMAN"


class LLMCallStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


VALUE_TYPES = (
    "TEXT",
    "IDENTIFIER",
    "ORGANIZATION",
    "PERSON",
    "DATE",
    "MONEY",
    "CURRENCY",
    "PERCENT",
    "QUANTITY",
    "INTEGER",
    "DAYS",
    "EMAIL",
    "PHONE",
    "BOOLEAN",
)


class Vendor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Vendor master data: the canonical names printed vendor names are normalized against."""

    __tablename__ = "vendors"
    __table_args__ = (
        Index(
            "ix_vendors_name_key_trgm",
            "name_key",
            postgresql_using="gin",
            postgresql_ops={"name_key": "gin_trgm_ops"},
        ),
        Index("ix_vendors_alias_keys", "alias_keys", postgresql_using="gin"),
        Index("ix_vendors_tax_id_key", "tax_id_key"),
        Index("ix_vendors_created_by_id", "created_by_id"),
        CheckConstraint(
            "default_currency IS NULL OR default_currency ~ '^[A-Z]{3}$'", name="currency_code"
        ),
        CheckConstraint(
            "payment_terms_days IS NULL OR payment_terms_days >= 0", name="payment_terms"
        ),
    )

    canonical_name: Mapped[str] = mapped_column(String(300), unique=True)
    # organization_key(canonical_name): casefolded, legal suffixes removed.
    name_key: Mapped[str] = mapped_column(String(300))
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(300)), default=list, server_default=text("'{}'")
    )
    alias_keys: Mapped[list[str]] = mapped_column(
        ARRAY(String(300)), default=list, server_default=text("'{}'")
    )
    tax_id: Mapped[str | None] = mapped_column(String(64))
    tax_id_key: Mapped[str | None] = mapped_column(String(64))
    default_currency: Mapped[str | None] = mapped_column(String(3))
    payment_terms_days: Mapped[int | None]
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class DocumentExtraction(UUIDPrimaryKeyMixin, Base):
    """One extraction run for a document version. Exactly one per document is current."""

    __tablename__ = "document_extractions"
    __table_args__ = (
        Index(
            "uq_document_extractions_current",
            "document_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_document_extractions_document_created", "document_id", "created_at"),
        Index("ix_document_extractions_version", "document_version_id"),
        Index(
            "ix_document_extractions_input_hash",
            "input_hash",
            postgresql_where=text("llm_output IS NOT NULL"),
        ),
        CheckConstraint(
            "overall_confidence >= 0 AND overall_confidence <= 1", name="confidence_range"
        ),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    schema_name: Mapped[str] = mapped_column(String(40))
    schema_version: Mapped[int]
    status: Mapped[ExtractionStatus] = mapped_column(str_enum(ExtractionStatus, "status"))
    method: Mapped[ExtractionMethodUsed] = mapped_column(str_enum(ExtractionMethodUsed, "method"))
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    # sha256 of everything the model saw (prompt, schema, model, images): the cache key.
    input_hash: Mapped[str | None] = mapped_column(String(64))
    # The model's validated output, kept so an unchanged input never costs a second call.
    llm_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB)
    normalized_output: Mapped[dict[str, Any]] = mapped_column(JSONB)
    checks: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    validation_errors: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    signals: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    overall_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    review_level: Mapped[ReviewLevelValue] = mapped_column(
        str_enum(ReviewLevelValue, "review_level", length=20)
    )
    is_current: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )

    fields: Mapped[list[ExtractedField]] = relationship(
        order_by="ExtractedField.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )


class ExtractedField(UUIDPrimaryKeyMixin, Base):
    """A field's value with provenance (page, quote, box), confidence and any human correction."""

    __tablename__ = "extracted_fields"
    __table_args__ = (
        UniqueConstraint("extraction_id", "field_path", name="uq_extracted_fields_path"),
        Index("ix_extracted_fields_corrected_by_id", "corrected_by_id"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint(
            "value_type IN (" + ", ".join(f"'{value}'" for value in VALUE_TYPES) + ")",
            name="value_type",
        ),
        CheckConstraint("page_number IS NULL OR page_number >= 1", name="page_number_positive"),
    )

    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_extractions.id", ondelete="CASCADE")
    )
    position: Mapped[int]  # display order: header fields, then rows, then lists
    field_path: Mapped[str] = mapped_column(String(120))  # "total", "line_items[2].quantity"
    field_name: Mapped[str] = mapped_column(String(60))
    group_name: Mapped[str | None] = mapped_column(String(40))  # "line_items", "skills"
    row_index: Mapped[int | None]
    value_type: Mapped[str] = mapped_column(String(20))
    is_required: Mapped[bool]
    original_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    page_number: Mapped[int | None]
    source_text: Mapped[str | None] = mapped_column(Text)
    bbox: Mapped[list[float] | None] = mapped_column(JSONB)
    evidence_status: Mapped[EvidenceStatusValue] = mapped_column(
        str_enum(EvidenceStatusValue, "evidence_status")
    )
    origin: Mapped[FieldOrigin | None] = mapped_column(str_enum(FieldOrigin, "origin"))
    method: Mapped[str | None] = mapped_column(String(300))
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    confidence_signals: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Values the other extractor proposed when the two disagreed (shown to reviewers).
    alternatives: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    corrected_value: Mapped[str | None] = mapped_column(Text)
    corrected_normalized: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correction_note: Mapped[str | None] = mapped_column(String(500))
    corrected_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    corrected_at: Mapped[datetime | None]

    corrected_by: Mapped[User | None] = relationship(lazy="joined")


class LLMCall(UUIDPrimaryKeyMixin, Base):
    """AI observability: one row per model call. No prompt or completion content is stored."""

    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("ix_llm_calls_provider_created", "provider", "created_at"),
        Index("ix_llm_calls_document_id", "document_id"),
        CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0", name="cost_non_negative"
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(100))
    purpose: Mapped[str] = mapped_column(String(60))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[LLMCallStatus] = mapped_column(str_enum(LLMCallStatus, "status"))
    error_code: Mapped[str | None] = mapped_column(String(60))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    thinking_tokens: Mapped[int | None]
    latency_ms: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
