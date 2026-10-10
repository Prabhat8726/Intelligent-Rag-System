"""Dashboard (Module 19): aggregates over the documents the caller can see."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from docintel.api.deps import SessionDep, require_permission
from docintel.api.schemas.common import PROBLEM_RESPONSES
from docintel.api.schemas.dashboard import DashboardSummary
from docintel.auth.permissions import Permission
from docintel.dashboard.service import DashboardService
from docintel.db.models import User

router = APIRouter(prefix="/dashboard", tags=["dashboard"], responses=PROBLEM_RESPONSES)

Reader = Annotated[User, Depends(require_permission(Permission.DASHBOARD_READ))]


@router.get(
    "/summary",
    response_model=DashboardSummary,
    summary="Documents, processing, review queue, discrepancies, investigations, workflows, "
    "confidence trend and recent activity",
    description="Computed live from the documents you can see (your department; administrators "
    "everything). Investigations are personal: administrators count all, others their own.",
)
async def summary(
    user: Reader,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=90, description="Length of the period")] = 30,
) -> DashboardSummary:
    figures = await DashboardService(session).summary(user, days=days)
    return DashboardSummary.model_validate(figures, from_attributes=True)
