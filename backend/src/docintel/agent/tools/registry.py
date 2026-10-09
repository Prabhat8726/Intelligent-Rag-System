"""The tool registry: lookup, authorization, validation, execution and the call log."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from docintel.agent.tools.base import (
    NOT_FOUND,
    Caller,
    Tool,
    ToolEnvironment,
    ToolFailure,
    ToolOutput,
    ToolResult,
    ToolScope,
)
from docintel.ai.errors import ProviderError
from docintel.audit.service import AuditAction, record_audit_event
from docintel.auth.permissions import Permission, permissions_for
from docintel.core.errors import AppError, ConflictError, NotFoundError, UnprocessableContentError
from docintel.core.logging import get_logger
from docintel.db.models import AgentToolCall, AuditOutcome, ToolCallStatus, ToolChannel, User

logger = get_logger(__name__)

_ERROR_LIMIT = 500
_MAX_VALIDATION_ERRORS = 5
_SUMMARY_LIMIT = 4000
AnyTool = Tool[Any, Any]


def effective_permissions(user: User, scopes: frozenset[str] | None) -> frozenset[Permission]:
    """The role's permissions, narrowed by API-token scopes (a scope never adds a permission)."""
    granted = permissions_for(user.role)
    if scopes is None:
        return granted
    return frozenset(permission for permission in granted if permission.value in scopes)


def validation_message(exc: ValidationError) -> str:
    """Field paths and messages - never the rejected input values."""
    parts = []
    for error in exc.errors()[:_MAX_VALIDATION_ERRORS]:
        location = ".".join(str(part) for part in error["loc"]) or "arguments"
        parts.append(f"{location}: {error['msg']}")
    more = exc.error_count() - _MAX_VALIDATION_ERRORS
    if more > 0:
        parts.append(f"... and {more} more")
    return "; ".join(parts)


def _bounded(summary: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(summary, default=str)) <= _SUMMARY_LIMIT:
        return summary
    return {"truncated": True, "keys": sorted(summary)[:20]}


class ToolRegistry:
    def __init__(self, tools: Iterable[AnyTool], environment: ToolEnvironment) -> None:
        self._tools: dict[str, AnyTool] = {}
        for tool in tools:
            if tool.name in self._tools:
                msg = f"duplicate tool {tool.name}"
                raise ValueError(msg)
            self._tools[tool.name] = tool
        self._env = environment

    @property
    def environment(self) -> ToolEnvironment:
        return self._env

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> AnyTool | None:
        return self._tools.get(name)

    def available_to(self, user: User, scopes: frozenset[str] | None = None) -> list[AnyTool]:
        allowed = effective_permissions(user, scopes)
        return [tool for tool in self._tools.values() if tool.permission in allowed]

    async def call(self, name: str, arguments: object, caller: Caller) -> ToolResult:
        """Run one tool call for `caller`. Never raises for tool-level problems: the outcome
        (SUCCEEDED, FAILED, DENIED, INVALID) is returned and logged."""
        call_id = uuid.uuid4()
        started = time.perf_counter()
        tool = self._tools.get(name)
        logged_args: dict[str, Any] = {}
        status = ToolCallStatus.SUCCEEDED
        error: str | None = None
        output: ToolOutput | None = None
        summary: dict[str, Any] | None = None

        async with self._env.sessionmaker() as session:
            user = await session.scalar(select(User).where(User.id == caller.user_id))
            try:
                if tool is None:
                    raise _Rejected(ToolCallStatus.INVALID, f"Unknown tool '{name[:60]}'.")
                if user is None or not user.is_active:
                    raise _Rejected(ToolCallStatus.DENIED, "Not permitted.")
                if tool.permission not in effective_permissions(user, caller.scopes):
                    raise _Rejected(
                        ToolCallStatus.DENIED,
                        f"Not permitted: {tool.name} needs the {tool.permission.value} permission.",
                    )
                try:
                    if isinstance(arguments, tool.input_model):
                        validated = arguments
                    else:
                        validated = tool.input_model.model_validate(
                            dict(arguments) if isinstance(arguments, Mapping) else arguments
                        )
                except ValidationError as exc:
                    raise _Rejected(ToolCallStatus.INVALID, validation_message(exc)) from exc
                logged_args = validated.model_dump(mode="json")
                scope = ToolScope(session, user, self._env.settings, self._env.rag, caller)
                timeout = tool.timeout_seconds or self._env.settings.agent_tool_timeout_seconds
                async with asyncio.timeout(timeout):
                    output = await tool.run(scope, validated)
                size = len(output.model_dump_json())
                if size > self._env.settings.agent_tool_max_output_bytes:
                    msg = f"The result is too large ({size} bytes); narrow the request."
                    raise ToolFailure(msg)
                summary = _bounded(tool.summary_of(output))
                await session.commit()
            except _Rejected as exc:
                await session.rollback()
                status, error = exc.status, exc.message
            except Exception as exc:  # every outcome is returned and logged
                await session.rollback()
                status, error = _failure(exc, call_id, name)
                output = None

            latency = round((time.perf_counter() - started) * 1000, 2)
            if user is not None:
                # A rollback expired the user: reload it (inside the session's async context).
                user = await session.get(User, caller.user_id, populate_existing=True)
            if user is not None:
                # Unknown users cannot be logged against (actor_id is required); they are denied
                # before anything runs and the structured log below records the attempt.
                session.add(
                    AgentToolCall(
                        id=call_id,
                        run_id=caller.run_id,
                        actor_id=user.id,
                        via=caller.via,
                        step_index=None,
                        node_name=caller.node,
                        tool_name=name[:60],
                        arguments=logged_args,
                        result_summary=summary,
                        status=status,
                        error=error[:_ERROR_LIMIT] if error else None,
                        latency_ms=Decimal(f"{latency:.2f}"),
                    )
                )
                if caller.via == ToolChannel.MCP:
                    record_audit_event(
                        session,
                        action=AuditAction.MCP_TOOL_CALLED,
                        outcome=AuditOutcome.DENIED
                        if status == ToolCallStatus.DENIED
                        else AuditOutcome.SUCCESS
                        if status == ToolCallStatus.SUCCEEDED
                        else AuditOutcome.FAILURE,
                        meta=caller.meta,
                        actor=user,
                        entity_type="agent_tool_call",
                        entity_id=call_id,
                        details={"tool": name[:60], "status": status.value, "via": "mcp"},
                    )
                await session.commit()

        logger.info(
            "agent.tool_call",
            tool=name[:60],
            status=status.value,
            via=caller.via.value,
            run_id=str(caller.run_id) if caller.run_id else None,
            node=caller.node,
            latency_ms=latency,
        )
        return ToolResult(
            call_id=call_id,
            tool=name[:60],
            status=status,
            output=output.model_dump(mode="json") if output is not None else None,
            error=error,
            latency_ms=latency,
        )


class _Rejected(Exception):  # noqa: N818 - control flow inside `call`
    def __init__(self, status: ToolCallStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _failure(exc: Exception, call_id: uuid.UUID, name: str) -> tuple[ToolCallStatus, str]:
    """A message that is safe for the caller (and a model) to see; details go to the log."""
    if isinstance(exc, ToolFailure):
        return ToolCallStatus.FAILED, str(exc)
    if isinstance(exc, NotFoundError):
        return ToolCallStatus.FAILED, NOT_FOUND
    if isinstance(exc, UnprocessableContentError | ConflictError):
        return ToolCallStatus.FAILED, exc.detail
    if isinstance(exc, TimeoutError):
        return ToolCallStatus.FAILED, "The tool timed out."
    if isinstance(exc, ProviderError):
        return ToolCallStatus.FAILED, "The AI provider is unavailable."
    if isinstance(exc, AppError):
        return ToolCallStatus.FAILED, exc.detail
    logger.exception("agent.tool_error", tool=name[:60], call_id=str(call_id))
    return ToolCallStatus.FAILED, f"Internal error (reference: tool call {call_id})."
