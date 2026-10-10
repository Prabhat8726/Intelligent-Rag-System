"""Request rate limit counters shared by the API replicas (UNLOGGED).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_counters",
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.CheckConstraint("hits >= 1", name=op.f("ck_rate_limit_counters_hits_positive")),
        sa.PrimaryKeyConstraint("key", "window_start", name=op.f("pk_rate_limit_counters")),
        prefixes=["UNLOGGED"],
    )
    op.create_index(
        "ix_rate_limit_counters_expires_at", "rate_limit_counters", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_counters_expires_at", table_name="rate_limit_counters")
    op.drop_table("rate_limit_counters")
