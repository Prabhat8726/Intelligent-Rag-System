"""Agent runs, their tool calls and per-user API tokens (Phase 7)."""

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
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from docintel.db.base import Base, UUIDPrimaryKeyMixin
from docintel.db.models.types import str_enum


class AgentRunType(StrEnum):
    INVESTIGATION = "INVESTIGATION"


class AgentRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


FINISHED_RUN_STATUSES = (AgentRunStatus.COMPLETED, AgentRunStatus.FAILED)


class ToolCallStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"  # the tool ran and failed (not found, not processable, provider error)
    DENIED = "DENIED"  # the caller lacks the tool's permission
    INVALID = "INVALID"  # unknown tool or arguments that do not validate


class ToolChannel(StrEnum):
    AGENT = "AGENT"  # called by the investigation graph inside a run
    MCP = "MCP"  # called directly by an MCP client with an API token


class AgentRun(UUIDPrimaryKeyMixin, Base):
    """One investigation: the request, the plan, the structured result and its accounting.

    `result` holds findings, confidence, recommendation and sources - never model reasoning.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_requested_by_created", "requested_by_id", "created_at"),
        Index("ix_agent_runs_status", "status"),
        CheckConstraint("char_length(query) <= 2000", name="query_length"),
        CheckConstraint(
            "(finished_at IS NOT NULL) = (status IN ('COMPLETED', 'FAILED'))",
            name="finished_when_done",
        ),
    )

    run_type: Mapped[AgentRunType] = mapped_column(str_enum(AgentRunType, "run_type"))
    status: Mapped[AgentRunStatus] = mapped_column(
        str_enum(AgentRunStatus, "status"), default=AgentRunStatus.QUEUED
    )
    query: Mapped[str] = mapped_column(Text)
    # Documents the requester named (all visible to them when the run was created).
    document_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid()), default=list, server_default=text("'{}'::uuid[]")
    )
    options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    requested_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    graph_version: Mapped[str] = mapped_column(String(40))
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Node, outcome and duration per step - no content and no model reasoning.
    trace: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(String(1000))
    tool_calls: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    llm_calls: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class AgentToolCall(UUIDPrimaryKeyMixin, Base):
    """One controlled tool call, by the agent or an MCP client, whatever its outcome."""

    __tablename__ = "agent_tool_calls"
    __table_args__ = (
        Index("ix_agent_tool_calls_run_step", "run_id", "step_index"),
        Index("ix_agent_tool_calls_actor_created", "actor_id", "created_at"),
        CheckConstraint("via = 'MCP' OR run_id IS NOT NULL", name="agent_calls_have_run"),
        CheckConstraint("latency_ms >= 0", name="latency_non_negative"),
    )

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    via: Mapped[ToolChannel] = mapped_column(str_enum(ToolChannel, "via"))
    step_index: Mapped[int | None]
    node_name: Mapped[str | None] = mapped_column(String(40))
    tool_name: Mapped[str] = mapped_column(String(60))
    # Validated arguments (or a note that they did not validate) and a bounded result summary.
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[ToolCallStatus] = mapped_column(str_enum(ToolCallStatus, "status"))
    error: Mapped[str | None] = mapped_column(String(500))
    latency_ms: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )


class ApiToken(UUIDPrimaryKeyMixin, Base):
    """A personal token for MCP clients: only its SHA-256 is stored; scopes narrow, never widen,
    what the owner's role allows (checked on every use)."""

    __tablename__ = "api_tokens"
    __table_args__ = (
        Index("ix_api_tokens_user_id", "user_id"),
        CheckConstraint("expires_at > created_at", name="expires_after_creation"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16))  # shown in lists to tell tokens apart
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(40)))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
