"""Identity and audit foundation: vector extension, departments, users, append-only audit_logs.

Revision ID: 0001
Revises:
Create Date: 2026-10-08

Note: `CREATE EXTENSION vector` needs a role allowed to create extensions (pgvector is not a
"trusted" extension). The docker-compose database user is a superuser; on managed Postgres,
enable pgvector through the provider's mechanism before running migrations.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ("ADMIN", "MANAGER", "ANALYST", "REVIEWER", "VIEWER")
ACTOR_TYPES = ("USER", "SYSTEM", "AGENT", "WORKER", "ANONYMOUS")
OUTCOMES = ("SUCCESS", "FAILURE", "DENIED")


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
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "departments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_departments")),
        sa.UniqueConstraint("name", name=op.f("uq_departments_name")),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.Enum(*ROLES, name="role", native_enum=False, create_constraint=True, length=20),
            nullable=False,
        ),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "failed_login_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
        sa.CheckConstraint(
            "failed_login_attempts >= 0", name=op.f("ck_users_failed_login_attempts_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_users_department_id_departments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_index(op.f("ix_users_department_id"), "users", ["department_id"])
    op.create_index(op.f("ix_users_role"), "users", ["role"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column(
            "actor_type",
            sa.Enum(
                *ACTOR_TYPES,
                name="actor_type",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("actor_role", sa.String(length=20), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=True),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(
                *OUTCOMES, name="outcome", native_enum=False, create_constraint=True, length=20
            ),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_audit_logs_actor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(op.f("ix_audit_logs_occurred_at"), "audit_logs", [sa.text("occurred_at DESC")])
    op.create_index(
        op.f("ix_audit_logs_actor_id_occurred_at"), "audit_logs", ["actor_id", "occurred_at"]
    )
    op.create_index(op.f("ix_audit_logs_entity"), "audit_logs", ["entity_type", "entity_id"])
    op.create_index(op.f("ix_audit_logs_action"), "audit_logs", ["action"])

    # Append-only enforcement at the database level (defence in depth against app bugs).
    op.execute(
        """
        CREATE FUNCTION audit_logs_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit_logs_reject_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_no_truncate
        BEFORE TRUNCATE ON audit_logs
        FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_reject_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs")
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update_delete ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_reject_mutation()")
    op.drop_index(op.f("ix_audit_logs_action"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_entity"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_actor_id_occurred_at"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_occurred_at"), table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index(op.f("ix_users_role"), table_name="users")
    op.drop_index(op.f("ix_users_department_id"), table_name="users")
    op.drop_table("users")
    op.drop_table("departments")
    op.execute("DROP EXTENSION IF EXISTS vector")
