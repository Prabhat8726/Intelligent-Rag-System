"""Agent investigations (Modules 14-15): start one, follow it, read its result."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from docintel.agent.service import AnalysisService
from docintel.agent.state import Plan
from docintel.api.deps import RequestMetaDep, SessionDep, SettingsDep, require_permission
from docintel.api.rate_limit import AI_LIMIT
from docintel.api.schemas.analysis import (
    AnalysisCreate,
    AnalysisPage,
    AnalysisRead,
    AnalysisResult,
    AnalysisSummary,
    AnalysisUsage,
    ToolCallRead,
    TraceStep,
)
from docintel.api.schemas.common import PROBLEM_RESPONSES, ProblemDetail
from docintel.auth.permissions import Permission
from docintel.db.models import AgentRun, AgentRunStatus, AgentToolCall, User

router = APIRouter(
    prefix="/analysis",
    tags=["analysis"],
    responses={
        **PROBLEM_RESPONSES,
        404: {"model": ProblemDetail, "description": "Not found or not accessible"},
    },
)
Runner = Annotated[User, Depends(require_permission(Permission.ANALYSIS_RUN))]
Reader = Annotated[User, Depends(require_permission(Permission.ANALYSIS_READ))]


def summary(run: AgentRun) -> AnalysisSummary:
    result = run.result or {}
    return AnalysisSummary(
        id=run.id,
        status=run.status,
        query=run.query,
        document_ids=list(run.document_ids),
        intent=(run.plan or {}).get("intent"),
        recommendation=(result.get("recommendation") or {}).get("action"),
        confidence=(result.get("confidence") or {}).get("level"),
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def detail(run: AgentRun, calls: list[AgentToolCall]) -> AnalysisRead:
    return AnalysisRead(
        **summary(run).model_dump(),
        graph_version=run.graph_version,
        allow_safe_actions=bool(run.options.get("allow_safe_actions", True)),
        plan=Plan.model_validate(run.plan) if run.plan else None,
        result=AnalysisResult.model_validate(run.result) if run.result else None,
        trace=[TraceStep.model_validate(step) for step in run.trace],
        tool_call_log=[ToolCallRead.model_validate(call) for call in calls],
        usage=AnalysisUsage(
            tool_calls=run.tool_calls,
            llm_calls=run.llm_calls,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            estimated_cost_usd=run.estimated_cost_usd,
        ),
    )


@router.post(
    "",
    dependencies=[AI_LIMIT],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AnalysisRead,
    summary="Start an investigation (runs in the background)",
    description="The agent plans from the request, gathers facts with controlled tools as you "
    "(documents, extracted fields, evidence, rules, comparisons, policies), writes findings "
    "with evidence, assesses confidence and recommends one allowlisted action. Low-risk "
    "actions (requesting a human review) may be executed; others are only proposed.",
    responses={429: {"model": ProblemDetail, "description": "Too many active investigations"}},
)
async def start_analysis(
    body: AnalysisCreate,
    response: Response,
    user: Runner,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> AnalysisRead:
    run = await AnalysisService(session, settings).start(
        user,
        query=body.query,
        document_ids=body.document_ids,
        allow_safe_actions=body.allow_safe_actions,
        meta=meta,
    )
    response.headers["Location"] = f"/api/v1/analysis/{run.id}"
    return detail(run, [])


@router.get("", response_model=AnalysisPage, summary="Your investigations, newest first")
async def list_analyses(
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    run_status: Annotated[AgentRunStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> AnalysisPage:
    runs, total = await AnalysisService(session, settings).runs(
        user, status=run_status, limit=limit, offset=offset
    )
    return AnalysisPage(
        items=[summary(run) for run in runs], total=total, limit=limit, offset=offset
    )


@router.get(
    "/{run_id}",
    response_model=AnalysisRead,
    summary="An investigation with its findings, evidence, recommendation and tool calls",
)
async def get_analysis(
    run_id: uuid.UUID, user: Reader, session: SessionDep, settings: SettingsDep
) -> AnalysisRead:
    service = AnalysisService(session, settings)
    run = await service.get(user, run_id)
    return detail(run, await service.tool_calls(run))
