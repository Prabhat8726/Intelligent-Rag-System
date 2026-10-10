"""The dashboard (Module 19): what is happening to the documents the caller can see.

Every figure is an aggregate computed live from the operational tables, scoped by
`visible_documents` like every other read (ADR-064): a department sees its own documents, an
administrator everything. Investigations are personal (Phase 7): an administrator sees every
run, anyone else their own. Recent activity lists audit events about visible documents from an
allowlist of actions, without network details.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.auth.policies import visible_documents
from docintel.db.models import (
    AgentRun,
    AuditLog,
    Document,
    DocumentExtraction,
    JobStatus,
    JobType,
    ProcessingJob,
    ReviewTask,
    Role,
    RuleOutcome,
    RuleResultRecord,
    User,
    Workflow,
    WorkflowStatus,
)
from docintel.db.models.matching import OPEN_TASK_STATUSES

# Actions that describe work on a document; personal (analysis.*), security and administration
# events stay in the audit log.
ACTIVITY_ACTIONS = (
    "document.uploaded",
    "document.version.uploaded",
    "document.processing.completed",
    "document.processing.failed",
    "document.classification.corrected",
    "document.extraction.field_corrected",
    "review.requested",
    "review_task.resolved",
    "workflow.started",
    "workflow.completed",
    "workflow.rejected",
    "workflow.failed",
    "workflow.action.approved",
    "workflow.action.rejected",
)
ACTIVITY_LIMIT = 12
TOP_RULES = 8


@dataclass(slots=True)
class DailyPoint:
    day: date
    processed: int = 0
    extraction_confidence: float | None = None
    auto_accepted: int = 0


@dataclass(slots=True)
class Summary:
    days: int
    since: datetime
    until: datetime
    documents: dict[str, Any] = field(default_factory=dict)
    processing: dict[str, Any] = field(default_factory=dict)
    review_queue: dict[str, Any] = field(default_factory=dict)
    discrepancies: dict[str, Any] = field(default_factory=dict)
    investigations: dict[str, Any] = field(default_factory=dict)
    workflows: dict[str, Any] = field(default_factory=dict)
    confidence: list[DailyPoint] = field(default_factory=list)
    activity: list[dict[str, Any]] = field(default_factory=list)


def _counts(rows: Any) -> dict[str, int]:
    return {str(getattr(key, "value", key)): int(count) for key, count in rows if key is not None}


class DashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def summary(self, actor: User, *, days: int, now: datetime | None = None) -> Summary:
        until = now or datetime.now(UTC)
        since = until - timedelta(days=days)
        visible = select(Document.id).where(visible_documents(actor))
        result = Summary(days=days, since=since, until=until)
        result.documents = await self._documents(actor, since)
        result.processing = await self._processing(visible, since)
        result.review_queue = await self._review_queue(visible, until)
        result.discrepancies = await self._discrepancies(visible)
        result.investigations = await self._investigations(actor, since)
        result.workflows = await self._workflows(visible, since)
        result.confidence = await self._confidence(visible, since, until)
        result.activity = await self._activity(visible)
        return result

    async def _documents(self, actor: User, since: datetime) -> dict[str, Any]:
        scope = visible_documents(actor)
        by_status = _counts(
            await self._session.execute(
                select(Document.status, func.count()).where(scope).group_by(Document.status)
            )
        )
        by_type = _counts(
            await self._session.execute(
                select(Document.document_type, func.count())
                .where(scope)
                .group_by(Document.document_type)
            )
        )
        unclassified = await self._session.scalar(
            select(func.count()).where(scope, Document.document_type.is_(None))
        )
        uploaded = await self._session.scalar(
            select(func.count()).where(scope, Document.created_at >= since)
        )
        return {
            "total": sum(by_status.values()),
            "uploaded_in_period": int(uploaded or 0),
            "by_status": by_status,
            "by_type": {**by_type, **({"UNCLASSIFIED": int(unclassified)} if unclassified else {})},
        }

    async def _processing(self, visible: Any, since: datetime) -> dict[str, Any]:
        jobs = and_(
            ProcessingJob.job_type == JobType.DOCUMENT_PROCESSING,
            ProcessingJob.document_id.in_(visible),
            ProcessingJob.finished_at >= since,
        )
        seconds = func.extract("epoch", ProcessingJob.finished_at - ProcessingJob.started_at)
        done = (
            await self._session.execute(
                select(
                    func.count(),
                    func.avg(seconds),
                    func.percentile_cont(0.95).within_group(seconds),
                ).where(jobs, ProcessingJob.status == JobStatus.COMPLETED)
            )
        ).one()
        failed_jobs = await self._session.scalar(
            select(func.count()).where(jobs, ProcessingJob.status == JobStatus.FAILED)
        )
        return {
            "processed_in_period": int(done[0] or 0),
            "average_seconds": round(float(done[1]), 1) if done[1] is not None else None,
            "p95_seconds": round(float(done[2]), 1) if done[2] is not None else None,
            "failed_in_period": int(failed_jobs or 0),
        }

    async def _review_queue(self, visible: Any, now: datetime) -> dict[str, Any]:
        open_tasks = and_(
            ReviewTask.document_id.in_(visible), ReviewTask.status.in_(OPEN_TASK_STATUSES)
        )
        by_priority = _counts(
            await self._session.execute(
                select(ReviewTask.priority, func.count())
                .where(open_tasks)
                .group_by(ReviewTask.priority)
            )
        )
        by_type = _counts(
            await self._session.execute(
                select(ReviewTask.task_type, func.count())
                .where(open_tasks)
                .group_by(ReviewTask.task_type)
            )
        )
        overdue = await self._session.scalar(
            select(func.count()).where(open_tasks, ReviewTask.due_at < now)
        )
        return {
            "open": sum(by_priority.values()),
            "overdue": int(overdue or 0),
            "by_priority": by_priority,
            "by_type": by_type,
        }

    async def _discrepancies(self, visible: Any) -> dict[str, Any]:
        """Rule failures standing now (rule results are rebuilt on every evaluation)."""
        failing = and_(
            RuleResultRecord.document_id.in_(visible),
            RuleResultRecord.outcome == RuleOutcome.FAIL,
        )
        documents = await self._session.scalar(
            select(func.count(func.distinct(RuleResultRecord.document_id))).where(failing)
        )
        rows = (
            await self._session.execute(
                select(
                    RuleResultRecord.rule_code,
                    func.count(func.distinct(RuleResultRecord.document_id)).label("documents"),
                )
                .where(failing)
                .group_by(RuleResultRecord.rule_code)
                .order_by(func.count(func.distinct(RuleResultRecord.document_id)).desc())
                .limit(TOP_RULES)
            )
        ).all()
        return {
            "documents_failing": int(documents or 0),
            "by_rule": [{"rule_code": code, "documents": int(count)} for code, count in rows],
        }

    async def _investigations(self, actor: User, since: datetime) -> dict[str, Any]:
        scope = (
            AgentRun.created_at >= since
            if actor.role == Role.ADMIN
            else and_(AgentRun.created_at >= since, AgentRun.requested_by_id == actor.id)
        )
        by_status = _counts(
            await self._session.execute(
                select(AgentRun.status, func.count()).where(scope).group_by(AgentRun.status)
            )
        )
        action = AgentRun.result["recommendation"]["action"].astext
        by_recommendation = _counts(
            await self._session.execute(
                select(action, func.count()).where(scope, action.is_not(None)).group_by(action)
            )
        )
        return {
            "scope": "all" if actor.role == Role.ADMIN else "mine",
            "in_period": sum(by_status.values()),
            "by_status": by_status,
            "by_recommendation": by_recommendation,
        }

    async def _workflows(self, visible: Any, since: datetime) -> dict[str, Any]:
        mine = Workflow.document_id.in_(visible)
        awaiting = await self._session.scalar(
            select(func.count()).where(mine, Workflow.status == WorkflowStatus.AWAITING_APPROVAL)
        )
        outcomes = _counts(
            await self._session.execute(
                select(Workflow.outcome, func.count())
                .where(mine, Workflow.finished_at >= since)
                .group_by(Workflow.outcome)
            )
        )
        by_status = _counts(
            await self._session.execute(
                select(Workflow.status, func.count())
                .where(mine, or_(Workflow.created_at >= since, Workflow.finished_at.is_(None)))
                .group_by(Workflow.status)
            )
        )
        return {
            "awaiting_approval": int(awaiting or 0),
            "finished_in_period": sum(outcomes.values()),
            "by_outcome": outcomes,
            "by_status": by_status,
        }

    async def _confidence(self, visible: Any, since: datetime, until: datetime) -> list[DailyPoint]:
        """Per UTC day: documents extracted, their mean confidence, how many needed no review."""
        day = cast(func.timezone("UTC", DocumentExtraction.created_at), Date)
        rows = (
            await self._session.execute(
                select(
                    day,
                    func.count(),
                    func.avg(DocumentExtraction.overall_confidence),
                    func.count().filter(DocumentExtraction.review_level == "AUTO"),
                )
                .where(
                    DocumentExtraction.document_id.in_(visible),
                    DocumentExtraction.is_current.is_(True),
                    DocumentExtraction.created_at >= since,
                )
                .group_by(day)
            )
        ).all()
        found = {
            row[0]: DailyPoint(
                day=row[0],
                processed=int(row[1]),
                extraction_confidence=round(float(row[2]), 4) if row[2] is not None else None,
                auto_accepted=int(row[3]),
            )
            for row in rows
        }
        first = since.astimezone(UTC).date() + timedelta(days=1)
        last = until.astimezone(UTC).date()
        return [
            found.get(first + timedelta(days=offset), DailyPoint(first + timedelta(days=offset)))
            for offset in range((last - first).days + 1)
        ]

    async def _activity(self, visible: Any) -> list[dict[str, Any]]:
        ids = select(cast(Document.id, String)).where(Document.id.in_(visible))
        rows = (
            await self._session.execute(
                select(AuditLog, User)
                .outerjoin(User, User.id == AuditLog.actor_id)
                .where(
                    AuditLog.action.in_(ACTIVITY_ACTIONS),
                    or_(
                        and_(AuditLog.entity_type == "document", AuditLog.entity_id.in_(ids)),
                        AuditLog.details["document_id"].astext.in_(ids),
                    ),
                )
                .order_by(AuditLog.id.desc())
                .limit(ACTIVITY_LIMIT)
            )
        ).all()
        items = [_activity_item(event, user) for event, user in rows]
        wanted = {item["document_id"] for item in items if item["document_id"]}
        names = dict(
            (
                await self._session.execute(
                    select(Document.id, Document.display_filename).where(Document.id.in_(wanted))
                )
            ).all()
        )
        for item in items:
            item["document_name"] = names.get(item["document_id"])
        return items


def _activity_item(event: AuditLog, user: User | None) -> dict[str, Any]:
    details = event.details or {}
    document_id = details.get("document_id") or (
        event.entity_id if event.entity_type == "document" else None
    )
    workflow_id = details.get("workflow_id") or (
        event.entity_id if event.entity_type == "workflow" else None
    )
    return {
        "id": event.id,
        "occurred_at": event.occurred_at,
        "action": event.action,
        "outcome": event.outcome.value,
        "actor": user.full_name if user else event.actor_type.value.lower(),
        "document_id": uuid.UUID(document_id) if document_id else None,
        "workflow_id": uuid.UUID(workflow_id) if workflow_id else None,
    }
