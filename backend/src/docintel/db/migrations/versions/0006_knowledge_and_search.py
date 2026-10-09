"""Knowledge base and search index: knowledge documents, knowledge and document chunks
(full-text and 768-d vectors), knowledge processing jobs.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORIES = (
    "POLICY",
    "PROCEDURE",
    "CONTRACT_GUIDELINE",
    "FAQ",
    "COMPLIANCE",
    "PUBLIC_REFERENCE",
)
STATUSES = ("PROCESSING", "ACTIVE", "SUPERSEDED", "ARCHIVED", "FAILED")
FORMATS = ("MARKDOWN", "TEXT", "PDF", "PNG", "JPEG", "TIFF")
SENSITIVITIES = ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED")
JOB_TYPES_BEFORE = ("DOCUMENT_PROCESSING",)
JOB_TYPES = ("DOCUMENT_PROCESSING", "KNOWLEDGE_PROCESSING")
DIMENSIONS = 768
SEARCH = (
    "setweight(to_tsvector('english', coalesce(context_prefix, '')), 'A') || "
    "setweight(to_tsvector('english', content), 'B')"
)


def _enum(values: tuple[str, ...], name: str, length: int = 20) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _chunk_columns() -> list[sa.Column[Any]]:
    """Columns shared by knowledge and document chunks."""
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("context_prefix", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "search", postgresql.TSVECTOR(), sa.Computed(SEARCH, persisted=True), nullable=False
        ),
        sa.Column("embedding", Vector(DIMENSIONS), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _chunk_indexes(table: str) -> None:
    # HNSW (cosine) for dense retrieval; rows without a vector are simply not in the index.
    op.create_index(
        f"ix_{table}_embedding",
        table,
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(f"ix_{table}_search", table, ["search"], postgresql_using="gin")


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_key", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("category", _enum(CATEGORIES, "category", 30), nullable=False),
        sa.Column("version_label", sa.String(length=50), nullable=True),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("sensitivity", _enum(SENSITIVITIES, "sensitivity"), nullable=False),
        sa.Column(
            "effective_sensitivity",
            _enum(SENSITIVITIES, "effective_sensitivity"),
            nullable=True,
        ),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("status", _enum(STATUSES, "status"), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("source_format", _enum(FORMATS, "format", 10), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_backend", sa.String(length=10), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("embedding_note", sa.String(length=300), nullable=True),
        sa.Column("processing_error", sa.String(length=1000), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_by_id", sa.Uuid(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "chunk_count >= 0", name=op.f("ck_knowledge_documents_chunk_count_non_negative")
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name=op.f("ck_knowledge_documents_effective_range"),
        ),
        sa.CheckConstraint(
            "size_bytes >= 0", name=op.f("ck_knowledge_documents_size_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_knowledge_documents_department_id_departments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["knowledge_documents.id"],
            name=op.f("fk_knowledge_documents_supersedes_id_knowledge_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"],
            name=op.f("fk_knowledge_documents_uploaded_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_documents")),
    )
    for column in ("department_id", "document_key", "supersedes_id", "uploaded_by_id"):
        op.create_index(op.f(f"ix_knowledge_documents_{column}"), "knowledge_documents", [column])
    op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"])
    op.create_index(
        "uq_knowledge_documents_active_key",
        "knowledge_documents",
        ["document_key"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "knowledge_chunks",
        *_chunk_columns(),
        sa.Column("knowledge_document_id", sa.Uuid(), nullable=False),
        sa.Column("section_path", sa.Text(), nullable=False),
        sa.Column("heading", sa.String(length=300), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        # Copied from the document so filters run in the same scan as the vector index.
        sa.Column("status", _enum(STATUSES, "status"), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("category", _enum(CATEGORIES, "category", 30), nullable=False),
        sa.Column("sensitivity", _enum(SENSITIVITIES, "sensitivity"), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "token_count >= 0", name=op.f("ck_knowledge_chunks_token_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_knowledge_chunks_department_id_departments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_document_id"],
            ["knowledge_documents.id"],
            name=op.f("fk_knowledge_chunks_knowledge_document_id_knowledge_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_chunks")),
        sa.UniqueConstraint(
            "knowledge_document_id", "chunk_index", name="uq_knowledge_chunks_position"
        ),
    )
    _chunk_indexes("knowledge_chunks")
    op.create_index(
        op.f("ix_knowledge_chunks_knowledge_document_id"),
        "knowledge_chunks",
        ["knowledge_document_id"],
    )
    op.create_index("ix_knowledge_chunks_scope", "knowledge_chunks", ["status", "department_id"])

    op.create_table(
        "document_chunks",
        *_chunk_columns(),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "token_count >= 0", name=op.f("ck_document_chunks_token_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_document_chunks_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint(
            "document_version_id", "chunk_index", name="uq_document_chunks_position"
        ),
    )
    _chunk_indexes("document_chunks")
    op.create_index(op.f("ix_document_chunks_document_id"), "document_chunks", ["document_id"])
    op.create_index(
        op.f("ix_document_chunks_document_version_id"), "document_chunks", ["document_version_id"]
    )

    # Knowledge documents are processed by the same job queue.
    op.add_column("processing_jobs", sa.Column("knowledge_document_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_processing_jobs_knowledge_document_id_knowledge_documents"),
        "processing_jobs",
        "knowledge_documents",
        ["knowledge_document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "uq_processing_jobs_active_knowledge",
        "processing_jobs",
        ["knowledge_document_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'PROCESSING')"),
    )
    _replace_job_type_check(JOB_TYPES)


def _replace_job_type_check(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_processing_jobs_job_type"), "processing_jobs", type_="check")
    allowed = ", ".join(f"'{value}'" for value in values)
    op.create_check_constraint(
        op.f("ck_processing_jobs_job_type"), "processing_jobs", f"job_type IN ({allowed})"
    )


def downgrade() -> None:
    op.execute("DELETE FROM processing_jobs WHERE job_type = 'KNOWLEDGE_PROCESSING'")
    _replace_job_type_check(JOB_TYPES_BEFORE)
    op.drop_index("uq_processing_jobs_active_knowledge", table_name="processing_jobs")
    op.drop_constraint(
        op.f("fk_processing_jobs_knowledge_document_id_knowledge_documents"),
        "processing_jobs",
        type_="foreignkey",
    )
    op.drop_column("processing_jobs", "knowledge_document_id")
    op.drop_table("document_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
