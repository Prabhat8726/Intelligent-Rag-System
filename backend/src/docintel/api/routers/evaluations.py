"""Recorded evaluation runs (Phase 10): what was measured, with which data and code, and whether
the regression gates held. Read-only; runs are recorded by `docintel evaluate --record` or
imported from the committed reports (`docintel evaluation import`)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on

from docintel.api.deps import SessionDep, require_permission
from docintel.api.schemas.common import PROBLEM_RESPONSES
from docintel.api.schemas.evaluations import (
    EvaluationDetail,
    EvaluationHeadline,
    EvaluationPage,
    EvaluationSummary,
    GateCheck,
    GateSummary,
    ReportTable,
)
from docintel.auth.permissions import Permission
from docintel.core.errors import NotFoundError
from docintel.db.models import Evaluation, User
from docintel.evaluation.headlines import HEADLINES, headlines_for

router = APIRouter(prefix="/evaluations", tags=["evaluations"], responses=PROBLEM_RESPONSES)

Reader = Annotated[User, Depends(require_permission(Permission.EVALUATIONS_READ))]

SUITE_ORDER = {
    suite: index for index, suite in enumerate(dict.fromkeys(h.suite for h in HEADLINES))
}


def _payload(row: Evaluation) -> dict[str, Any]:
    return {
        "suite": row.suite,
        "metrics": row.metrics,
        "dataset": row.dataset,
        "config": row.config,
        "environment": row.environment,
    }


def _gates(row: Evaluation) -> GateSummary | None:
    if not row.gates:
        return None
    checks = row.gates.get("checks", [])
    return GateSummary(
        passed=bool(row.gates.get("passed")),
        mode=str(row.gates.get("mode", "full")),
        checks=len(checks),
        failed=sum(1 for check in checks if not check.get("passed")),
    )


def summary(row: Evaluation) -> EvaluationSummary:
    return EvaluationSummary(
        id=row.id,
        suite=row.suite,
        title=row.title,
        quick=row.quick,
        git_revision=row.git_revision,
        run_at=row.run_at,
        recorded_at=row.recorded_at,
        source=row.source.value,
        recorded_by=row.recorded_by.full_name if row.recorded_by else None,
        gates=_gates(row),
        headlines=[
            EvaluationHeadline(label=label, value=value)
            for label, value in headlines_for(row.suite, _payload(row))
        ],
    )


@router.get(
    "",
    response_model=EvaluationPage,
    summary="Recorded evaluation runs, newest first, or the latest full run of every suite",
)
async def list_evaluations(
    _: Reader,
    session: SessionDep,
    suite: Annotated[str | None, Query(pattern=r"^[a-z][a-z_]{0,39}$")] = None,
    latest: Annotated[
        bool, Query(description="Only the newest run of each suite (suite order)")
    ] = False,
    include_quick: Annotated[
        bool, Query(description="Include quick (smoke-test) runs; never with latest")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EvaluationPage:
    query = select(Evaluation)
    if suite:
        query = query.where(Evaluation.suite == suite)
    if latest or not include_quick:
        query = query.where(Evaluation.quick.is_(False))
    if latest:
        newest = (
            select(Evaluation.id)
            .ext(distinct_on(Evaluation.suite))
            .where(Evaluation.quick.is_(False))
            .order_by(Evaluation.suite, Evaluation.run_at.desc(), Evaluation.recorded_at.desc())
        )
        query = query.where(Evaluation.id.in_(newest))
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    if latest:
        rows = sorted(
            await session.scalars(query),
            key=lambda row: (SUITE_ORDER.get(row.suite, len(SUITE_ORDER)), row.suite),
        )[offset : offset + limit]
    else:
        rows = list(
            await session.scalars(
                query.order_by(Evaluation.run_at.desc(), Evaluation.recorded_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
    return EvaluationPage(
        items=[summary(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/{evaluation_id}",
    response_model=EvaluationDetail,
    summary="One run: its tables, gate checks, datasets, configuration and environment",
)
async def get_evaluation(
    evaluation_id: uuid.UUID, _: Reader, session: SessionDep
) -> EvaluationDetail:
    row = await session.get(Evaluation, evaluation_id)
    if row is None:
        raise NotFoundError("Evaluation not found.")
    return EvaluationDetail(
        **summary(row).model_dump(),
        dataset=row.dataset,
        config=row.config,
        environment=row.environment,
        metrics=row.metrics,
        notes=row.notes,
        tables=[ReportTable.model_validate(table) for table in row.tables],
        gate_checks=[
            GateCheck.model_validate(check) for check in (row.gates or {}).get("checks", [])
        ],
        report_markdown=row.report_markdown,
    )
