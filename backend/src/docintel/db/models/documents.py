"""Documents, immutable file versions and the processing job queue (Phase 2)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docintel.core.config import StorageBackendName
from docintel.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from docintel.db.models.identity import Department, User
from docintel.db.models.types import str_enum

if TYPE_CHECKING:
    from docintel.db.models.extraction import Vendor


class DocumentStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class DocumentType(StrEnum):
    INVOICE = "INVOICE"
    PURCHASE_ORDER = "PURCHASE_ORDER"
    CONTRACT = "CONTRACT"
    RECEIPT = "RECEIPT"
    DELIVERY_NOTE = "DELIVERY_NOTE"
    RESUME = "RESUME"
    BANK_STATEMENT = "BANK_STATEMENT"
    POLICY = "POLICY"
    OTHER = "OTHER"


class Sensitivity(StrEnum):
    """Ordered from least to most sensitive; gates external AI processing (ADR-007)."""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class DocumentSource(StrEnum):
    UPLOAD = "UPLOAD"
    API = "API"
    SYNTHETIC = "SYNTHETIC"


class JobType(StrEnum):
    DOCUMENT_PROCESSING = "DOCUMENT_PROCESSING"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index(
            "ix_documents_department_status_created",
            "department_id",
            "status",
            text("created_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_documents_owner_id", "owner_id"),
        Index("ix_documents_document_type", "document_type"),
        Index("ix_documents_duplicate_of_id", "duplicate_of_id"),
        Index("ix_documents_vendor_id", "vendor_id"),
        # Matching (Phase 5): related documents by order reference and duplicates by number.
        Index(
            "ix_documents_department_po_key",
            "department_id",
            "po_key",
            postgresql_where=text("deleted_at IS NULL AND po_key IS NOT NULL"),
        ),
        Index(
            "ix_documents_department_number_key",
            "department_id",
            "number_key",
            postgresql_where=text("deleted_at IS NULL AND number_key IS NOT NULL"),
        ),
        CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="currency_code"),
        CheckConstraint(
            "type_confidence IS NULL OR (type_confidence >= 0 AND type_confidence <= 1)",
            name="type_confidence_range",
        ),
    )

    display_filename: Mapped[str] = mapped_column(String(255))
    document_type: Mapped[DocumentType | None] = mapped_column(
        str_enum(DocumentType, "document_type", length=30)
    )
    type_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    status: Mapped[DocumentStatus] = mapped_column(
        str_enum(DocumentStatus, "status"), default=DocumentStatus.PENDING
    )
    sensitivity: Mapped[Sensitivity] = mapped_column(
        str_enum(Sensitivity, "sensitivity"), default=Sensitivity.INTERNAL
    )
    source: Mapped[DocumentSource] = mapped_column(
        str_enum(DocumentSource, "source"), default=DocumentSource.UPLOAD
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT")
    )
    # use_alter: documents <-> document_versions reference each other.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "document_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_documents_current_version_id_document_versions",
        )
    )
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    duplicate_reason: Mapped[str | None] = mapped_column(String(50))
    # Vendor master entry the extracted vendor name resolved to (Phase 4).
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="SET NULL")
    )
    # Key facts of the current extraction, kept in step with it (and with corrections), so
    # matching finds related documents and duplicates by index (Phase 5).
    number_key: Mapped[str | None] = mapped_column(String(100))
    po_key: Mapped[str | None] = mapped_column(String(100))
    document_date: Mapped[date | None]
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    vendor_key: Mapped[str | None] = mapped_column(String(300))
    processing_error: Mapped[str | None] = mapped_column(String(500))
    # ReviewReason codes: what processing and the rules found. Whether a person still has to
    # look is the open review task (status REVIEW_REQUIRED), see docintel.review.
    review_reasons: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    last_processed_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    deleted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )

    owner: Mapped[User] = relationship(foreign_keys=[owner_id], lazy="joined")
    department: Mapped[Department | None] = relationship(lazy="joined")
    current_version: Mapped[DocumentVersion | None] = relationship(
        foreign_keys=[current_version_id], post_update=True, lazy="joined"
    )
    vendor: Mapped[Vendor | None] = relationship(lazy="joined")


class DocumentVersion(UUIDPrimaryKeyMixin, Base):
    """An immutable uploaded file. New uploads of a document create new versions."""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_versions_number"),
        Index("ix_document_versions_sha256", "sha256"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("page_count >= 1", name="page_count_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    version_number: Mapped[int]
    storage_backend: Mapped[StorageBackendName] = mapped_column(
        str_enum(StorageBackendName, "storage_backend", length=10)
    )
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    file_kind: Mapped[str] = mapped_column(String(10))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int]
    # Written by the worker's inspection stage: per-page text-layer / OCR-need analysis.
    inspection: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Content findings (card numbers, ...) and the sensitivity they imply (external-AI gate).
    sensitivity_assessment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    uploaded_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class ProcessingJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """PostgreSQL-backed job queue entry (ADR-002). Claimed with FOR UPDATE SKIP LOCKED."""

    __tablename__ = "processing_jobs"
    __table_args__ = (
        Index(
            "ix_processing_jobs_claimable",
            text("priority DESC"),
            "run_after",
            postgresql_where=text("status = 'QUEUED'"),
        ),
        Index(
            "ix_processing_jobs_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'PROCESSING'"),
        ),
        Index("ix_processing_jobs_document_id", "document_id"),
        # At most one active job per (type, document version): re-process requests can't pile up.
        Index(
            "uq_processing_jobs_active_version",
            "job_type",
            "document_version_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'PROCESSING')"),
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
    )

    job_type: Mapped[JobType] = mapped_column(str_enum(JobType, "job_type", length=40))
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, "status"), default=JobStatus.QUEUED
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    priority: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    max_attempts: Mapped[int]
    # clock_timestamp(), not now(): now() is the transaction start, so jobs inserted in one
    # transaction would tie. Wall-clock insertion time gives a true FIFO order.
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )
    run_after: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[datetime | None]
    lease_expires_at: Mapped[datetime | None]
    stage: Mapped[str | None] = mapped_column(String(50))
    stage_timings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    last_error: Mapped[str | None] = mapped_column(String(1000))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
