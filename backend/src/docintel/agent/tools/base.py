"""Controlled tools (Module 15): the only way the agent and MCP clients touch the platform.

A tool is a typed function with
* an input model (validated, unknown keys rejected) and an output model (bounded sizes),
* the permission it needs, checked against the caller's role (narrowed by API-token scopes),
* a side-effect class (READ, RECORD: stores an analysis result, WRITE: changes work state),
* a timeout and an output size cap,
* a log row in `agent_tool_calls` for every call, whatever its outcome.

The handler runs as the requesting user, in its own database session, through the same
services and access-policy predicates as the REST API: a tool can never see or do more than
the caller could through the API. There is no shell, file, network, SQL or code tool.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.audit.service import RequestMeta
from docintel.auth.permissions import Permission
from docintel.core.config import Settings
from docintel.db.models import ToolCallStatus, ToolChannel, User
from docintel.knowledge.rag import RagEngines

NOT_FOUND = "Not found or not permitted."


class SideEffect(StrEnum):
    READ = "READ"  # reads only
    RECORD = "RECORD"  # stores an analysis result (a comparison) - no business state changes
    WRITE = "WRITE"  # changes work state (a review task); low risk, audited


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolFailure(Exception):  # noqa: N818 - an expected outcome, not a programming error
    """A failure whose message is safe to return to the caller (and to a model)."""


@dataclass(slots=True)
class ToolEnvironment:
    """Long-lived dependencies shared by every call (built once per process)."""

    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    rag: RagEngines


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is calling, through which channel, for which run."""

    user_id: uuid.UUID
    via: ToolChannel
    meta: RequestMeta = field(default_factory=RequestMeta)
    run_id: uuid.UUID | None = None
    # API-token scopes: the caller's role permissions are intersected with them (None = role).
    scopes: frozenset[str] | None = None
    node: str | None = None


@dataclass(slots=True)
class ToolScope:
    """What a handler gets: a session of its own and the reloaded, active user."""

    session: AsyncSession
    actor: User
    settings: Settings
    rag: RagEngines
    caller: Caller


@dataclass(frozen=True, slots=True)
class Tool[I: ToolInput, O: ToolOutput]:
    name: str
    description: str
    input_model: type[I]
    output_model: type[O]
    permission: Permission
    side_effect: SideEffect
    handler: Callable[[ToolScope, I], Awaitable[O]]
    # A short, content-free summary of the output for the call log.
    summarize: Callable[[O], dict[str, Any]]
    timeout_seconds: float | None = None  # None: AGENT_TOOL_TIMEOUT_SECONDS

    async def run(self, scope: ToolScope, arguments: ToolInput) -> ToolOutput:
        assert isinstance(arguments, self.input_model)  # noqa: S101 - validated by the registry
        return await self.handler(scope, arguments)

    def summary_of(self, output: ToolOutput) -> dict[str, Any]:
        assert isinstance(output, self.output_model)  # noqa: S101 - produced by `run`
        return self.summarize(output)


@dataclass(slots=True)
class ToolResult:
    call_id: uuid.UUID
    tool: str
    status: ToolCallStatus
    output: dict[str, Any] | None
    error: str | None
    latency_ms: float

    @property
    def ok(self) -> bool:
        return self.status == ToolCallStatus.SUCCEEDED

    def to_json(self) -> dict[str, Any]:
        return {
            "call_id": str(self.call_id),
            "tool": self.tool,
            "status": self.status.value,
            "output": self.output,
            "error": self.error,
            "latency_ms": self.latency_ms,
        }


def clip(value: str | None, limit: int) -> str | None:
    """Bound a text for tool output (whitespace collapsed, an ellipsis marks the cut)."""
    if value is None:
        return None
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
