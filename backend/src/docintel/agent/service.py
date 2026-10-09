"""Starting and reading investigations (the /analysis API)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.agent.state import GRAPH_VERSION
from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.policies import visible_documents
from docintel.core.config import Settings
from docintel.core.errors import NotFoundError, TooManyRequestsError
from docintel.db.models import (
    AgentRun,
    AgentRunStatus,
    AgentRunType,
    AgentToolCall,
    AuditOutcome,
    Document,
    JobType,
    Role,
    User,
)
from docintel.workers.queue import enqueue_job

NOT_FOUND = "Investigation not found."
ACTIVE = (AgentRunStatus.QUEUED, AgentRunStatus.RUNNING)


def query_fingerprint(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


class AnalysisService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    @staticmethod
    def _visible(actor: User) -> ColumnElement[bool]:
        """Runs are personal: their requester sees them; administrators see all."""
        if actor.role == Role.ADMIN:
            return true()
        return AgentRun.requested_by_id == actor.id

    async def start(
        self,
        actor: User,
        *,
        query: str,
        document_ids: Sequence[uuid.UUID],
        allow_safe_actions: bool,
        meta: RequestMeta,
    ) -> AgentRun:
        active = await self._session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.requested_by_id == actor.id, AgentRun.status.in_(ACTIVE))
        )
        limit = self._settings.agent_max_active_runs_per_user
        if int(active or 0) >= limit:
            msg = (
                f"You already have {limit} investigations queued or running; "
                "wait for one to finish."
            )
            raise TooManyRequestsError(msg, headers={"Retry-After": "30"})
        ids = list(dict.fromkeys(document_ids))
        if ids:
            visible = set(
                await self._session.scalars(
                    select(Document.id).where(Document.id.in_(ids), visible_documents(actor))
                )
            )
            missing = [str(document_id) for document_id in ids if document_id not in visible]
            if missing:
                raise NotFoundError(f"Documents not found: {', '.join(missing[:5])}.")
        run = AgentRun(
            id=uuid.uuid4(),
            run_type=AgentRunType.INVESTIGATION,
            status=AgentRunStatus.QUEUED,
            query=query,
            document_ids=ids,
            options={"allow_safe_actions": allow_safe_actions},
            requested_by_id=actor.id,
            graph_version=GRAPH_VERSION,
        )
        self._session.add(run)
        await self._session.flush()
        await enqueue_job(
            self._session,
            job_type=JobType.AGENT_ANALYSIS,
            max_attempts=1,
            agent_run_id=run.id,
            requested_by_id=actor.id,
        )
        record_audit_event(
            self._session,
            action=AuditAction.ANALYSIS_REQUESTED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="agent_run",
            entity_id=run.id,
            details={
                # The request may itself hold sensitive text: only its fingerprint is kept.
                "query_sha256": query_fingerprint(query),
                "query_chars": len(query),
                "documents": [str(document_id) for document_id in ids],
                "allow_safe_actions": allow_safe_actions,
                "graph_version": GRAPH_VERSION,
            },
        )
        await self._session.commit()
        await self._session.refresh(run)
        return run

    async def runs(
        self,
        actor: User,
        *,
        status: AgentRunStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[AgentRun], int]:
        conditions = [self._visible(actor)]
        if status is not None:
            conditions.append(AgentRun.status == status)
        total = await self._session.scalar(
            select(func.count()).select_from(AgentRun).where(*conditions)
        )
        rows = await self._session.scalars(
            select(AgentRun)
            .where(*conditions)
            .order_by(AgentRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def get(self, actor: User, run_id: uuid.UUID) -> AgentRun:
        run: AgentRun | None = await self._session.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id, self._visible(actor))
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise NotFoundError(NOT_FOUND)
        return run

    async def tool_calls(self, run: AgentRun) -> list[AgentToolCall]:
        rows = await self._session.scalars(
            select(AgentToolCall)
            .where(AgentToolCall.run_id == run.id)
            .order_by(AgentToolCall.created_at)
        )
        return list(rows)
