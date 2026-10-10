"""Recorded evaluation runs: one row per suite report, recorded by a run or imported.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("suite", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("quick", sa.Boolean(), nullable=False),
        sa.Column("git_revision", sa.String(length=64), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dataset", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("environment", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tables", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("report_markdown", sa.Text(), nullable=False),
        sa.Column("report_sha256", sa.String(length=64), nullable=False),
        sa.Column("gates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "source",
            sa.Enum(
                "RUN", "IMPORT", name="source", native_enum=False, create_constraint=True, length=20
            ),
            nullable=False,
        ),
        sa.Column("recorded_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("suite ~ '^[a-z][a-z_]{0,39}$'", name=op.f("ck_evaluations_suite_name")),
        sa.CheckConstraint(
            "report_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_evaluations_sha256_hex")
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_id"],
            ["users.id"],
            name=op.f("fk_evaluations_recorded_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluations")),
        sa.UniqueConstraint("suite", "report_sha256", name="uq_evaluations_report"),
    )
    op.create_index("ix_evaluations_suite_run_at", "evaluations", ["suite", "run_at"])


def downgrade() -> None:
    op.drop_index("ix_evaluations_suite_run_at", table_name="evaluations")
    op.drop_table("evaluations")
