"""Structured extraction: vendors, document extractions, field provenance, LLM call accounting.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTRACTION_STATUSES = ("SUCCEEDED", "PARTIAL", "FAILED")
EXTRACTION_METHODS = ("LOCAL", "LLM", "COMBINED")
REVIEW_LEVELS = ("AUTO", "ANALYST_REVIEW", "MANDATORY_REVIEW")
EVIDENCE_STATUSES = ("VERIFIED", "FUZZY", "UNSUPPORTED", "NOT_FOUND", "HUMAN")
ORIGINS = ("LOCAL", "LLM", "BOTH", "DERIVED", "HUMAN")
CALL_STATUSES = ("SUCCEEDED", "FAILED")
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


def _enum(values: tuple[str, ...], name: str, length: int = 20) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _timestamp(name: str, default: Any) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=False)


def upgrade() -> None:
    # Trigram similarity for vendor-name candidates (trusted extension since PostgreSQL 13).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "vendors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_name", sa.String(length=300), nullable=False),
        sa.Column("name_key", sa.String(length=300), nullable=False),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.String(length=300)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "alias_keys",
            postgresql.ARRAY(sa.String(length=300)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("tax_id", sa.String(length=64), nullable=True),
        sa.Column("tax_id_key", sa.String(length=64), nullable=True),
        sa.Column("default_currency", sa.String(length=3), nullable=True),
        sa.Column("payment_terms_days", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        _timestamp("created_at", sa.func.now()),
        _timestamp("updated_at", sa.func.now()),
        sa.CheckConstraint(
            "default_currency IS NULL OR default_currency ~ '^[A-Z]{3}$'",
            name=op.f("ck_vendors_currency_code"),
        ),
        sa.CheckConstraint(
            "payment_terms_days IS NULL OR payment_terms_days >= 0",
            name=op.f("ck_vendors_payment_terms"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_vendors_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendors")),
        sa.UniqueConstraint("canonical_name", name=op.f("uq_vendors_canonical_name")),
    )
    op.create_index(
        op.f("ix_vendors_name_key_trgm"),
        "vendors",
        ["name_key"],
        postgresql_using="gin",
        postgresql_ops={"name_key": "gin_trgm_ops"},
    )
    op.create_index(
        op.f("ix_vendors_alias_keys"), "vendors", ["alias_keys"], postgresql_using="gin"
    )
    op.create_index(op.f("ix_vendors_tax_id_key"), "vendors", ["tax_id_key"])
    op.create_index(op.f("ix_vendors_created_by_id"), "vendors", ["created_by_id"])

    op.add_column("documents", sa.Column("vendor_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_documents_vendor_id_vendors"),
        "documents",
        "vendors",
        ["vendor_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_documents_vendor_id"), "documents", ["vendor_id"])

    op.create_table(
        "document_extractions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("schema_name", sa.String(length=40), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", _enum(EXTRACTION_STATUSES, "status"), nullable=False),
        sa.Column("method", _enum(EXTRACTION_METHODS, "method"), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.String(length=40), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("llm_output", _jsonb(), nullable=True),
        sa.Column("output", _jsonb(), nullable=False),
        sa.Column("normalized_output", _jsonb(), nullable=False),
        sa.Column("checks", _jsonb(), nullable=False),
        sa.Column("validation_errors", _jsonb(), nullable=False),
        sa.Column("signals", _jsonb(), nullable=False),
        sa.Column("overall_confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("review_level", _enum(REVIEW_LEVELS, "review_level"), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        _timestamp("created_at", sa.func.clock_timestamp()),
        sa.CheckConstraint(
            "overall_confidence >= 0 AND overall_confidence <= 1",
            name=op.f("ck_document_extractions_confidence_range"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name=op.f("ck_document_extractions_schema_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_extractions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_document_extractions_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_extractions")),
    )
    op.create_index(
        op.f("uq_document_extractions_current"),
        "document_extractions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        op.f("ix_document_extractions_document_created"),
        "document_extractions",
        ["document_id", "created_at"],
    )
    op.create_index(
        op.f("ix_document_extractions_version"), "document_extractions", ["document_version_id"]
    )
    op.create_index(
        op.f("ix_document_extractions_input_hash"),
        "document_extractions",
        ["input_hash"],
        postgresql_where=sa.text("llm_output IS NOT NULL"),
    )

    op.create_table(
        "extracted_fields",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("field_path", sa.String(length=120), nullable=False),
        sa.Column("field_name", sa.String(length=60), nullable=False),
        sa.Column("group_name", sa.String(length=40), nullable=True),
        sa.Column("row_index", sa.Integer(), nullable=True),
        sa.Column("value_type", sa.String(length=20), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("original_value", sa.Text(), nullable=True),
        sa.Column("normalized_value", _jsonb(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.Column("bbox", _jsonb(), nullable=True),
        sa.Column("evidence_status", _enum(EVIDENCE_STATUSES, "evidence_status"), nullable=False),
        sa.Column("origin", _enum(ORIGINS, "origin"), nullable=True),
        sa.Column("method", sa.String(length=300), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("confidence_signals", _jsonb(), nullable=False),
        sa.Column("alternatives", _jsonb(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("corrected_value", sa.Text(), nullable=True),
        sa.Column("corrected_normalized", _jsonb(), nullable=True),
        sa.Column("correction_note", sa.String(length=500), nullable=True),
        sa.Column("corrected_by_id", sa.Uuid(), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_extracted_fields_confidence_range"),
        ),
        sa.CheckConstraint(
            "value_type IN (" + ", ".join(f"'{value}'" for value in VALUE_TYPES) + ")",
            name=op.f("ck_extracted_fields_value_type"),
        ),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number >= 1",
            name=op.f("ck_extracted_fields_page_number_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["document_extractions.id"],
            name=op.f("fk_extracted_fields_extraction_id_document_extractions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["corrected_by_id"],
            ["users.id"],
            name=op.f("fk_extracted_fields_corrected_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extracted_fields")),
        sa.UniqueConstraint("extraction_id", "field_path", name=op.f("uq_extracted_fields_path")),
    )
    op.create_index(
        op.f("ix_extracted_fields_corrected_by_id"), "extracted_fields", ["corrected_by_id"]
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        _timestamp("created_at", sa.func.clock_timestamp()),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("purpose", sa.String(length=60), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("prompt_version", sa.String(length=40), nullable=True),
        sa.Column("status", _enum(CALL_STATUSES, "status"), nullable=False),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("thinking_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Numeric(12, 2), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name=op.f("ck_llm_calls_cost_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_llm_calls_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index(op.f("ix_llm_calls_provider_created"), "llm_calls", ["provider", "created_at"])
    op.create_index(op.f("ix_llm_calls_document_id"), "llm_calls", ["document_id"])


def downgrade() -> None:
    op.drop_table("llm_calls")
    op.drop_table("extracted_fields")
    op.drop_table("document_extractions")
    op.drop_index(op.f("ix_documents_vendor_id"), table_name="documents")
    op.drop_constraint(op.f("fk_documents_vendor_id_vendors"), "documents", type_="foreignkey")
    op.drop_column("documents", "vendor_id")
    op.drop_table("vendors")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
