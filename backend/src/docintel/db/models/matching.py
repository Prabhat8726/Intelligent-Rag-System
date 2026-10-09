"""Comparisons, business rules, rule results and the review queue (Phase 5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docintel.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from docintel.db.models.identity import User
from docintel.db.models.types import str_enum


class ComparisonType(StrEnum):
    INVOICE_PO = "INVOICE_PO"
    INVOICE_DELIVERY = "INVOICE_DELIVERY"
    INVOICE_PO_DELIVERY = "INVOICE_PO_DELIVERY"
    PO_DELIVERY = "PO_DELIVERY"


class ComparisonOrigin(StrEnum):
    AUTO = "AUTO"  # built by the worker for an invoice or delivery note (one per subject)
    MANUAL = "MANUAL"  # requested by a user for documents they chose


class ComparisonRole(StrEnum):
    INVOICE = "INVOICE"
    PURCHASE_ORDER = "PURCHASE_ORDER"
    DELIVERY_NOTE = "DELIVERY_NOTE"


class ComparisonItemStatus(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    MISSING = "MISSING"
    UNCERTAIN = "UNCERTAIN"


class ComparisonCategory(StrEnum):
    HEADER = "HEADER"
    LINE_ITEM = "LINE_ITEM"


class RuleSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RuleOutcome(StrEnum):
    PASS = "PASS"  # noqa: S105 - an outcome, not a password
    FAIL = "FAIL"
    WARN = "WARN"
    ERROR = "ERROR"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReviewTaskType(StrEnum):
    """The most important kind of finding in the task (a task lists all of them)."""

    DUPLICATE_REVIEW = "DUPLICATE_REVIEW"
    DISCREPANCY_REVIEW = "DISCREPANCY_REVIEW"
    EXTRACTION_REVIEW = "EXTRACTION_REVIEW"
    CLASSIFICATION_REVIEW = "CLASSIFICATION_REVIEW"
    REQUESTED_REVIEW = "REQUESTED_REVIEW"  # a person or the agent asked for a look


class ReviewTaskStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"  # claimed by a reviewer
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"  # the document was deleted or replaced by a new version


OPEN_TASK_STATUSES = (ReviewTaskStatus.OPEN, ReviewTaskStatus.IN_PROGRESS)


class ReviewPriority(StrEnum):
    URGENT = "URGENT"
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


class ReviewResolution(StrEnum):
    APPROVED = "APPROVED"  # the document is fine as it is (findings accepted)
    CORRECTED = "CORRECTED"  # the reviewer corrected values; remaining findings accepted
    REJECTED = "REJECTED"  # the document must not be processed further (e.g. a duplicate)
    CLEARED = "CLEARED"  # the findings went away (a correction, a new document, a rule change)


HUMAN_RESOLUTIONS = (
    ReviewResolution.APPROVED,
    ReviewResolution.CORRECTED,
    ReviewResolution.REJECTED,
)


class Comparison(UUIDPrimaryKeyMixin, Base):
    """A comparison of a subject document (invoice, delivery note) with its order and deliveries."""

    __tablename__ = "comparisons"
    __table_args__ = (
        Index(
            "uq_comparisons_auto_subject",
            "subject_document_id",
            unique=True,
            postgresql_where=text("origin = 'AUTO'"),
        ),
        Index("ix_comparisons_department_created", "department_id", "created_at"),
        Index("ix_comparisons_requested_by_id", "requested_by_id"),
    )

    comparison_type: Mapped[ComparisonType] = mapped_column(
        str_enum(ComparisonType, "comparison_type", length=30)
    )
    origin: Mapped[ComparisonOrigin] = mapped_column(str_enum(ComparisonOrigin, "origin"))
    subject_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT")
    )
    # Counts per item status, e.g. {"MATCH": 7, "MISMATCH": 1, "MISSING": 0, "UNCERTAIN": 0}.
    summary: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)
    # Tolerances and thresholds used, so a result can be explained later.
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    documents: Mapped[list[ComparisonDocument]] = relationship(
        cascade="all, delete-orphan",
        order_by="ComparisonDocument.position",
        lazy="selectin",
    )
    items: Mapped[list[ComparisonResult]] = relationship(
        cascade="all, delete-orphan", order_by="ComparisonResult.position", lazy="raise"
    )
    requested_by: Mapped[User | None] = relationship(lazy="joined")


class ComparisonDocument(Base):
    """A document taking part in a comparison, with the extraction that was compared."""

    __tablename__ = "comparison_documents"
    __table_args__ = (
        Index("ix_comparison_documents_document_id", "document_id"),
        Index("ix_comparison_documents_extraction_id", "extraction_id"),
        Index("ix_comparison_documents_version_id", "document_version_id"),
    )

    comparison_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("comparisons.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="SET NULL")
    )
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_extractions.id", ondelete="SET NULL")
    )
    role: Mapped[ComparisonRole] = mapped_column(str_enum(ComparisonRole, "role", length=20))
    position: Mapped[int]


class ComparisonResult(UUIDPrimaryKeyMixin, Base):
    """One compared item (a header value or a line check) with evidence from both sides."""

    __tablename__ = "comparison_results"
    __table_args__ = (Index("ix_comparison_results_comparison_id", "comparison_id", "position"),)

    comparison_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("comparisons.id", ondelete="CASCADE")
    )
    position: Mapped[int]
    item_key: Mapped[str] = mapped_column(String(200))
    category: Mapped[ComparisonCategory] = mapped_column(str_enum(ComparisonCategory, "category"))
    check_name: Mapped[str] = mapped_column(String(40))
    line_key: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[ComparisonItemStatus] = mapped_column(str_enum(ComparisonItemStatus, "status"))
    left_value: Mapped[str | None] = mapped_column(Text)
    right_value: Mapped[str | None] = mapped_column(Text)
    difference: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    tolerance: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    # {"left": [side, ...], "right": [side, ...]}: document, field, page, quote, box, confidence.
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    explanation: Mapped[str] = mapped_column(Text)


class BusinessRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A configured rule: an evaluator (rule_type) with validated parameters."""

    __tablename__ = "business_rules"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_business_rules_updated_by_id", "updated_by_id"),
    )

    code: Mapped[str] = mapped_column(String(60), unique=True)
    rule_type: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    applies_to: Mapped[list[str]] = mapped_column(ARRAY(String(30)))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    severity: Mapped[RuleSeverity] = mapped_column(str_enum(RuleSeverity, "severity"))
    is_enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    version: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )

    updated_by: Mapped[User | None] = relationship(lazy="joined")


class RuleResultRecord(UUIDPrimaryKeyMixin, Base):
    """The latest outcome of one rule for one document (replaced on every evaluation)."""

    __tablename__ = "rule_results"
    __table_args__ = (
        Index("ix_rule_results_document_id", "document_id"),
        Index("ix_rule_results_rule_id", "rule_id"),
        Index("ix_rule_results_comparison_id", "comparison_id"),
        Index("ix_rule_results_version_id", "document_version_id"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    comparison_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("comparisons.id", ondelete="SET NULL")
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("business_rules.id", ondelete="CASCADE"))
    rule_code: Mapped[str] = mapped_column(String(60))
    rule_version: Mapped[int]
    outcome: Mapped[RuleOutcome] = mapped_column(str_enum(RuleOutcome, "outcome"))
    severity: Mapped[RuleSeverity] = mapped_column(str_enum(RuleSeverity, "severity"))
    message: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Comparison item keys behind the outcome.
    items: Mapped[list[str]] = mapped_column(JSONB, default=list)
    evaluated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReviewTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The human review queue: one open task per document, listing everything to look at."""

    __tablename__ = "review_tasks"
    __table_args__ = (
        Index(
            "uq_review_tasks_open_document",
            "document_id",
            unique=True,
            postgresql_where=text("status IN ('OPEN', 'IN_PROGRESS')"),
        ),
        Index("ix_review_tasks_status_priority_created", "status", "priority", "created_at"),
        Index("ix_review_tasks_document_id", "document_id"),
        Index("ix_review_tasks_version_id", "document_version_id"),
        Index("ix_review_tasks_assigned_to_id", "assigned_to_id"),
        Index("ix_review_tasks_resolved_by_id", "resolved_by_id"),
        CheckConstraint(
            "(status IN ('RESOLVED', 'CANCELLED')) = (resolved_at IS NOT NULL)",
            name="resolved_at_when_closed",
        ),
        CheckConstraint(
            "(status = 'RESOLVED') = (resolution IS NOT NULL)", name="resolution_when_resolved"
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    task_type: Mapped[ReviewTaskType] = mapped_column(
        str_enum(ReviewTaskType, "task_type", length=30)
    )
    status: Mapped[ReviewTaskStatus] = mapped_column(
        str_enum(ReviewTaskStatus, "status"), default=ReviewTaskStatus.OPEN
    )
    priority: Mapped[ReviewPriority] = mapped_column(str_enum(ReviewPriority, "priority"))
    # [{"key", "category", "code", "severity", "message"}] - what the reviewer should look at.
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # Stable keys of those findings: a finding a person already resolved does not reopen a task.
    reason_keys: Mapped[list[str]] = mapped_column(ARRAY(String(300)), default=list)
    due_at: Mapped[datetime | None]
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    claimed_at: Mapped[datetime | None]
    resolution: Mapped[ReviewResolution | None] = mapped_column(
        str_enum(ReviewResolution, "resolution")
    )
    resolution_note: Mapped[str | None] = mapped_column(String(1000))
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    resolved_at: Mapped[datetime | None]

    assigned_to: Mapped[User | None] = relationship(foreign_keys=[assigned_to_id], lazy="joined")
    resolved_by: Mapped[User | None] = relationship(foreign_keys=[resolved_by_id], lazy="joined")


class ReviewRequest(UUIDPrimaryKeyMixin, Base):
    """A request (by a user or an investigation) to have a document version reviewed.

    Requests are review items like rule findings: they keep the version's task open until a
    person resolves it, survive re-evaluations, and stop counting once a resolution covers them.
    """

    __tablename__ = "review_requests"
    __table_args__ = (
        Index("ix_review_requests_document_version", "document_id", "document_version_id"),
        Index("ix_review_requests_version_id", "document_version_id"),
        Index("ix_review_requests_requested_by_id", "requested_by_id"),
        Index("ix_review_requests_agent_run_id", "agent_run_id"),
        CheckConstraint("char_length(reason) BETWEEN 1 AND 1000", name="reason_length"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    requested_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    priority: Mapped[ReviewPriority] = mapped_column(str_enum(ReviewPriority, "priority"))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )
