"""Workflows run in the worker (WORKFLOW jobs, one attempt each).

The job runs the steps in order, each in its own transaction, until the workflow needs a
person (an action awaiting approval) or ends. The investigation step is the Phase 7 graph,
called as the person who started the workflow, with safe actions off: the workflow - not the
investigation - turns the recommendation into its action. Approval resumes the workflow in the
API (`WorkflowService.approve`), not in a paused graph (ADR-005).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.agent.graph import AgentDeps, RunContext
from docintel.agent.runner import investigate, record_run_failure, record_run_result
from docintel.agent.state import GRAPH_VERSION
from docintel.audit.service import SYSTEM_REQUEST, AuditAction, record_audit_event
from docintel.core.config import Settings
from docintel.core.errors import ConflictError, NotFoundError
from docintel.core.logging import get_logger
from docintel.db.models import (
    FINISHED_WORKFLOW_STATUSES,
    OPEN_TASK_STATUSES,
    ActionStatus,
    ActorType,
    AgentRun,
    AgentRunStatus,
    AgentRunType,
    AuditOutcome,
    Document,
    DocumentStatus,
    DocumentVersion,
    JobStatus,
    ProposerType,
    ReviewTask,
    StepStatus,
    User,
    Workflow,
    WorkflowAction,
    WorkflowStatus,
)
from docintel.documents.versions import VersionService
from docintel.processing.pipeline import PermanentProcessingError
from docintel.workers.queue import ClaimedJob
from docintel.workflows.actions import carry_out, finish, now, set_step, step_of, transition
from docintel.workflows.definitions import (
    APPROVAL,
    CHECK_DOCUMENT,
    COMPARE_VERSIONS,
    EXECUTE_ACTION,
    INVESTIGATE,
    PROPOSE_ACTION,
    REPORT,
    definition_for,
)
from docintel.workflows.policy import decide

logger = get_logger(__name__)

PROCESSED = (DocumentStatus.COMPLETED, DocumentStatus.REVIEW_REQUIRED)
# Steps the job does not run itself: they follow a proposal or a decision.
_DECISION_STEPS = (APPROVAL, EXECUTE_ACTION, REPORT)
MAX_CHANGED_CLAUSES = 15


async def makers_of(session: AsyncSession, workflow: Workflow) -> list[uuid.UUID]:
    """Who may not approve the workflow's action: whoever started it, uploaded the document or
    the version it verifies (Approval Matrix 1.1: the person who enters an invoice or proposes
    a payment cannot approve it)."""
    owner = await session.scalar(
        select(Document.owner_id).where(Document.id == workflow.document_id)
    )
    uploader = await session.scalar(
        select(DocumentVersion.uploaded_by_id).where(
            DocumentVersion.id == workflow.document_version_id
        )
    )
    makers = [workflow.initiated_by_id, owner, uploader]
    return list(dict.fromkeys(maker for maker in makers if maker is not None))


@dataclass(slots=True)
class WorkflowJob:
    job: ClaimedJob
    workflow_id: uuid.UUID
    stage_timings: dict[str, float] = field(default_factory=dict)


class WorkflowRunner:
    def __init__(
        self,
        deps: AgentDeps,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._deps = deps
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._steps: dict[str, Callable[[uuid.UUID], Awaitable[None]]] = {
            CHECK_DOCUMENT: self.check_document,
            COMPARE_VERSIONS: self.compare_versions,
            INVESTIGATE: self.investigate,
            PROPOSE_ACTION: self.propose_action,
        }

    async def _locked(self, session: AsyncSession, workflow_id: uuid.UUID) -> Workflow:
        workflow: Workflow | None = await session.scalar(
            select(Workflow)
            .where(Workflow.id == workflow_id)
            .with_for_update(of=Workflow)
            .execution_options(populate_existing=True)
        )
        if workflow is None:
            raise PermanentProcessingError("The workflow no longer exists.")
        return workflow

    async def run(
        self,
        workflow_id: uuid.UUID,
        on_stage: Callable[[str, dict[str, Any]], Awaitable[None]],
        timings: dict[str, float],
    ) -> None:
        while True:
            async with self._sessionmaker() as session, session.begin():
                workflow = await self._locked(session, workflow_id)
                if workflow.status != WorkflowStatus.RUNNING:
                    return  # waiting for a person, finished or cancelled
                step = next((s for s in workflow.steps if s.status == StepStatus.PENDING), None)
                if step is None or step.step_name in _DECISION_STEPS:
                    return
                name = step.step_name
                set_step(workflow, name, StepStatus.RUNNING)
            await on_stage(name, timings)
            started = time.perf_counter()
            await self._steps[name](workflow_id)
            timings[name] = round((time.perf_counter() - started) * 1000, 2)

    # ------------------------------------------------------------------ steps
    async def check_document(self, workflow_id: uuid.UUID) -> None:
        async with self._sessionmaker() as session, session.begin():
            workflow = await self._locked(session, workflow_id)
            definition = definition_for(workflow.workflow_type)
            document = await session.get(Document, workflow.document_id, populate_existing=True)
            initiator = await session.get(User, workflow.initiated_by_id)
            if document is None or document.deleted_at is not None:
                raise PermanentProcessingError("The document was deleted.")
            if initiator is None or not initiator.is_active:
                raise PermanentProcessingError("The person who started the workflow is inactive.")
            if document.current_version_id != workflow.document_version_id:
                msg = "A newer version of the document was uploaded; start a new workflow."
                raise PermanentProcessingError(msg)
            if document.status not in PROCESSED:
                raise PermanentProcessingError("The document has not finished processing.")
            if document.document_type != definition.document_type:
                kind = document.document_type.value if document.document_type else "unclassified"
                msg = (
                    f"{definition.title} needs a {definition.document_type.value.lower()}; "
                    f"the document is {kind.lower()}."
                )
                raise PermanentProcessingError(msg)
            version = await session.get(DocumentVersion, workflow.document_version_id)
            set_step(
                workflow,
                CHECK_DOCUMENT,
                StepStatus.COMPLETED,
                output={
                    "document_type": document.document_type.value,
                    "status": document.status.value,
                    "version_number": version.version_number if version else None,
                    "review_reasons": list(document.review_reasons or []),
                },
            )

    async def compare_versions(self, workflow_id: uuid.UUID) -> None:
        async with self._sessionmaker() as session, session.begin():
            workflow = await self._locked(session, workflow_id)
            document = await session.get(Document, workflow.document_id)
            version = await session.get(DocumentVersion, workflow.document_version_id)
            if document is None or version is None or version.version_number <= 1:
                set_step(
                    workflow,
                    COMPARE_VERSIONS,
                    StepStatus.SKIPPED,
                    output={"reason": "This is the first version."},
                )
                return
            previous = version.version_number - 1
            try:
                compared = await VersionService(session).compare(
                    document, previous, version.version_number
                )
            except (ConflictError, NotFoundError) as exc:
                set_step(
                    workflow, COMPARE_VERSIONS, StepStatus.SKIPPED, output={"reason": str(exc)}
                )
                return
            summary = compared["summary"]
            changed = [c for c in compared["clauses"] if c["change"] != "UNCHANGED"]
            set_step(
                workflow,
                COMPARE_VERSIONS,
                StepStatus.COMPLETED,
                output={
                    "from_version": previous,
                    "to_version": version.version_number,
                    "summary": summary,
                    "changed": len(changed),
                    "clauses": [
                        {"title": c["title"], "change": c["change"]}
                        for c in changed[:MAX_CHANGED_CLAUSES]
                    ],
                },
            )

    async def investigate(self, workflow_id: uuid.UUID) -> None:
        async with self._sessionmaker() as session, session.begin():
            workflow = await self._locked(session, workflow_id)
            definition = definition_for(workflow.workflow_type)
            run = AgentRun(
                id=uuid.uuid4(),
                run_type=AgentRunType.INVESTIGATION,
                status=AgentRunStatus.RUNNING,
                query=definition.query,
                document_ids=[workflow.document_id],
                options={"allow_safe_actions": False, "workflow_id": str(workflow.id)},
                requested_by_id=workflow.initiated_by_id,
                graph_version=GRAPH_VERSION,
                started_at=now(),
            )
            session.add(run)
            workflow.agent_run_id = run.id
            user_id, document_id = workflow.initiated_by_id, workflow.document_id
        context = RunContext(self._deps, run.id, user_id)
        try:
            state = await investigate(
                context,
                query=definition.query,
                document_ids=[document_id],
                allow_safe_actions=False,
            )
        except Exception as exc:
            message = (
                "The investigation exceeded its time limit (AGENT_TIMEOUT_SECONDS)."
                if isinstance(exc, TimeoutError)
                else "The investigation failed."
            )
            if not isinstance(exc, TimeoutError):
                logger.exception("workflow.investigation_failed", workflow_id=str(workflow_id))
            async with self._sessionmaker() as session, session.begin():
                failed = await session.get(AgentRun, run.id, with_for_update=True)
                if failed is not None and failed.status == AgentRunStatus.RUNNING:
                    await record_run_failure(session, failed, message)
            raise PermanentProcessingError(message) from exc
        async with self._sessionmaker() as session, session.begin():
            workflow = await self._locked(session, workflow_id)
            stored = await session.get(AgentRun, run.id, with_for_update=True)
            assert stored is not None  # noqa: S101 - created above, cascades with nothing
            result = await record_run_result(session, stored, state, context, datetime.now(UTC))
            recommendation = result["recommendation"]
            set_step(
                workflow,
                INVESTIGATE,
                StepStatus.COMPLETED,
                output={
                    "agent_run_id": str(run.id),
                    "recommendation": recommendation["action"],
                    "recommendation_source": recommendation["source"],
                    "confidence": result["confidence"]["level"],
                    "findings": len(result["findings"]),
                    "sources": len(result["sources"]),
                    "tool_calls": context.tool_calls,
                },
            )

    async def propose_action(self, workflow_id: uuid.UUID) -> None:
        async with self._sessionmaker() as session, session.begin():
            workflow = await self._locked(session, workflow_id)
            run = (
                await session.get(AgentRun, workflow.agent_run_id)
                if workflow.agent_run_id
                else None
            )
            if run is None or run.result is None:
                raise PermanentProcessingError("The investigation result is missing.")
            initiator = await session.get(User, workflow.initiated_by_id)
            assert initiator is not None  # noqa: S101 - RESTRICT foreign key
            compared = step_of(workflow, COMPARE_VERSIONS)
            changes = (
                compared.output
                if compared is not None and compared.status == StepStatus.COMPLETED
                else None
            )
            # A plain read: row locks here would come before the department lock (lock order).
            open_review = await session.scalar(
                select(ReviewTask.task_type).where(
                    ReviewTask.document_id == workflow.document_id,
                    ReviewTask.status.in_(OPEN_TASK_STATUSES),
                )
            )
            proposal = decide(
                workflow.workflow_type,
                run.result,
                document_id=str(workflow.document_id),
                version_changes=changes,
                open_review=open_review.value if open_review is not None else None,
            )
            if proposal is None:
                set_step(workflow, PROPOSE_ACTION, StepStatus.COMPLETED, output={"action": None})
                await finish(
                    session,
                    self._settings,
                    workflow,
                    action=None,
                    actor=initiator,
                    actor_type=ActorType.SYSTEM,
                    meta=SYSTEM_REQUEST,
                )
                return
            policy = proposal.policy
            action = WorkflowAction(
                id=uuid.uuid4(),
                workflow_id=workflow.id,
                document_id=workflow.document_id,
                document_version_id=workflow.document_version_id,
                action_type=proposal.action_type,
                status=ActionStatus.PROPOSED,
                risk_level=policy.risk,
                requires_approval=policy.requires_approval,
                required_role=policy.required_role.value if policy.required_role else None,
                proposed_by_type=proposal.proposer,
                proposed_by_user_id=workflow.initiated_by_id,
                agent_run_id=run.id,
                rationale=proposal.rationale,
                confidence_level=proposal.confidence_level,
                confidence_score=proposal.confidence_score,
                payload=proposal.payload,
                maker_ids=await makers_of(session, workflow),
                idempotency_key=f"{workflow.id}:{proposal.action_type.value}",
            )
            session.add(action)
            await session.flush()
            proposer_type = (
                ActorType.AGENT if proposal.proposer == ProposerType.AGENT else ActorType.SYSTEM
            )
            transition(
                session,
                action,
                ActionStatus.PROPOSED,
                previous=None,
                actor=initiator,
                actor_type=proposer_type,
                reason=proposal.rationale,
                meta=SYSTEM_REQUEST,
                details={"risk": policy.risk.value, "proposed_by": proposal.proposer.value},
            )
            set_step(
                workflow,
                PROPOSE_ACTION,
                StepStatus.COMPLETED,
                output={
                    "action_id": str(action.id),
                    "action_type": action.action_type.value,
                    "risk": policy.risk.value,
                    "requires_approval": policy.requires_approval,
                    "required_role": action.required_role,
                },
            )
            if policy.requires_approval:
                transition(
                    session,
                    action,
                    ActionStatus.AWAITING_APPROVAL,
                    previous=ActionStatus.PROPOSED,
                    actor=None,
                    actor_type=ActorType.SYSTEM,
                    reason=f"Risk {policy.risk.value}: needs the approval of a "
                    f"{action.required_role} who did not start the workflow or upload the "
                    "document.",
                    meta=SYSTEM_REQUEST,
                )
                workflow.status = WorkflowStatus.AWAITING_APPROVAL
                set_step(
                    workflow,
                    APPROVAL,
                    StepStatus.RUNNING,
                    output={"action_id": str(action.id), "required_role": action.required_role},
                )
                return
            action.decided_at = now()
            transition(
                session,
                action,
                ActionStatus.APPROVED,
                previous=ActionStatus.PROPOSED,
                actor=None,
                actor_type=ActorType.SYSTEM,
                reason=f"Risk {policy.risk.value}: carried out without approval (risk table).",
                meta=SYSTEM_REQUEST,
            )
            set_step(
                workflow,
                APPROVAL,
                StepStatus.SKIPPED,
                output={"reason": f"Risk {policy.risk.value}: no approval needed."},
            )
            await carry_out(
                session,
                self._settings,
                workflow,
                action,
                actor=initiator,
                actor_type=ActorType.SYSTEM,
                meta=SYSTEM_REQUEST,
            )
            await finish(
                session,
                self._settings,
                workflow,
                action=action,
                actor=initiator,
                actor_type=ActorType.SYSTEM,
                meta=SYSTEM_REQUEST,
            )


class WorkflowHandler:
    """Job handler for JobType.WORKFLOW."""

    def __init__(
        self,
        deps: AgentDeps,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._runner = WorkflowRunner(deps, sessionmaker, settings)

    async def prepare(self, session: AsyncSession, job: ClaimedJob) -> WorkflowJob | None:
        if job.workflow_id is None:
            return None
        workflow = await session.scalar(
            select(Workflow).where(Workflow.id == job.workflow_id).with_for_update(of=Workflow)
        )
        if workflow is None or workflow.status != WorkflowStatus.QUEUED:
            return None  # cancelled (or already run)
        workflow.status = WorkflowStatus.RUNNING
        workflow.started_at = now()
        workflow.updated_at = workflow.started_at
        return WorkflowJob(job=job, workflow_id=workflow.id)

    async def execute(
        self, context: WorkflowJob, on_stage: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        await self._runner.run(context.workflow_id, on_stage, context.stage_timings)

    async def on_success(
        self, session: AsyncSession, context: WorkflowJob, finished_at: datetime
    ) -> None:
        workflow = await session.get(Workflow, context.workflow_id)
        if workflow is not None:
            logger.info(
                "workflow.job_completed",
                workflow_id=str(workflow.id),
                status=workflow.status.value,
                outcome=workflow.outcome,
            )

    async def on_failure(
        self, session: AsyncSession, job: ClaimedJob, *, new_status: Any, user_message: str
    ) -> None:
        if job.workflow_id is None or new_status != JobStatus.FAILED:
            return
        workflow = await session.scalar(
            select(Workflow)
            .where(Workflow.id == job.workflow_id)
            .with_for_update(of=Workflow)
            .execution_options(populate_existing=True)
        )
        if workflow is None or workflow.status in FINISHED_WORKFLOW_STATUSES:
            return
        for step in workflow.steps:
            if step.status == StepStatus.RUNNING:
                set_step(workflow, step.step_name, StepStatus.FAILED, error=user_message)
            elif step.status == StepStatus.PENDING:
                set_step(workflow, step.step_name, StepStatus.SKIPPED)
        stamp = now()
        workflow.status = WorkflowStatus.FAILED
        workflow.outcome = "WORKFLOW_FAILED"
        workflow.error = user_message[:1000]
        workflow.finished_at = stamp
        workflow.updated_at = stamp
        workflow.current_step = None
        initiator = await session.get(User, workflow.initiated_by_id)
        record_audit_event(
            session,
            action=AuditAction.WORKFLOW_FAILED,
            outcome=AuditOutcome.FAILURE,
            meta=SYSTEM_REQUEST,
            actor=initiator,
            actor_type=ActorType.WORKER,
            entity_type="workflow",
            entity_id=workflow.id,
            details={
                "workflow_type": workflow.workflow_type.value,
                "document_id": str(workflow.document_id),
                "error": user_message[:300],
            },
        )
