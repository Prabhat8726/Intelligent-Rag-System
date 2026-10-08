"""Document understanding: pages, tables, classifications, review reasons, sensitivity findings.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
EXTRACTION_METHODS = ("NATIVE", "OCR")
TABLE_METHODS = ("NATIVE", "OCR", "MIXED")
CLASSIFICATION_METHODS = ("LOCAL_MODEL", "LLM", "ENSEMBLE", "HUMAN")


def _enum(values: tuple[str, ...], name: str, length: int) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _created_at(default: Any) -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=default, nullable=False
    )


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "review_reasons", _jsonb(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    )
    op.add_column("document_versions", sa.Column("sensitivity_assessment", _jsonb(), nullable=True))

    op.create_table(
        "document_pages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("width", sa.Float(), nullable=False),
        sa.Column("height", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=2), nullable=False),
        sa.Column("rotation_applied", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "extraction_method", _enum(EXTRACTION_METHODS, "extraction_method", 10), nullable=False
        ),
        sa.Column("ocr_confidence", sa.Numeric(5, 2), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("words", _jsonb(), nullable=False),
        sa.Column("layout", _jsonb(), nullable=False),
        sa.Column("preview_storage_key", sa.String(length=512), nullable=True),
        sa.Column("preview_width", sa.Integer(), nullable=True),
        sa.Column("preview_height", sa.Integer(), nullable=True),
        _created_at(sa.func.now()),
        sa.CheckConstraint("page_number >= 1", name=op.f("ck_document_pages_page_number_positive")),
        sa.CheckConstraint("unit IN ('pt', 'px')", name=op.f("ck_document_pages_unit")),
        sa.CheckConstraint(
            "rotation_applied IN (0, 90, 180, 270)", name=op.f("ck_document_pages_rotation_applied")
        ),
        sa.CheckConstraint(
            "ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 100)",
            name=op.f("ck_document_pages_ocr_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_document_pages_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_pages")),
        sa.UniqueConstraint(
            "document_version_id", "page_number", name=op.f("uq_document_pages_number")
        ),
    )

    op.create_table(
        "document_tables",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("table_index", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("header", _jsonb(), nullable=False),
        sa.Column("bbox", _jsonb(), nullable=False),
        sa.Column(
            "extraction_method", _enum(TABLE_METHODS, "extraction_method", 10), nullable=False
        ),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        _created_at(sa.func.now()),
        sa.CheckConstraint("page_end >= page_start", name=op.f("ck_document_tables_page_range")),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_document_tables_confidence_range")
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_document_tables_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_tables")),
        sa.UniqueConstraint(
            "document_version_id", "table_index", name=op.f("uq_document_tables_index")
        ),
    )

    op.create_table(
        "table_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("cells", _jsonb(), nullable=False),
        sa.Column("bbox", _jsonb(), nullable=False),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["document_tables.id"],
            name=op.f("fk_table_rows_table_id_document_tables"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_table_rows")),
        sa.UniqueConstraint("table_id", "row_index", name=op.f("uq_table_rows_index")),
    )

    op.create_table(
        "document_classifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("label", _enum(DOCUMENT_TYPES, "label", 30), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("method", _enum(CLASSIFICATION_METHODS, "method", 20), nullable=False),
        sa.Column("model_version", sa.String(length=100), nullable=True),
        sa.Column("signals", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        _created_at(sa.func.clock_timestamp()),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_document_classifications_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_classifications_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_document_classifications_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_document_classifications_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_classifications")),
    )
    op.create_index(
        op.f("uq_document_classifications_current"),
        "document_classifications",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        op.f("ix_document_classifications_document_created"),
        "document_classifications",
        ["document_id", "created_at"],
    )
    op.create_index(
        op.f("ix_document_classifications_version"),
        "document_classifications",
        ["document_version_id"],
    )
    op.create_index(
        op.f("ix_document_classifications_created_by"),
        "document_classifications",
        ["created_by_id"],
    )


def downgrade() -> None:
    op.drop_table("document_classifications")
    op.drop_table("table_rows")
    op.drop_table("document_tables")
    op.drop_table("document_pages")
    op.drop_column("document_versions", "sensitivity_assessment")
    op.drop_column("documents", "review_reasons")
