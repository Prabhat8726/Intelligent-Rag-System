"""Review task lifecycle: sync with findings, claim, release, resolve."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.policies import visible_documents
from docintel.core.config import Settings
from docintel.core.errors import ConflictError, NotFoundError, UnprocessableContentError
from docintel.db.models import (
    HUMAN_RESOLUTIONS,
    OPEN_TASK_STATUSES,
    AuditOutcome,
    Document,
    DocumentStatus,
    ReviewPriority,
    ReviewRequest,
    ReviewResolution,
    ReviewTask,
    ReviewTaskStatus,
    Role,
    User,
)
from docintel.matching.store import lock_department
from docintel.review.items import ReviewItem, ordered, priority, request_item, task_type

# Statuses a document can be in when its review state is (re)decided.
_REVIEWABLE = (DocumentStatus.PROCESSING, DocumentStatus.COMPLETED, DocumentStatus.REVIEW_REQUIRED)
# Documents a review can be requested for: processed (a running job would supersede the task).
_REQUESTABLE = (DocumentStatus.COMPLETED, DocumentStatus.REVIEW_REQUIRED)
REQUESTABLE_PRIORITIES = (ReviewPriority.HIGH, ReviewPriority.NORMAL, ReviewPriority.LOW)
CLEARED_NOTE = "Every finding was cleared (a correction, a new related document or a rule change)."


def _now() -> datetime:
    return datetime.now(UTC)


async def open_task(session: AsyncSession, document_id: uuid.UUID) -> ReviewTask | None:
    task: ReviewTask | None = await session.scalar(
        select(ReviewTask)
        .where(ReviewTask.document_id == document_id, ReviewTask.status.in_(OPEN_TASK_STATUSES))
        .with_for_update(of=ReviewTask)
    )
    return task


async def requested_items(session: AsyncSession, document: Document) -> list[ReviewItem]:
    """Review requests for the document's current version, as review items."""
    if document.current_version_id is None:
        return []
    rows = await session.scalars(
        select(ReviewRequest)
        .where(
            ReviewRequest.document_id == document.id,
            ReviewRequest.document_version_id == document.current_version_id,
        )
        .order_by(ReviewRequest.created_at)
    )
    return [request_item(str(row.id), row.priority, row.reason) for row in rows]


def _close(task: ReviewTask, status: ReviewTaskStatus, now: datetime) -> None:
    task.status = status
    task.resolved_at = now
    task.updated_at = now


async def sync_review(
    session: AsyncSession,
    document: Document,
    items: Sequence[ReviewItem],
    *,
    settings: Settings,
    actor: User | None = None,
) -> ReviewTask | None:
    """Make the document's open task (and status) reflect `items` plus the review requests for
    its current version. Returns the open task."""
    now = _now()
    items = [*items, *await requested_items(session, document)]
    version_id = document.current_version_id
    task = await open_task(session, document.id)
    if task is not None and task.document_version_id != version_id:
        _close(task, ReviewTaskStatus.CANCELLED, now)  # replaced by a new version
        task.resolution_note = "Superseded by a new version of the document."
        await session.flush()
        task = None
    acknowledged: set[str] = set()
    for keys in await session.scalars(
        select(ReviewTask.reason_keys).where(
            ReviewTask.document_id == document.id,
            ReviewTask.document_version_id == version_id,
            ReviewTask.status == ReviewTaskStatus.RESOLVED,
            ReviewTask.resolution.in_(HUMAN_RESOLUTIONS),
        )
    ):
        acknowledged.update(keys)
    pending = ordered([item for item in items if item.key not in acknowledged])

    if document.deleted_at is None and document.status in _REVIEWABLE:
        document.status = DocumentStatus.REVIEW_REQUIRED if pending else DocumentStatus.COMPLETED

    if not pending:
        if task is not None:
            _close(task, ReviewTaskStatus.RESOLVED, now)
            task.resolution = ReviewResolution.CLEARED
            task.resolution_note = CLEARED_NOTE
            task.resolved_by_id = actor.id if actor else None
        return None

    level = priority(pending)
    due = now + timedelta(hours=settings.review_sla_hours[level.value])
    if task is None:
        task = ReviewTask(
            document_id=document.id,
            document_version_id=version_id,
            status=ReviewTaskStatus.OPEN,
            created_at=now,
            due_at=due,
        )
        session.add(task)
    elif task.due_at is None or due < task.due_at:
        task.due_at = due  # a more urgent finding pulls the due date forward
    task.task_type = task_type(pending)
    task.priority = level
    task.reasons = [item.to_json() for item in pending]
    task.reason_keys = [item.key for item in pending]
    task.updated_at = now
    return task


async def cancel_open_task(session: AsyncSession, document: Document, note: str) -> None:
    task = await open_task(session, document.id)
    if task is not None:
        _close(task, ReviewTaskStatus.CANCELLED, _now())
        task.resolution_note = note


# ------------------------------------------------------------------------------ queue actions
class ReviewService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def _locked(self, task_id: uuid.UUID) -> ReviewTask:
        task: ReviewTask | None = await self._session.scalar(
            select(ReviewTask)
            .where(ReviewTask.id == task_id)
            .with_for_update(of=ReviewTask)
            .execution_options(populate_existing=True)
        )
        if task is None:
            raise NotFoundError("Review task not found.")
        return task

    @staticmethod
    def _require_open(task: ReviewTask) -> None:
        if task.status not in OPEN_TASK_STATUSES:
            msg = f"The task is {task.status.value.lower()}."
            raise ConflictError(msg)

    @staticmethod
    def _may_override(actor: User) -> bool:
        """Managers and admins may act on a task someone else claimed."""
        return actor.role in (Role.ADMIN, Role.MANAGER)

    async def claim(self, actor: User, task_id: uuid.UUID, meta: RequestMeta) -> ReviewTask:
        task = await self._locked(task_id)
        self._require_open(task)
        if task.assigned_to_id not in (None, actor.id) and not self._may_override(actor):
            msg = "The task is claimed by someone else."
            raise ConflictError(msg)
        previous = task.assigned_to_id
        task.assigned_to_id = actor.id
        task.claimed_at = _now()
        task.status = ReviewTaskStatus.IN_PROGRESS
        task.updated_at = task.claimed_at
        record_audit_event(
            self._session,
            action=AuditAction.REVIEW_TASK_CLAIMED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="review_task",
            entity_id=task.id,
            details={
                "document_id": str(task.document_id),
                "taken_over_from": str(previous) if previous and previous != actor.id else None,
            },
        )
        return task

    async def release(self, actor: User, task_id: uuid.UUID, meta: RequestMeta) -> ReviewTask:
        task = await self._locked(task_id)
        self._require_open(task)
        if task.assigned_to_id not in (None, actor.id) and not self._may_override(actor):
            msg = "The task is claimed by someone else."
            raise ConflictError(msg)
        task.assigned_to_id = None
        task.claimed_at = None
        task.status = ReviewTaskStatus.OPEN
        task.updated_at = _now()
        record_audit_event(
            self._session,
            action=AuditAction.REVIEW_TASK_RELEASED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="review_task",
            entity_id=task.id,
            details={"document_id": str(task.document_id)},
        )
        return task

    async def resolve(
        self,
        actor: User,
        task_id: uuid.UUID,
        resolution: ReviewResolution,
        note: str | None,
        meta: RequestMeta,
    ) -> ReviewTask:
        if resolution not in HUMAN_RESOLUTIONS:
            msg = "Resolve a task as APPROVED, CORRECTED or REJECTED."
            raise UnprocessableContentError(msg)
        if resolution == ReviewResolution.REJECTED and not (note and note.strip()):
            msg = "A rejection needs a note explaining why."
            raise UnprocessableContentError(msg)
        # Same lock order as matching: department, document, then the task.
        department_id = await self._session.scalar(
            select(Document.department_id)
            .join(ReviewTask, ReviewTask.document_id == Document.id)
            .where(ReviewTask.id == task_id)
        )
        await lock_department(self._session, department_id)
        document = await self._session.scalar(
            select(Document)
            .join(ReviewTask, ReviewTask.document_id == Document.id)
            .where(ReviewTask.id == task_id)
            .with_for_update(of=Document)
            .execution_options(populate_existing=True)
        )
        task = await self._locked(task_id)
        self._require_open(task)
        if task.assigned_to_id not in (None, actor.id) and not self._may_override(actor):
            msg = "The task is claimed by someone else."
            raise ConflictError(msg)
        if document is None:  # pragma: no cover - cascades with the task
            raise NotFoundError("Document not found.")
        now = _now()
        _close(task, ReviewTaskStatus.RESOLVED, now)
        task.resolution = resolution
        task.resolution_note = note.strip() if note else None
        task.resolved_by_id = actor.id
        if task.assigned_to_id is None:
            task.assigned_to_id = actor.id
        if document.deleted_at is None and document.status == DocumentStatus.REVIEW_REQUIRED:
            document.status = DocumentStatus.COMPLETED
        record_audit_event(
            self._session,
            action=AuditAction.REVIEW_TASK_RESOLVED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="review_task",
            entity_id=task.id,
            details={
                "document_id": str(task.document_id),
                "resolution": resolution.value,
                "task_type": task.task_type.value,
                "findings": [reason.get("code") for reason in task.reasons],
                "note": bool(note),
            },
        )
        return task


@dataclass(slots=True)
class RequestOutcome:
    task: ReviewTask | None  # None: a person already resolved this very request
    request: ReviewRequest
    created: bool  # False: the same request was already on file


class ReviewRequestService:
    """Ask for a human review of a document (REST users and the agent's create_review_task)."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def request(
        self,
        actor: User,
        document_id: uuid.UUID,
        *,
        reason: str,
        priority_level: ReviewPriority,
        meta: RequestMeta,
        agent_run_id: uuid.UUID | None = None,
    ) -> RequestOutcome:
        """Add the request to the document's open task (or open one). The caller commits."""
        reason = " ".join(reason.split())
        if not reason:
            msg = "A review request needs a reason."
            raise UnprocessableContentError(msg)
        if priority_level not in REQUESTABLE_PRIORITIES:
            msg = "Request a review with priority HIGH, NORMAL or LOW."
            raise UnprocessableContentError(msg)
        # Same lock order as matching: department, then the document.
        found = (
            await self._session.execute(
                select(Document.id, Document.department_id).where(
                    Document.id == document_id, visible_documents(actor)
                )
            )
        ).first()
        if found is None:
            raise NotFoundError("Document not found.")
        await lock_department(self._session, found.department_id)
        document = await self._session.scalar(
            select(Document)
            .where(Document.id == document_id, visible_documents(actor))
            .with_for_update(of=Document)
            .execution_options(populate_existing=True)
        )
        if document is None:
            raise NotFoundError("Document not found.")
        if document.status not in _REQUESTABLE or document.current_version_id is None:
            msg = f"{document.display_filename} has not been processed yet."
            raise UnprocessableContentError(msg)

        existing = await self._session.scalar(
            select(ReviewRequest).where(
                ReviewRequest.document_id == document.id,
                ReviewRequest.document_version_id == document.current_version_id,
                ReviewRequest.requested_by_id == actor.id,
                ReviewRequest.reason == reason,
            )
        )
        task = await open_task(self._session, document.id)
        if existing is not None:
            covered = task is not None and request_item(
                str(existing.id), existing.priority, existing.reason
            ).key in (task.reason_keys or [])
            return RequestOutcome(task if covered else None, existing, created=False)

        request = ReviewRequest(
            document_id=document.id,
            document_version_id=document.current_version_id,
            requested_by_id=actor.id,
            agent_run_id=agent_run_id,
            priority=priority_level,
            reason=reason,
            created_at=_now(),
        )
        self._session.add(request)
        await self._session.flush()
        # The open task already lists the document's current findings; without one, every
        # finding was resolved by a person (or there are none), so only the requests remain.
        current = [
            ReviewItem.from_json(item)
            for item in (task.reasons if task is not None else [])
            if item.get("category") != "REQUESTED"
        ]
        task = await sync_review(
            self._session, document, current, settings=self._settings, actor=actor
        )
        await self._session.flush()
        record_audit_event(
            self._session,
            action=AuditAction.REVIEW_REQUESTED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="document",
            entity_id=document.id,
            details={
                "review_request_id": str(request.id),
                "review_task_id": str(task.id) if task else None,
                "priority": priority_level.value,
                "agent_run_id": str(agent_run_id) if agent_run_id else None,
                # The reason may quote document content: only its size is kept here.
                "reason_chars": len(reason),
            },
        )
        return RequestOutcome(task, request, created=True)
