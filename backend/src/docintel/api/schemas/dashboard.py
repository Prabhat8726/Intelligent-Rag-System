"""Dashboard (Module 19)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import Field

from docintel.api.schemas.common import ResponseModel


class DocumentFigures(ResponseModel):
    total: int
    uploaded_in_period: int
    by_status: dict[str, int]
    by_type: dict[str, int]


class ProcessingFigures(ResponseModel):
    processed_in_period: int
    average_seconds: float | None = Field(description="Mean processing time of a document")
    p95_seconds: float | None
    failed_in_period: int


class ReviewQueueFigures(ResponseModel):
    open: int
    overdue: int
    by_priority: dict[str, int]
    by_type: dict[str, int]


class RuleFailures(ResponseModel):
    rule_code: str
    documents: int


class DiscrepancyFigures(ResponseModel):
    documents_failing: int = Field(description="Documents with at least one failed rule now")
    by_rule: list[RuleFailures]


class InvestigationFigures(ResponseModel):
    scope: Literal["all", "mine"] = Field(
        description="Investigations are personal: administrators see all, others their own"
    )
    in_period: int
    by_status: dict[str, int]
    by_recommendation: dict[str, int]


class WorkflowFigures(ResponseModel):
    awaiting_approval: int
    finished_in_period: int
    by_outcome: dict[str, int]
    by_status: dict[str, int]


class ConfidencePoint(ResponseModel):
    day: date
    processed: int = Field(description="Documents whose current extraction was made that day")
    extraction_confidence: float | None = Field(description="Their mean overall confidence")
    auto_accepted: int = Field(description="Of those, accepted without review")


class ActivityItem(ResponseModel):
    id: int
    occurred_at: datetime
    action: str
    outcome: str
    actor: str
    document_id: uuid.UUID | None
    document_name: str | None
    workflow_id: uuid.UUID | None


class DashboardSummary(ResponseModel):
    days: int
    since: datetime
    until: datetime
    documents: DocumentFigures
    processing: ProcessingFigures
    review_queue: ReviewQueueFigures
    discrepancies: DiscrepancyFigures
    investigations: InvestigationFigures
    workflows: WorkflowFigures
    confidence: list[ConfidencePoint]
    activity: list[ActivityItem]
