"""Workflows, human approval of their actions (HITL) and generated reports (Phase 8).

A workflow is an instance of a code-defined sequence of steps over one document version. Its
high-impact outcome is a `WorkflowAction` that a person other than its makers must approve
before an allowlisted executor carries it out; every state change of an action is kept in
`workflow_action_transitions` (append-only). Reports are rendered from a stored snapshot, so the
same snapshot always renders to the same bytes (`content_sha256`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docintel.db.base import Base, UUIDPrimaryKeyMixin
from docintel.db.models.audit import ActorType
from docintel.db.models.identity import User
from docintel.db.models.types import str_enum


class WorkflowType(StrEnum):
    INVOICE_PROCESSING = "INVOICE_PROCESSING"
    CONTRACT_REVIEW = "CONTRACT_REVIEW"


class WorkflowStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"  # the proposed action was rejected by an approver
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_WORKFLOW_STATUSES = (
    WorkflowStatus.QUEUED,
    WorkflowStatus.RUNNING,
    WorkflowStatus.AWAITING_APPROVAL,
)
FINISHED_WORKFLOW_STATUSES = (
    WorkflowStatus.COMPLETED,
    WorkflowStatus.REJECTED,
    WorkflowStatus.FAILED,
    WorkflowStatus.CANCELLED,
)


class WorkflowTrigger(StrEnum):
    MANUAL = "MANUAL"  # started by a person (API)
    AUTO = "AUTO"  # started when the document finished processing (WORKFLOW_AUTO_START)


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class WorkflowActionType(StrEnum):
    """The allowlist: an action outside it can never be stored, approved or executed."""

    APPROVE_FOR_PAYMENT = "APPROVE_FOR_PAYMENT"
    REJECT_DUPLICATE = "REJECT_DUPLICATE"
    REQUEST_VENDOR_CLARIFICATION = "REQUEST_VENDOR_CLARIFICATION"
    HOLD_FOR_REVIEW = "HOLD_FOR_REVIEW"
    APPROVE_CONTRACT = "APPROVE_CONTRACT"
    REQUEST_LEGAL_REVIEW = "REQUEST_LEGAL_REVIEW"


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class ActionRisk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ProposerType(StrEnum):
    RULES = "RULES"  # the deterministic recommendation
    AGENT = "AGENT"  # a model's proposal that passed the guardrails
    USER = "USER"


class ReportType(StrEnum):
    INVOICE_VERIFICATION = "INVOICE_VERIFICATION"
    CONTRACT_REVIEW = "CONTRACT_REVIEW"
    DOCUMENT_COMPARISON = "DOCUMENT_COMPARISON"
    COMPLIANCE_REVIEW = "COMPLIANCE_REVIEW"
    AI_ANALYSIS = "AI_ANALYSIS"


class ReportSubject(StrEnum):
    DOCUMENT = "DOCUMENT"
    COMPARISON = "COMPARISON"
    AGENT_RUN = "AGENT_RUN"


class Workflow(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "workflows"
    __table_args__ = (
        Index("ix_workflows_document_id", "document_id"),
        Index("ix_workflows_status", "status"),
        Index("ix_workflows_initiated_by_created", "initiated_by_id", "created_at"),
        # One active workflow of a type per document: a second start is a conflict.
        Index(
            "uq_workflows_active_document_type",
            "document_id",
            "workflow_type",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING', 'AWAITING_APPROVAL')"),
        ),
        CheckConstraint(
            "(finished_at IS NOT NULL) = "
            "(status IN ('COMPLETED', 'REJECTED', 'FAILED', 'CANCELLED'))",
            name="finished_when_done",
        ),
        CheckConstraint("definition_version >= 1", name="definition_version_positive"),
    )

    workflow_type: Mapped[WorkflowType] = mapped_column(
        str_enum(WorkflowType, "workflow_type", length=40)
    )
    definition_version: Mapped[int]
    status: Mapped[WorkflowStatus] = mapped_column(
        str_enum(WorkflowStatus, "status"), default=WorkflowStatus.QUEUED
    )
    trigger: Mapped[WorkflowTrigger] = mapped_column(str_enum(WorkflowTrigger, "trigger"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    # The version the workflow verifies; a newer version needs a new workflow.
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    initiated_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    current_step: Mapped[str | None] = mapped_column(String(40))
    # What the workflow ended with (e.g. EXECUTED APPROVE_FOR_PAYMENT, NO_ACTION).
    outcome: Mapped[str | None] = mapped_column(String(60))
    error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )

    initiated_by: Mapped[User] = relationship(lazy="joined", foreign_keys=[initiated_by_id])
    steps: Mapped[list[WorkflowStep]] = relationship(
        order_by="WorkflowStep.sequence", lazy="selectin", cascade="all, delete-orphan"
    )
    actions: Mapped[list[WorkflowAction]] = relationship(
        order_by="WorkflowAction.created_at", lazy="selectin", viewonly=True
    )


class WorkflowStep(UUIDPrimaryKeyMixin, Base):
    """One step of a workflow instance (`workflow_tasks`): bounded, content-free output."""

    __tablename__ = "workflow_tasks"
    __table_args__ = (
        UniqueConstraint("workflow_id", "sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    sequence: Mapped[int]
    step_name: Mapped[str] = mapped_column(String(40))
    status: Mapped[StepStatus] = mapped_column(
        str_enum(StepStatus, "status"), default=StepStatus.PENDING
    )
    output: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(String(1000))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class WorkflowAction(UUIDPrimaryKeyMixin, Base):
    """The unit of human approval (Module 17).

    `maker_ids` lists everyone who may not approve the action - whoever started the workflow,
    uploaded the document or proposed the action; the database rejects a decision by any of
    them (maker-checker), whatever the application does.
    """

    __tablename__ = "workflow_actions"
    __table_args__ = (
        Index("ix_workflow_actions_workflow_id", "workflow_id"),
        Index("ix_workflow_actions_document_id", "document_id"),
        Index("ix_workflow_actions_status", "status"),
        CheckConstraint(
            "decided_by_id IS NULL OR NOT (decided_by_id = ANY (maker_ids))",
            name="maker_checker",
        ),
        CheckConstraint(
            "status <> 'REJECTED' OR char_length(btrim(coalesce(decision_reason, ''))) > 0",
            name="rejection_has_reason",
        ),
        CheckConstraint(
            "NOT requires_approval OR required_role IS NOT NULL", name="approval_has_role"
        ),
        # An action that needs approval is only ever decided by a person.
        CheckConstraint(
            "NOT requires_approval OR status IN ('PROPOSED', 'AWAITING_APPROVAL') "
            "OR decided_by_id IS NOT NULL",
            name="approved_by_person",
        ),
        CheckConstraint(
            "status IN ('PROPOSED', 'AWAITING_APPROVAL') OR decided_at IS NOT NULL",
            name="decided_at_when_decided",
        ),
        CheckConstraint("status <> 'EXECUTED' OR executed_at IS NOT NULL", name="executed_at_set"),
        CheckConstraint("char_length(rationale) <= 2000", name="rationale_length"),
        CheckConstraint(
            "decision_reason IS NULL OR char_length(decision_reason) <= 1000",
            name="decision_reason_length",
        ),
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)",
            name="confidence_score_range",
        ),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    action_type: Mapped[WorkflowActionType] = mapped_column(
        str_enum(WorkflowActionType, "action_type", length=40)
    )
    status: Mapped[ActionStatus] = mapped_column(str_enum(ActionStatus, "status"))
    risk_level: Mapped[ActionRisk] = mapped_column(str_enum(ActionRisk, "risk_level"))
    requires_approval: Mapped[bool]
    required_role: Mapped[str | None] = mapped_column(String(20))
    proposed_by_type: Mapped[ProposerType] = mapped_column(str_enum(ProposerType, "proposed_by"))
    proposed_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    rationale: Mapped[str] = mapped_column(Text)
    confidence_level: Mapped[str | None] = mapped_column(String(10))
    confidence_score: Mapped[float | None] = mapped_column(Numeric(4, 3, asdecimal=False))
    # What the executor needs (amounts, discrepancies, duplicates) - fixed at proposal time.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    maker_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid()))
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    decided_at: Mapped[datetime | None]
    decision_reason: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime | None]
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(1000))
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )

    decided_by: Mapped[User | None] = relationship(lazy="joined", foreign_keys=[decided_by_id])
    transitions: Mapped[list[WorkflowActionTransition]] = relationship(
        order_by="WorkflowActionTransition.id", lazy="selectin", viewonly=True
    )


class WorkflowActionTransition(Base):
    """Every state change of an action: who, when, from what to what, and why (append-only)."""

    __tablename__ = "workflow_action_transitions"
    __table_args__ = (
        Index("ix_workflow_action_transitions_action_id", "action_id", "id"),
        CheckConstraint("reason IS NULL OR char_length(reason) <= 1000", name="reason_length"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    action_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_actions.id", ondelete="CASCADE")
    )
    from_status: Mapped[ActionStatus | None] = mapped_column(str_enum(ActionStatus, "from_status"))
    to_status: Mapped[ActionStatus] = mapped_column(str_enum(ActionStatus, "to_status"))
    actor_type: Mapped[ActorType] = mapped_column(str_enum(ActorType, "actor_type"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )

    actor: Mapped[User | None] = relationship(lazy="joined")


class Report(UUIDPrimaryKeyMixin, Base):
    """A generated report: the snapshot it was rendered from, the rendering and its SHA-256.

    Readable by whoever can read every document it includes (`document_ids`).
    """

    __tablename__ = "reports"
    __table_args__ = (
        Index("ix_reports_subject", "subject_type", "subject_id"),
        Index("ix_reports_generated_by_created", "generated_by_id", "created_at"),
        Index("ix_reports_workflow_id", "workflow_id"),
        Index("ix_reports_document_ids", "document_ids", postgresql_using="gin"),
        CheckConstraint("cardinality(document_ids) >= 1", name="has_documents"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint("template_version >= 1", name="template_version_positive"),
    )

    report_type: Mapped[ReportType] = mapped_column(str_enum(ReportType, "report_type", length=40))
    subject_type: Mapped[ReportSubject] = mapped_column(str_enum(ReportSubject, "subject_type"))
    subject_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(String(300))
    document_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid()))
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL")
    )
    template_version: Mapped[int]
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content: Mapped[str] = mapped_column(Text)  # Markdown
    content_sha256: Mapped[str] = mapped_column(String(64))
    # The newest timestamp among the records the report shows (not when it was generated).
    as_of: Mapped[datetime]
    generated_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )

    generated_by: Mapped[User] = relationship(lazy="joined")
