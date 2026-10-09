"""Agent: investigation runs, tool calls, API tokens, review requests, agent analysis jobs.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_TYPES = ("INVESTIGATION",)
RUN_STATUSES = ("QUEUED", "RUNNING", "COMPLETED", "FAILED")
TOOL_STATUSES = ("SUCCEEDED", "FAILED", "DENIED", "INVALID")
CHANNELS = ("AGENT", "MCP")
PRIORITIES = ("URGENT", "HIGH", "NORMAL", "LOW")
JOB_TYPES_BEFORE = ("DOCUMENT_PROCESSING", "KNOWLEDGE_PROCESSING")
JOB_TYPES = ("DOCUMENT_PROCESSING", "KNOWLEDGE_PROCESSING", "AGENT_ANALYSIS")
TASK_TYPES_BEFORE = (
    "DUPLICATE_REVIEW",
    "DISCREPANCY_REVIEW",
    "EXTRACTION_REVIEW",
    "CLASSIFICATION_REVIEW",
)
TASK_TYPES = (*TASK_TYPES_BEFORE, "REQUESTED_REVIEW")


def _enum(values: tuple[str, ...], name: str, length: int = 20) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _created_at(default: str = "clock_timestamp()") -> sa.Column[datetime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text(default), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_type", _enum(RUN_TYPES, "run_type"), nullable=False),
        sa.Column("status", _enum(RUN_STATUSES, "status"), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column(
            "document_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("requested_by_id", sa.Uuid(), nullable=False),
        sa.Column("graph_version", sa.String(length=40), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "trace",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column("tool_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("llm_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=True),
        _created_at(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("char_length(query) <= 2000", name=op.f("ck_agent_runs_query_length")),
        sa.CheckConstraint(
            "(finished_at IS NOT NULL) = (status IN ('COMPLETED', 'FAILED'))",
            name=op.f("ck_agent_runs_finished_when_done"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name=op.f("fk_agent_runs_requested_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index(
        "ix_agent_runs_requested_by_created", "agent_runs", ["requested_by_id", "created_at"]
    )
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])

    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("via", _enum(CHANNELS, "via"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("node_name", sa.String(length=40), nullable=True),
        sa.Column("tool_name", sa.String(length=60), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", _enum(TOOL_STATUSES, "status"), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("latency_ms", sa.Numeric(12, 2), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "via = 'MCP' OR run_id IS NOT NULL",
            name=op.f("ck_agent_tool_calls_agent_calls_have_run"),
        ),
        sa.CheckConstraint(
            "latency_ms >= 0", name=op.f("ck_agent_tool_calls_latency_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name=op.f("fk_agent_tool_calls_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_agent_tool_calls_actor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_tool_calls")),
    )
    op.create_index("ix_agent_tool_calls_run_step", "agent_tool_calls", ["run_id", "step_index"])
    op.create_index(
        "ix_agent_tool_calls_actor_created", "agent_tool_calls", ["actor_id", "created_at"]
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(length=40)), nullable=False),
        _created_at("now()"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_api_tokens_expires_after_creation")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_api_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])

    op.create_table(
        "review_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("priority", _enum(PRIORITIES, "priority"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 1000", name=op.f("ck_review_requests_reason_length")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_review_requests_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_review_requests_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name=op.f("fk_review_requests_requested_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_review_requests_agent_run_id_agent_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_requests")),
    )
    op.create_index(
        "ix_review_requests_document_version",
        "review_requests",
        ["document_id", "document_version_id"],
    )
    op.create_index("ix_review_requests_version_id", "review_requests", ["document_version_id"])
    op.create_index("ix_review_requests_requested_by_id", "review_requests", ["requested_by_id"])
    op.create_index("ix_review_requests_agent_run_id", "review_requests", ["agent_run_id"])
    _replace_check("review_tasks", "task_type", TASK_TYPES)

    # Model calls made for a run are attributed to it.
    op.add_column("llm_calls", sa.Column("agent_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_llm_calls_agent_run_id_agent_runs"),
        "llm_calls",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_llm_calls_agent_run_id", "llm_calls", ["agent_run_id"])

    # Runs are executed by the worker through the same job queue.
    op.add_column("processing_jobs", sa.Column("agent_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_processing_jobs_agent_run_id_agent_runs"),
        "processing_jobs",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "uq_processing_jobs_agent_run_id", "processing_jobs", ["agent_run_id"], unique=True
    )
    _replace_check("processing_jobs", "job_type", JOB_TYPES)


def _replace_check(table: str, column: str, values: tuple[str, ...]) -> None:
    name = op.f(f"ck_{table}_{column}")
    op.drop_constraint(name, table, type_="check")
    allowed = ", ".join(f"'{value}'" for value in values)
    op.create_check_constraint(name, table, f"{column} IN ({allowed})")


def downgrade() -> None:
    op.execute("DELETE FROM processing_jobs WHERE job_type = 'AGENT_ANALYSIS'")
    _replace_check("processing_jobs", "job_type", JOB_TYPES_BEFORE)
    op.drop_index("uq_processing_jobs_agent_run_id", table_name="processing_jobs")
    op.drop_constraint(
        op.f("fk_processing_jobs_agent_run_id_agent_runs"), "processing_jobs", type_="foreignkey"
    )
    op.drop_column("processing_jobs", "agent_run_id")
    op.drop_index("ix_llm_calls_agent_run_id", table_name="llm_calls")
    op.drop_constraint(
        op.f("fk_llm_calls_agent_run_id_agent_runs"), "llm_calls", type_="foreignkey"
    )
    op.drop_column("llm_calls", "agent_run_id")
    # Requested reviews become a type the previous schema does not know.
    op.execute(
        "UPDATE review_tasks SET status = 'CANCELLED', resolved_at = now(), "
        "resolution_note = 'Requested reviews were removed by a schema downgrade.' "
        "WHERE task_type = 'REQUESTED_REVIEW' AND status IN ('OPEN', 'IN_PROGRESS')"
    )
    op.execute(
        "UPDATE review_tasks SET task_type = 'EXTRACTION_REVIEW' "
        "WHERE task_type = 'REQUESTED_REVIEW'"
    )
    _replace_check("review_tasks", "task_type", TASK_TYPES_BEFORE)
    op.drop_table("review_requests")
    op.drop_table("api_tokens")
    op.drop_table("agent_tool_calls")
    op.drop_table("agent_runs")
