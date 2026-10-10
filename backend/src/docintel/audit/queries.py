"""Reading the audit trail (GET /audit-logs, `audit:read`).

Administrators read every event. Managers read the events of their department: those its
members caused, and those about its documents (directly, or through `details.document_id`).
Network details (IP address, user agent) are shown to administrators only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, String, and_, cast, false, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.db.models import AuditLog, AuditOutcome, Document, Role, User


@dataclass(slots=True)
class AuditFilters:
    actor_id: uuid.UUID | None = None
    action: str | None = None  # exact, or a prefix when it ends with "." (e.g. "workflow.")
    entity_type: str | None = None
    entity_id: str | None = None
    outcome: AuditOutcome | None = None
    since: datetime | None = None
    until: datetime | None = None


def visible_events(actor: User) -> ColumnElement[bool]:
    if actor.role == Role.ADMIN:
        return true()
    if actor.role != Role.MANAGER:
        return false()
    if actor.department_id is None:
        return AuditLog.actor_id == actor.id
    members = select(User.id).where(User.department_id == actor.department_id)
    documents = select(cast(Document.id, String)).where(
        Document.department_id == actor.department_id
    )
    return or_(
        AuditLog.actor_id.in_(members),
        and_(AuditLog.entity_type == "document", AuditLog.entity_id.in_(documents)),
        AuditLog.details["document_id"].astext.in_(documents),
    )


class AuditLogReader:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def events(
        self,
        actor: User,
        filters: AuditFilters,
        *,
        limit: int,
        before_id: int | None = None,
    ) -> tuple[list[tuple[AuditLog, User | None]], int | None]:
        """Newest first; returns the page and the id to pass as `before_id` for the next one."""
        conditions: list[ColumnElement[bool]] = [visible_events(actor)]
        if filters.actor_id is not None:
            conditions.append(AuditLog.actor_id == filters.actor_id)
        if filters.action:
            if filters.action.endswith("."):
                conditions.append(AuditLog.action.startswith(filters.action, autoescape=True))
            else:
                conditions.append(AuditLog.action == filters.action)
        if filters.entity_type:
            conditions.append(AuditLog.entity_type == filters.entity_type)
        if filters.entity_id:
            conditions.append(AuditLog.entity_id == filters.entity_id)
        if filters.outcome is not None:
            conditions.append(AuditLog.outcome == filters.outcome)
        if filters.since is not None:
            conditions.append(AuditLog.occurred_at >= filters.since)
        if filters.until is not None:
            conditions.append(AuditLog.occurred_at < filters.until)
        if before_id is not None:
            conditions.append(AuditLog.id < before_id)
        rows = (
            await self._session.execute(
                select(AuditLog, User)
                .outerjoin(User, User.id == AuditLog.actor_id)
                .where(*conditions)
                .order_by(AuditLog.id.desc())
                .limit(limit + 1)
            )
        ).all()
        page: list[tuple[AuditLog, User | None]] = [(row[0], row[1]) for row in rows[:limit]]
        next_before = page[-1][0].id if len(rows) > limit and page else None
        return page, next_before
