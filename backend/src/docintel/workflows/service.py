"""Starting, reading and deciding workflows (the /workflows API and automatic starts).

A workflow is visible to whoever can see its document. Deciding its action needs
`workflows:approve`, the action's required role (or a higher one), and not being one of its
makers (who started the workflow or uploaded the document); the database enforces the last
rule as well. Every refusal to decide is audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, and_, func, not_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import SYSTEM_REQUEST, AuditAction, RequestMeta, record_audit_event
from docintel.auth.permissions import Permission, has_permission
from docintel.auth.policies import visible_documents
from docintel.core.config import Settings
from docintel.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableContentError,
)
from docintel.core.logging import get_logger
from docintel.db.models import (
    ACTIVE_WORKFLOW_STATUSES,
    ActionStatus,
    ActorType,
    AuditOutcome,
    Document,
    DocumentStatus,
    DocumentVersion,
    JobStatus,
    JobType,
    ProcessingJob,
    ReviewPriority,
    Role,
    StepStatus,
    User,
    Workflow,
    WorkflowAction,
    WorkflowStatus,
    WorkflowStep,
    WorkflowTrigger,
    WorkflowType,
)
from docintel.review.service import ReviewRequestService
from docintel.workers.queue import enqueue_job
from docintel.workflows.actions import carry_out, finish, now, set_step, transition
from docintel.workflows.definitions import (
    APPROVAL,
    EXECUTE_ACTION,
    definition_by_document_type,
    definition_for,
)
from docintel.workflows.policy import ACTION_POLICIES, role_may_approve

logger = get_logger(__name__)

NOT_FOUND = "Workflow not found."
PROCESSED = (DocumentStatus.COMPLETED, DocumentStatus.REVIEW_REQUIRED)
CANCELLABLE = (WorkflowStatus.QUEUED, WorkflowStatus.RUNNING)


def visible_workflows(actor: User) -> ColumnElement[bool]:
    """A workflow is as visible as its document."""
    return Workflow.document_id.in_(select(Document.id).where(visible_documents(actor)))


def decidable_by(actor: User) -> ColumnElement[bool]:
    """Actions awaiting a decision this person may take (role and maker-checker)."""
    roles = [
        role.value for role in (Role.REVIEWER, Role.MANAGER) if role_may_approve(actor.role, role)
    ]
    return and_(
        WorkflowAction.status == ActionStatus.AWAITING_APPROVAL,
        WorkflowAction.required_role.in_(roles),
        not_(WorkflowAction.maker_ids.contains([actor.id])),
    )


def decision_blockers(actor: User, workflow: Workflow, action: WorkflowAction) -> list[str]:
    """Why `actor` may not decide `action` (empty: they may)."""
    reasons: list[str] = []
    if not has_permission(actor.role, Permission.WORKFLOWS_APPROVE):
        reasons.append("Deciding workflow actions needs the workflows:approve permission.")
    required = Role(action.required_role) if action.required_role else None
    if not role_may_approve(actor.role, required):
        title = ACTION_POLICIES[action.action_type].title
        reasons.append(f"'{title}' must be decided by a {action.required_role}.")
    if actor.id in action.maker_ids:
        who = (
            "started this workflow"
            if actor.id == workflow.initiated_by_id
            else "uploaded this document"
        )
        reasons.append(f"You {who}: someone else must decide (maker-checker).")
    return reasons


def pending_action(workflow: Workflow) -> WorkflowAction | None:
    return next((a for a in workflow.actions if a.status == ActionStatus.AWAITING_APPROVAL), None)


class WorkflowService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # ------------------------------------------------------------------ start
    async def start(
        self,
        actor: User,
        workflow_type: WorkflowType,
        document_id: uuid.UUID,
        *,
        meta: RequestMeta,
        trigger: WorkflowTrigger = WorkflowTrigger.MANUAL,
    ) -> Workflow:
        """Queue a workflow for the document's current version. The caller commits."""
        definition = definition_for(workflow_type)
        document = await self._session.scalar(
            select(Document)
            .where(Document.id == document_id, visible_documents(actor))
            .with_for_update(of=Document)
        )
        if document is None:
            raise NotFoundError("Document not found.")
        if document.document_type != definition.document_type:
            kind = (
                document.document_type.value.lower() if document.document_type else "unclassified"
            )
            msg = (
                f"{definition.title} needs a {definition.document_type.value.lower()}; "
                f"{document.display_filename} is {kind}."
            )
            raise UnprocessableContentError(msg)
        if document.status not in PROCESSED or document.current_version_id is None:
            raise ConflictError(f"{document.display_filename} has not finished processing yet.")
        active = await self._session.scalar(
            select(Workflow).where(
                Workflow.document_id == document.id,
                Workflow.workflow_type == workflow_type,
                Workflow.status.in_(ACTIVE_WORKFLOW_STATUSES),
            )
        )
        if active is not None:
            msg = (
                f"A {definition.title.lower()} workflow is already "
                f"{active.status.value.lower().replace('_', ' ')} for this document."
            )
            raise ConflictError(msg)
        workflow = Workflow(
            id=uuid.uuid4(),
            workflow_type=workflow_type,
            definition_version=definition.version,
            status=WorkflowStatus.QUEUED,
            trigger=trigger,
            document_id=document.id,
            document_version_id=document.current_version_id,
            initiated_by_id=actor.id,
            steps=[
                WorkflowStep(sequence=index, step_name=name, status=StepStatus.PENDING)
                for index, name in enumerate(definition.steps, start=1)
            ],
        )
        self._session.add(workflow)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError as exc:  # a concurrent start won the race (unique index)
            msg = f"A {definition.title.lower()} workflow is already active for this document."
            raise ConflictError(msg) from exc
        await enqueue_job(
            self._session,
            job_type=JobType.WORKFLOW,
            max_attempts=1,
            document_id=document.id,
            workflow_id=workflow.id,
            requested_by_id=actor.id,
        )
        record_audit_event(
            self._session,
            action=AuditAction.WORKFLOW_STARTED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            actor_type=ActorType.USER if trigger == WorkflowTrigger.MANUAL else ActorType.SYSTEM,
            entity_type="workflow",
            entity_id=workflow.id,
            details={
                "workflow_type": workflow_type.value,
                "definition_version": definition.version,
                "document_id": str(document.id),
                "document_version_id": str(document.current_version_id),
                "trigger": trigger.value,
            },
        )
        return workflow

    # ------------------------------------------------------------------ read
    async def workflows(
        self,
        actor: User,
        *,
        status: WorkflowStatus | None = None,
        workflow_type: WorkflowType | None = None,
        document_id: uuid.UUID | None = None,
        awaiting_me: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Workflow], int]:
        conditions: list[ColumnElement[bool]] = [visible_workflows(actor)]
        if status is not None:
            conditions.append(Workflow.status == status)
        if workflow_type is not None:
            conditions.append(Workflow.workflow_type == workflow_type)
        if document_id is not None:
            conditions.append(Workflow.document_id == document_id)
        if awaiting_me:
            conditions.append(
                Workflow.id.in_(select(WorkflowAction.workflow_id).where(decidable_by(actor)))
            )
        total = await self._session.scalar(
            select(func.count()).select_from(Workflow).where(*conditions)
        )
        rows = await self._session.scalars(
            select(Workflow)
            .where(*conditions)
            .order_by(Workflow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def get(self, actor: User, workflow_id: uuid.UUID) -> Workflow:
        workflow: Workflow | None = await self._session.scalar(
            select(Workflow)
            .where(Workflow.id == workflow_id, visible_workflows(actor))
            .execution_options(populate_existing=True)
        )
        if workflow is None:
            raise NotFoundError(NOT_FOUND)
        return workflow

    async def documents(self, workflows: Sequence[Workflow]) -> dict[uuid.UUID, Document]:
        ids = {workflow.document_id for workflow in workflows}
        if not ids:
            return {}
        rows = await self._session.scalars(select(Document).where(Document.id.in_(ids)))
        return {row.id: row for row in rows}

    async def version_numbers(self, workflows: Sequence[Workflow]) -> dict[uuid.UUID, int]:
        ids = {workflow.document_version_id for workflow in workflows}
        if not ids:
            return {}
        rows = await self._session.execute(
            select(DocumentVersion.id, DocumentVersion.version_number).where(
                DocumentVersion.id.in_(ids)
            )
        )
        return {row.id: row.version_number for row in rows}

    async def awaiting_count(self, actor: User) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(WorkflowAction)
            .join(Workflow, Workflow.id == WorkflowAction.workflow_id)
            .where(decidable_by(actor), visible_workflows(actor))
        )
        return int(count or 0)

    # ------------------------------------------------------------------ decide
    async def _locked(self, actor: User, workflow_id: uuid.UUID) -> Workflow:
        workflow: Workflow | None = await self._session.scalar(
            select(Workflow)
            .where(Workflow.id == workflow_id, visible_workflows(actor))
            .with_for_update(of=Workflow)
            .execution_options(populate_existing=True)
        )
        if workflow is None:
            raise NotFoundError(NOT_FOUND)
        return workflow

    async def decide(
        self,
        actor: User,
        workflow_id: uuid.UUID,
        *,
        approve: bool,
        reason: str | None,
        meta: RequestMeta,
    ) -> Workflow:
        """Approve or reject the pending action, then carry it out and close the workflow.
        Commits (a refusal is audited and committed before the error is raised)."""
        reason = " ".join(reason.split()) if reason else None
        if not approve and not reason:
            raise UnprocessableContentError("A rejection needs a reason.")
        workflow = await self._locked(actor, workflow_id)
        action: WorkflowAction | None = await self._session.scalar(
            select(WorkflowAction)
            .where(
                WorkflowAction.workflow_id == workflow.id,
                WorkflowAction.status == ActionStatus.AWAITING_APPROVAL,
            )
            .with_for_update(of=WorkflowAction)
            .execution_options(populate_existing=True)
        )
        if workflow.status != WorkflowStatus.AWAITING_APPROVAL or action is None:
            state = workflow.status.value.lower().replace("_", " ")
            raise ConflictError(f"The workflow is not awaiting approval (it is {state}).")
        blockers = decision_blockers(actor, workflow, action)
        if blockers:
            record_audit_event(
                self._session,
                action=AuditAction.WORKFLOW_APPROVAL_DENIED,
                outcome=AuditOutcome.DENIED,
                meta=meta,
                actor=actor,
                entity_type="workflow_action",
                entity_id=action.id,
                details={
                    "workflow_id": str(workflow.id),
                    "decision": "APPROVE" if approve else "REJECT",
                    "reasons": blockers,
                },
            )
            await self._session.commit()
            raise PermissionDeniedError(" ".join(blockers))
        document = await self._session.get(Document, workflow.document_id)
        if approve and (
            document is None
            or document.deleted_at is not None
            or document.current_version_id != action.document_version_id
        ):
            msg = (
                "The document changed after the proposal (it was deleted or a new version was "
                "uploaded): reject this proposal and start a new workflow."
            )
            raise ConflictError(msg)
        stamp = now()
        action.decided_by_id = actor.id
        action.decided_at = stamp
        action.decision_reason = reason
        transition(
            self._session,
            action,
            ActionStatus.APPROVED if approve else ActionStatus.REJECTED,
            previous=ActionStatus.AWAITING_APPROVAL,
            actor=actor,
            actor_type=ActorType.USER,
            reason=reason,
            meta=meta,
        )
        set_step(
            workflow,
            APPROVAL,
            StepStatus.COMPLETED,
            output={
                "decision": "APPROVED" if approve else "REJECTED",
                "decided_by": actor.email,
                "action_id": str(action.id),
            },
        )
        if approve:
            await carry_out(
                self._session,
                self._settings,
                workflow,
                action,
                actor=actor,
                actor_type=ActorType.USER,
                meta=meta,
            )
        else:
            set_step(workflow, EXECUTE_ACTION, StepStatus.SKIPPED, output={"reason": "rejected"})
            await self._hand_back(actor, action, reason or "", meta)
        await finish(
            self._session,
            self._settings,
            workflow,
            action=action,
            actor=actor,
            actor_type=ActorType.USER,
            meta=meta,
        )
        await self._session.commit()
        return await self.get(actor, workflow.id)

    async def _hand_back(
        self, actor: User, action: WorkflowAction, reason: str, meta: RequestMeta
    ) -> None:
        """A rejected proposal leaves the document with a person: request a review with the
        approver's reason (if the document can still be reviewed)."""
        title = ACTION_POLICIES[action.action_type].title
        try:
            async with self._session.begin_nested():
                await ReviewRequestService(self._session, self._settings).request(
                    actor,
                    action.document_id,
                    reason=f"Proposal '{title}' rejected by {actor.email}: {reason}"[:1000],
                    priority_level=ReviewPriority.NORMAL,
                    meta=meta,
                    agent_run_id=action.agent_run_id,
                )
        except (NotFoundError, UnprocessableContentError) as exc:
            logger.info("workflow.hand_back_skipped", action_id=str(action.id), reason=str(exc))

    async def cancel(
        self, actor: User, workflow_id: uuid.UUID, *, reason: str | None, meta: RequestMeta
    ) -> Workflow:
        """Stop a queued or running workflow (its initiator, a manager or an administrator).
        A workflow awaiting approval is decided (rejected) instead. Commits."""
        workflow = await self._locked(actor, workflow_id)
        if workflow.initiated_by_id != actor.id and actor.role not in (Role.MANAGER, Role.ADMIN):
            raise PermissionDeniedError(
                "Only the person who started the workflow, a manager or an administrator can "
                "cancel it."
            )
        if workflow.status not in CANCELLABLE:
            state = workflow.status.value.lower().replace("_", " ")
            hint = " Reject its proposal instead." if state == "awaiting approval" else ""
            raise ConflictError(f"The workflow is {state} and cannot be cancelled.{hint}")
        await self._session.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.workflow_id == workflow.id,
                ProcessingJob.status == JobStatus.QUEUED,
            )
            .values(status=JobStatus.CANCELLED, finished_at=func.now(), last_error="cancelled")
        )
        for step in workflow.steps:
            if step.status in (StepStatus.PENDING, StepStatus.RUNNING):
                set_step(workflow, step.step_name, StepStatus.SKIPPED)
        stamp = now()
        workflow.status = WorkflowStatus.CANCELLED
        workflow.outcome = "CANCELLED"
        workflow.finished_at = stamp
        workflow.updated_at = stamp
        workflow.current_step = None
        record_audit_event(
            self._session,
            action=AuditAction.WORKFLOW_CANCELLED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="workflow",
            entity_id=workflow.id,
            details={
                "workflow_type": workflow.workflow_type.value,
                "document_id": str(workflow.document_id),
                "reason_chars": len(reason or ""),
            },
        )
        await self._session.commit()
        return await self.get(actor, workflow.id)


async def auto_start(session: AsyncSession, settings: Settings, document: Document) -> None:
    """WORKFLOW_AUTO_START: queue the document type's workflow once per version, as the
    version's uploader (if they may start workflows). Runs in the processing transaction."""
    if not settings.workflow_auto_start or document.document_type is None:
        return
    definition = definition_by_document_type(document.document_type)
    if definition is None or definition.workflow_type.value not in settings.workflow_auto_start:
        return
    if document.deleted_at is not None or document.current_version_id is None:
        return
    if document.status not in PROCESSED:
        return
    exists = await session.scalar(
        select(Workflow.id).where(
            Workflow.document_id == document.id,
            Workflow.document_version_id == document.current_version_id,
            Workflow.workflow_type == definition.workflow_type,
        )
    )
    if exists is not None:
        return
    uploader = await session.scalar(
        select(User)
        .join(DocumentVersion, DocumentVersion.uploaded_by_id == User.id)
        .where(DocumentVersion.id == document.current_version_id)
    )
    if (
        uploader is None
        or not uploader.is_active
        or not has_permission(uploader.role, Permission.WORKFLOWS_START)
    ):
        logger.info("workflow.auto_start_skipped", document_id=str(document.id))
        return
    try:
        async with session.begin_nested():
            await WorkflowService(session, settings).start(
                uploader,
                definition.workflow_type,
                document.id,
                meta=SYSTEM_REQUEST,
                trigger=WorkflowTrigger.AUTO,
            )
    except (ConflictError, NotFoundError, UnprocessableContentError) as exc:
        logger.info("workflow.auto_start_skipped", document_id=str(document.id), reason=str(exc))
