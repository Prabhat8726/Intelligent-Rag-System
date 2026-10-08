"""Documents, immutable document versions and the PostgreSQL job queue.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOCUMENT_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "REVIEW_REQUIRED")
DOCUMENT_TYPES = (
    "INVOICE",
    "PURCHASE_ORDER",
    "CONTRACT",
    "RECEIPT",
    "DELIVERY_NOTE",
    "RESUME",
    "BANK_STATEMENT",
    "POLICY",
    "OTHER",
)
SENSITIVITIES = ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED")
SOURCES = ("UPLOAD", "API", "SYNTHETIC")
STORAGE_BACKENDS = ("local", "s3")
JOB_TYPES = ("DOCUMENT_PROCESSING",)
JOB_STATUSES = ("QUEUED", "PROCESSING", "COMPLETED", "FAILED", "CANCELLED")


def _enum(values: tuple[str, ...], name: str, length: int = 20) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_filename", sa.String(length=255), nullable=False),
        sa.Column("document_type", _enum(DOCUMENT_TYPES, "document_type", 30), nullable=True),
        sa.Column("type_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("status", _enum(DOCUMENT_STATUSES, "status"), nullable=False),
        sa.Column("sensitivity", _enum(SENSITIVITIES, "sensitivity"), nullable=False),
        sa.Column("source", _enum(SOURCES, "source"), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("duplicate_of_id", sa.Uuid(), nullable=True),
        sa.Column("duplicate_reason", sa.String(length=50), nullable=True),
        sa.Column("processing_error", sa.String(length=500), nullable=True),
        sa.Column("last_processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "type_confidence IS NULL OR (type_confidence >= 0 AND type_confidence <= 1)",
            name=op.f("ck_documents_type_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_documents_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_documents_department_id_departments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_id"],
            ["documents.id"],
            name=op.f("fk_documents_duplicate_of_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by_id"],
            ["users.id"],
            name=op.f("fk_documents_deleted_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(
        op.f("ix_documents_department_status_created"),
        "documents",
        ["department_id", "status", sa.text("created_at DESC")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(op.f("ix_documents_owner_id"), "documents", ["owner_id"])
    op.create_index(op.f("ix_documents_document_type"), "documents", ["document_type"])
    op.create_index(op.f("ix_documents_duplicate_of_id"), "documents", ["duplicate_of_id"])

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "storage_backend", _enum(STORAGE_BACKENDS, "storage_backend", 10), nullable=False
        ),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("file_kind", sa.String(length=10), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("inspection", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("uploaded_by_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "version_number >= 1", name=op.f("ck_document_versions_version_number_positive")
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_document_versions_size_positive")),
        sa.CheckConstraint(
            "page_count >= 1", name=op.f("ck_document_versions_page_count_positive")
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_document_versions_sha256_hex")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_versions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"],
            name=op.f("fk_document_versions_uploaded_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "document_id", "version_number", name=op.f("uq_document_versions_number")
        ),
        sa.UniqueConstraint("storage_key", name=op.f("uq_document_versions_storage_key")),
    )
    op.create_index(op.f("ix_document_versions_sha256"), "document_versions", ["sha256"])

    # Circular reference documents.current_version_id -> document_versions.id,
    # added once both tables exist.
    op.create_foreign_key(
        op.f("fk_documents_current_version_id_document_versions"),
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_type", _enum(JOB_TYPES, "job_type", 40), nullable=False),
        sa.Column("status", _enum(JOB_STATUSES, "status"), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("document_version_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "run_after",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column("locked_by", sa.String(length=100), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stage", sa.String(length=50), nullable=True),
        sa.Column(
            "stage_timings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_processing_jobs_attempts_non_negative")),
        sa.CheckConstraint(
            "max_attempts >= 1", name=op.f("ck_processing_jobs_max_attempts_positive")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_processing_jobs_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_processing_jobs_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name=op.f("fk_processing_jobs_requested_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_jobs")),
    )
    op.create_index(
        op.f("ix_processing_jobs_claimable"),
        "processing_jobs",
        [sa.text("priority DESC"), "run_after"],
        postgresql_where=sa.text("status = 'QUEUED'"),
    )
    op.create_index(
        op.f("ix_processing_jobs_lease"),
        "processing_jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.create_index(op.f("ix_processing_jobs_document_id"), "processing_jobs", ["document_id"])
    op.create_index(
        op.f("uq_processing_jobs_active_version"),
        "processing_jobs",
        ["job_type", "document_version_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'PROCESSING')"),
    )


def downgrade() -> None:
    op.drop_table("processing_jobs")
    op.drop_constraint(
        op.f("fk_documents_current_version_id_document_versions"), "documents", type_="foreignkey"
    )
    op.drop_table("document_versions")
    op.drop_table("documents")
