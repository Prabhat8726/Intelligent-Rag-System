"""Review queue reads: the tasks a reviewer can see, most urgent first."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.auth.policies import visible_documents
from docintel.core.errors import NotFoundError
from docintel.db.models import (
    OPEN_TASK_STATUSES,
    Document,
    ReviewPriority,
    ReviewTask,
    ReviewTaskStatus,
    ReviewTaskType,
    User,
)

NOT_FOUND = "Review task not found."
_RANK = case(
    {
        ReviewPriority.URGENT: 0,
        ReviewPriority.HIGH: 1,
        ReviewPriority.NORMAL: 2,
        ReviewPriority.LOW: 3,
    },
    value=ReviewTask.priority,
)


@dataclass(slots=True)
class ReviewFilters:
    state: Literal["open", "closed", "all"] = "open"
    task_type: ReviewTaskType | None = None
    priority: ReviewPriority | None = None
    assigned: Literal["any", "me", "unassigned"] = "any"
    document_id: uuid.UUID | None = None
    overdue: bool = False


class ReviewQueue:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def tasks(
        self, actor: User, filters: ReviewFilters, *, limit: int, offset: int
    ) -> tuple[list[tuple[ReviewTask, Document]], int]:
        conditions = [visible_documents(actor)]
        if filters.state == "open":
            conditions.append(ReviewTask.status.in_(OPEN_TASK_STATUSES))
        elif filters.state == "closed":
            conditions.append(
                ReviewTask.status.in_((ReviewTaskStatus.RESOLVED, ReviewTaskStatus.CANCELLED))
            )
        if filters.task_type is not None:
            conditions.append(ReviewTask.task_type == filters.task_type)
        if filters.priority is not None:
            conditions.append(ReviewTask.priority == filters.priority)
        if filters.assigned == "me":
            conditions.append(ReviewTask.assigned_to_id == actor.id)
        elif filters.assigned == "unassigned":
            conditions.append(ReviewTask.assigned_to_id.is_(None))
        if filters.document_id is not None:
            conditions.append(ReviewTask.document_id == filters.document_id)
        if filters.overdue:
            conditions.append(ReviewTask.status.in_(OPEN_TASK_STATUSES))
            conditions.append(ReviewTask.due_at < datetime.now(UTC))
        where = and_(*conditions)
        base = select(ReviewTask, Document).join(Document, Document.id == ReviewTask.document_id)
        total = await self._session.scalar(
            select(func.count())
            .select_from(ReviewTask)
            .join(Document, Document.id == ReviewTask.document_id)
            .where(where)
        )
        rows = (
            await self._session.execute(
                base.where(where)
                .order_by(
                    # Open work first, then urgency and due date; history newest first.
                    ReviewTask.status.in_(OPEN_TASK_STATUSES).desc(),
                    _RANK,
                    ReviewTask.due_at.asc().nulls_last(),
                    ReviewTask.created_at.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return [(task, document) for task, document in rows], int(total or 0)

    async def get(
        self, actor: User, task_id: uuid.UUID, *, fresh: bool = False
    ) -> tuple[ReviewTask, Document]:
        """`fresh`: reload from the database (after a change in this session)."""
        statement = (
            select(ReviewTask, Document)
            .join(Document, Document.id == ReviewTask.document_id)
            .where(ReviewTask.id == task_id, visible_documents(actor))
        )
        if fresh:
            statement = statement.execution_options(populate_existing=True)
        row = (await self._session.execute(statement)).first()
        if row is None:
            raise NotFoundError(NOT_FOUND)
        return row[0], row[1]

    async def history(self, document_id: uuid.UUID) -> list[ReviewTask]:
        return list(
            await self._session.scalars(
                select(ReviewTask)
                .where(ReviewTask.document_id == document_id)
                .order_by(ReviewTask.created_at.desc())
            )
        )
