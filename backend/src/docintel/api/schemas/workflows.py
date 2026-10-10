"""Workflow and approval API schemas (Modules 17, 18)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from docintel.api.schemas.analysis import AnalysisResult
from docintel.api.schemas.common import QueryText, RequestModel, ResponseModel
from docintel.db.models import (
    ActionRisk,
    ActionStatus,
    ActorType,
    DocumentStatus,
    DocumentType,
    ProposerType,
    StepStatus,
    WorkflowActionType,
    WorkflowStatus,
    WorkflowTrigger,
    WorkflowType,
)


class WorkflowCreate(RequestModel):
    workflow_type: WorkflowType
    document_id: uuid.UUID


class WorkflowApprove(RequestModel):
    reason: QueryText | None = Field(
        default=None, max_length=1000, description="Optional note recorded with the approval"
    )


class WorkflowReject(RequestModel):
    reason: QueryText = Field(
        min_length=3, max_length=1000, description="Why the proposal is rejected (required)"
    )


class WorkflowCancel(RequestModel):
    reason: QueryText | None = Field(default=None, max_length=1000)


class UserRef(ResponseModel):
    id: uuid.UUID
    email: str
    full_name: str


class WorkflowDocument(ResponseModel):
    id: uuid.UUID
    filename: str
    document_type: DocumentType | None
    status: DocumentStatus
    version_number: int | None
    is_current_version: bool = Field(description="False: a newer version was uploaded since")


class StepRead(ResponseModel):
    sequence: int
    step_name: str
    status: StepStatus
    output: dict[str, Any]
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


class TransitionRead(ResponseModel):
    from_status: ActionStatus | None
    to_status: ActionStatus
    actor_type: ActorType
    actor: UserRef | None
    reason: str | None
    created_at: datetime


class ActionRead(ResponseModel):
    id: uuid.UUID
    action_type: WorkflowActionType
    title: str
    status: ActionStatus
    risk_level: ActionRisk
    requires_approval: bool
    required_role: str | None
    proposed_by_type: ProposerType
    rationale: str
    confidence_level: str | None
    confidence_score: float | None
    payload: dict[str, Any] = Field(description="What the action acts on, fixed at proposal")
    decided_by: UserRef | None
    decided_at: datetime | None
    decision_reason: str | None
    executed_at: datetime | None
    execution_result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    transitions: list[TransitionRead]
    can_decide: bool = Field(description="Whether you may approve or reject it now")
    blockers: list[str] = Field(description="Why you may not decide it (empty if you may)")


class PendingAction(ResponseModel):
    id: uuid.UUID
    action_type: WorkflowActionType
    title: str
    risk_level: ActionRisk
    required_role: str | None


class WorkflowSummary(ResponseModel):
    id: uuid.UUID
    workflow_type: WorkflowType
    title: str
    status: WorkflowStatus
    outcome: str | None
    trigger: WorkflowTrigger
    document: WorkflowDocument
    initiated_by: UserRef
    current_step: str | None
    pending_action: PendingAction | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class WorkflowRead(WorkflowSummary):
    definition_version: int
    steps: list[StepRead]
    actions: list[ActionRead]
    agent_run_id: uuid.UUID | None
    analysis: AnalysisResult | None = Field(
        description="The investigation the proposal rests on (findings, evidence, sources)"
    )
    report_ids: list[uuid.UUID]


class WorkflowPage(ResponseModel):
    items: list[WorkflowSummary]
    total: int
    limit: int
    offset: int


class WorkflowCounts(ResponseModel):
    awaiting_my_decision: int = Field(description="Actions you may approve or reject now")
