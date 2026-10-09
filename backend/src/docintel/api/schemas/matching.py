"""Comparisons, business rules, rule results, review tasks and document versions (Phase 5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import Field, computed_field

from docintel.api.schemas.common import QueryText, RequestModel, ResponseModel
from docintel.api.schemas.documents import UserSummary
from docintel.db.models import (
    ComparisonCategory,
    ComparisonItemStatus,
    ComparisonOrigin,
    ComparisonRole,
    ComparisonType,
    DocumentStatus,
    DocumentType,
    ReviewPriority,
    ReviewResolution,
    ReviewTaskStatus,
    ReviewTaskType,
    RuleOutcome,
    RuleSeverity,
)


# ------------------------------------------------------------------------------ comparisons
class ComparisonSide(ResponseModel):
    role: ComparisonRole
    document_id: uuid.UUID | None
    document_label: str
    value: str | None
    printed: str | None
    field_id: uuid.UUID | None = None
    page: int | None = None
    source_text: str | None = None
    bbox: list[float] | None = None
    confidence: float | None = None
    corrected: bool = False


class ComparisonItemRead(ResponseModel):
    id: uuid.UUID
    position: int
    item_key: str
    category: ComparisonCategory
    check_name: str
    line_key: str | None
    status: ComparisonItemStatus
    left_value: str | None
    right_value: str | None
    difference: dict[str, str] | None
    tolerance: dict[str, str] | None
    explanation: str
    left: list[ComparisonSide]
    right: list[ComparisonSide]


class ComparisonDocumentRead(ResponseModel):
    document_id: uuid.UUID
    role: ComparisonRole
    position: int
    document_version_id: uuid.UUID | None
    extraction_id: uuid.UUID | None
    display_filename: str
    document_type: DocumentType | None


class ComparisonSummary(ResponseModel):
    id: uuid.UUID
    comparison_type: ComparisonType
    origin: ComparisonOrigin
    subject_document_id: uuid.UUID
    summary: dict[str, int]
    created_at: datetime
    requested_by: UserSummary | None
    documents: list[ComparisonDocumentRead]


class ComparisonRead(ComparisonSummary):
    settings: dict[str, Any] = Field(description="Tolerances and thresholds used")
    items: list[ComparisonItemRead]


class ComparisonPage(ResponseModel):
    items: list[ComparisonSummary]
    total: int
    limit: int
    offset: int


class ComparisonInput(RequestModel):
    document_id: uuid.UUID
    role: ComparisonRole


class ComparisonCreate(RequestModel):
    """One invoice or delivery note (the subject) with a purchase order and/or delivery notes."""

    documents: list[ComparisonInput] = Field(min_length=2, max_length=10)


# ------------------------------------------------------------------------------ rules
class RuleRead(ResponseModel):
    id: uuid.UUID
    code: str
    rule_type: str
    name: str
    description: str
    applies_to: list[DocumentType]
    params: dict[str, Any]
    severity: RuleSeverity
    is_enabled: bool
    version: int
    updated_by: UserSummary | None
    updated_at: datetime
    params_schema: dict[str, Any] = Field(
        default_factory=dict, description="JSON schema of the parameters this rule type accepts"
    )


class RuleUpdate(RequestModel):
    params: dict[str, Any] | None = None
    severity: RuleSeverity | None = None
    is_enabled: bool | None = None
    note: str | None = Field(default=None, max_length=500)


class RuleEvaluate(RequestModel):
    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class RuleResultRead(ResponseModel):
    id: uuid.UUID
    rule_code: str
    rule_version: int
    outcome: RuleOutcome
    severity: RuleSeverity
    message: str
    evidence: dict[str, Any]
    items: list[str]
    comparison_id: uuid.UUID | None
    evaluated_at: datetime


class RuleEvaluation(ResponseModel):
    document_id: uuid.UUID
    evaluated: bool
    results: list[RuleResultRead]


# ------------------------------------------------------------------------------ review queue
class ReviewReasonRead(ResponseModel):
    key: str
    category: str
    code: str
    severity: RuleSeverity
    message: str


class ReviewDocument(ResponseModel):
    id: uuid.UUID
    display_filename: str
    document_type: DocumentType | None
    status: DocumentStatus


class ReviewTaskRead(ResponseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None
    task_type: ReviewTaskType
    status: ReviewTaskStatus
    priority: ReviewPriority
    reasons: list[ReviewReasonRead]
    due_at: datetime | None
    assigned_to: UserSummary | None
    claimed_at: datetime | None
    resolution: ReviewResolution | None
    resolution_note: str | None
    resolved_by: UserSummary | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def overdue(self) -> bool:
        return (
            self.status in (ReviewTaskStatus.OPEN, ReviewTaskStatus.IN_PROGRESS)
            and self.due_at is not None
            and self.due_at < datetime.now(UTC)
        )


class ReviewTaskListItem(ReviewTaskRead):
    document: ReviewDocument


class ReviewTaskPage(ResponseModel):
    items: list[ReviewTaskListItem]
    total: int
    limit: int
    offset: int


class ReviewResolve(RequestModel):
    resolution: ReviewResolution = Field(
        description="APPROVED (fine as it is), CORRECTED (values fixed) or REJECTED (note needed)"
    )
    note: str | None = Field(default=None, max_length=1000)


# ------------------------------------------------------------------------------ findings
class DuplicateRead(ResponseModel):
    kind: str
    document_id: uuid.UUID
    display_filename: str
    direction: str = Field(description="'original' (this one is a copy of it) or 'copy'")
    evidence: dict[str, Any] = Field(default_factory=dict)


class FindingsRead(ResponseModel):
    """Everything matching found for a document: comparisons, rules, duplicates, reviews."""

    comparisons: list[ComparisonSummary]
    rule_results: list[RuleResultRead]
    duplicates: list[DuplicateRead]
    open_task: ReviewTaskRead | None
    review_history: list[ReviewTaskRead]


# ------------------------------------------------------------------------------ versions
class VersionRead(ResponseModel):
    id: uuid.UUID
    version_number: int
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    page_count: int
    created_at: datetime
    processed: bool
    is_current: bool


class ClauseRead(ResponseModel):
    key: str
    number: str | None
    title: str
    text: str
    page: int | None


class ClauseDiffRead(ResponseModel):
    change: str
    title: str
    old: ClauseRead | None
    new: ClauseRead | None
    similarity: float
    renumbered: bool
    operations: list[dict[str, str]]


class VersionComparisonRead(ResponseModel):
    document_id: uuid.UUID
    from_version: int
    to_version: int
    from_version_id: uuid.UUID
    to_version_id: uuid.UUID
    summary: dict[str, int]
    clauses: list[ClauseDiffRead]


class ReviewRequestCreate(RequestModel):
    document_id: uuid.UUID
    reason: QueryText = Field(
        min_length=5, max_length=1000, description="What a reviewer should check, and why"
    )
    priority: ReviewPriority = ReviewPriority.NORMAL


class ReviewRequestRead(ResponseModel):
    review_request_id: uuid.UUID
    created: bool = Field(description="False: the same request was already on file")
    task_id: uuid.UUID | None
    task_status: ReviewTaskStatus | None
    task_priority: ReviewPriority | None
