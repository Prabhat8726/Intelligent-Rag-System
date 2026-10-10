"""Investigations run in the worker (AGENT_ANALYSIS jobs): one attempt each.

A run is never retried automatically: it may already have recorded a comparison or requested a
review, and its model calls cost money. A failed run is FAILED with a user-safe reason; the user
can start a new one.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.agent.graph import AgentDeps, Investigation, RunContext, compose_result
from docintel.agent.state import InvestigationState
from docintel.agent.tools import ToolEnvironment, build_registry
from docintel.ai.accounting import AccountedLLMProvider, DatabaseLLMCallLog
from docintel.ai.base import LLMProvider
from docintel.ai.registry import build_llm_provider, model_prices
from docintel.ai.routing import ExternalAIGate
from docintel.audit.service import SYSTEM_REQUEST, AuditAction, record_audit_event
from docintel.core.config import Settings
from docintel.core.logging import get_logger
from docintel.db.models import (
    FINISHED_RUN_STATUSES,
    ActorType,
    AgentRun,
    AgentRunStatus,
    AgentToolCall,
    AuditOutcome,
    JobStatus,
    LLMCall,
    Sensitivity,
    User,
)
from docintel.knowledge.embedding import ChunkEmbedder
from docintel.knowledge.rag import build_rag_engines
from docintel.processing.pipeline import PermanentProcessingError
from docintel.workers.queue import ClaimedJob

logger = get_logger(__name__)

RECURSION_LIMIT = 40


def build_agent_deps(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    llm: LLMProvider | None = None,
    embedder: ChunkEmbedder | None = None,
) -> AgentDeps:
    """Tools, model and gate for investigations. `llm` must already be accounted (the worker
    passes its pipeline model); otherwise one is built and accounted here."""
    if llm is None and settings.agent_llm_enabled and settings.llm_configured:
        log = DatabaseLLMCallLog(
            sessionmaker, daily_request_budget=settings.llm_daily_request_budget
        )
        llm = AccountedLLMProvider(build_llm_provider(settings), log, model_prices(settings))
    model = llm if settings.agent_llm_enabled else None
    # Retrieval only: the agent never asks the RAG answer generator (no second accounting).
    rag = build_rag_engines(settings, None, llm=None, embedder=embedder)
    return AgentDeps(
        registry=build_registry(ToolEnvironment(settings, sessionmaker, rag)),
        settings=settings,
        llm=model,
        gate=ExternalAIGate(
            max_sensitivity=Sensitivity(settings.ai_external_max_sensitivity),
            provider_configured=model is not None,
            provider_local=model.local if model is not None else False,
        ),
    )


async def investigate(
    context: RunContext,
    *,
    query: str,
    document_ids: list[uuid.UUID],
    allow_safe_actions: bool,
) -> InvestigationState:
    """Run the graph once (bounded by AGENT_TIMEOUT_SECONDS)."""
    initial: InvestigationState = {
        "run_id": str(context.run_id),
        "user_id": str(context.user_id),
        "query": query,
        "requested_documents": [str(document_id) for document_id in document_ids],
        "allow_safe_actions": allow_safe_actions,
        "notices": [],
        "tool_calls": [],
        "trace": [],
        "rounds": 0,
    }
    graph = Investigation(context).build()
    async with asyncio.timeout(context.deps.settings.agent_timeout_seconds):
        state: InvestigationState = await graph.ainvoke(
            initial, config={"recursion_limit": RECURSION_LIMIT}
        )
    return state


@dataclass(slots=True)
class AnalysisJob:
    job: ClaimedJob
    run_id: uuid.UUID
    user_id: uuid.UUID
    query: str
    document_ids: list[uuid.UUID]
    allow_safe_actions: bool
    stage_timings: dict[str, float] = field(default_factory=dict)
    state: InvestigationState | None = None
    context: RunContext | None = None


async def tool_call_count(session: AsyncSession, run_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count()).select_from(AgentToolCall).where(AgentToolCall.run_id == run_id)
    )
    return int(count or 0)


async def usage(
    session: AsyncSession, run_id: uuid.UUID
) -> tuple[int, int | None, int | None, Decimal | None]:
    """Model calls, tokens and estimated cost recorded for the run (llm_calls)."""
    row = (
        await session.execute(
            select(
                func.count(),
                func.sum(LLMCall.input_tokens),
                func.sum(LLMCall.output_tokens),
                func.sum(LLMCall.estimated_cost_usd),
            ).where(LLMCall.agent_run_id == run_id)
        )
    ).one()
    return int(row[0]), row[1], row[2], row[3]


async def record_run_result(
    session: AsyncSession,
    run: AgentRun,
    state: InvestigationState,
    run_context: RunContext,
    finished_at: datetime,
) -> dict[str, Any]:
    """Store a finished investigation (result, trace, usage) and audit it. Returns the result."""
    result = compose_result(state, run_context)
    calls, input_tokens, output_tokens, cost = await usage(session, run.id)
    run.status = AgentRunStatus.COMPLETED
    run.plan = state["plan"]
    run.result = result
    run.trace = list(state.get("trace", []))
    run.tool_calls = run_context.tool_calls
    run.llm_calls = calls
    run.input_tokens = input_tokens
    run.output_tokens = output_tokens
    run.estimated_cost_usd = cost
    run.finished_at = finished_at
    run.error = None
    user = await session.get(User, run.requested_by_id)
    recommendation = result["recommendation"]
    record_audit_event(
        session,
        action=AuditAction.ANALYSIS_COMPLETED,
        outcome=AuditOutcome.SUCCESS,
        meta=SYSTEM_REQUEST,
        actor=user,
        actor_type=ActorType.AGENT,
        entity_type="agent_run",
        entity_id=run.id,
        details={
            "intent": result["intent"],
            "documents": [d["document_id"] for d in result["documents"]],
            "recommendation": recommendation["action"],
            "recommendation_source": recommendation["source"],
            "requires_approval": recommendation["requires_approval"],
            "action": (result["action"] or {}).get("status"),
            "confidence": result["confidence"]["level"],
            "tool_calls": run_context.tool_calls,
            "llm_calls": calls,
            "model": result["model"],
            "workflow_id": run.options.get("workflow_id"),
        },
    )
    logger.info(
        "agent.run_completed",
        run_id=str(run.id),
        intent=result["intent"],
        recommendation=recommendation["action"],
        tool_calls=run_context.tool_calls,
        llm_calls=calls,
    )
    return result


async def record_run_failure(session: AsyncSession, run: AgentRun, user_message: str) -> None:
    """Mark a run FAILED with a user-safe reason, its usage so far, and audit it."""
    run.status = AgentRunStatus.FAILED
    run.error = user_message[:1000]
    run.finished_at = datetime.now(UTC)
    calls, input_tokens, output_tokens, cost = await usage(session, run.id)
    run.llm_calls, run.input_tokens, run.output_tokens = calls, input_tokens, output_tokens
    run.estimated_cost_usd = cost
    run.tool_calls = await tool_call_count(session, run.id)
    user = await session.get(User, run.requested_by_id)
    record_audit_event(
        session,
        action=AuditAction.ANALYSIS_FAILED,
        outcome=AuditOutcome.FAILURE,
        meta=SYSTEM_REQUEST,
        actor=user,
        actor_type=ActorType.AGENT,
        entity_type="agent_run",
        entity_id=run.id,
        details={"error": user_message[:300]},
    )


class AgentAnalysisHandler:
    def __init__(self, deps: AgentDeps) -> None:
        self._deps = deps

    async def prepare(self, session: AsyncSession, job: ClaimedJob) -> AnalysisJob | None:
        if job.agent_run_id is None:
            return None
        run = await session.scalar(
            select(AgentRun).where(AgentRun.id == job.agent_run_id).with_for_update()
        )
        if run is None or run.status in FINISHED_RUN_STATUSES:
            return None
        run.status = AgentRunStatus.RUNNING
        run.started_at = datetime.now(UTC)
        return AnalysisJob(
            job=job,
            run_id=run.id,
            user_id=run.requested_by_id,
            query=run.query,
            document_ids=list(run.document_ids),
            allow_safe_actions=bool(run.options.get("allow_safe_actions", True)),
        )

    async def execute(
        self, context: AnalysisJob, on_stage: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        async def on_node(node: str) -> None:
            await on_stage(node, context.stage_timings)

        run = RunContext(self._deps, context.run_id, context.user_id, on_node=on_node)
        context.context = run
        try:
            context.state = await investigate(
                run,
                query=context.query,
                document_ids=context.document_ids,
                allow_safe_actions=context.allow_safe_actions,
            )
        except TimeoutError as exc:
            msg = "The investigation exceeded its time limit (AGENT_TIMEOUT_SECONDS)."
            raise PermanentProcessingError(msg) from exc
        for step in context.state.get("trace", []):
            node = step["node"]
            context.stage_timings[node] = round(
                context.stage_timings.get(node, 0.0) + float(step["duration_ms"]), 2
            )

    async def on_success(
        self, session: AsyncSession, context: AnalysisJob, finished_at: datetime
    ) -> None:
        assert context.state is not None and context.context is not None  # noqa: S101, PT018
        run = await session.scalar(
            select(AgentRun).where(AgentRun.id == context.run_id).with_for_update()
        )
        if run is None:
            return
        await record_run_result(session, run, context.state, context.context, finished_at)

    async def on_failure(
        self, session: AsyncSession, job: ClaimedJob, *, new_status: Any, user_message: str
    ) -> None:
        if job.agent_run_id is None:
            return
        run = await session.scalar(
            select(AgentRun).where(AgentRun.id == job.agent_run_id).with_for_update()
        )
        if run is None or run.status in FINISHED_RUN_STATUSES:
            return
        if new_status != JobStatus.FAILED:
            run.status = AgentRunStatus.QUEUED
            return
        await record_run_failure(session, run, user_message)
