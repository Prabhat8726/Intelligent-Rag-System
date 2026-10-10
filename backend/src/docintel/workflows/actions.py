"""The action state machine (Module 17) and how a decided action finishes its workflow.

  (new) -> PROPOSED -> AWAITING_APPROVAL -> APPROVED -> EXECUTED
                    \\                    \\-> REJECTED   \\-> FAILED
                     \\-> APPROVED (only actions the risk table lets through without a person)

Every change goes through `transition`, which checks the table, writes a
`workflow_action_transitions` row and an audit event in the caller's transaction. Used by the
worker (proposals, low-risk actions) and the API (approve, reject) alike.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.core.config import Settings
from docintel.core.errors import ConflictError
from docintel.core.logging import get_logger
from docintel.db.models import (
    ActionStatus,
    ActorType,
    AuditOutcome,
    Document,
    StepStatus,
    User,
    Workflow,
    WorkflowAction,
    WorkflowActionTransition,
    WorkflowStatus,
    WorkflowStep,
)
from docintel.matching.store import lock_department
from docintel.workflows.definitions import APPROVAL, EXECUTE_ACTION, REPORT, definition_for
from docintel.workflows.executors import EXECUTORS, ExecutionContext, ExecutionRefusedError
from docintel.workflows.policy import ACTION_POLICIES, FAILED_OUTCOME, REJECTED_OUTCOME

logger = get_logger(__name__)

S = ActionStatus
TRANSITIONS: dict[ActionStatus | None, frozenset[ActionStatus]] = {
    None: frozenset({S.PROPOSED}),
    S.PROPOSED: frozenset({S.AWAITING_APPROVAL, S.APPROVED}),
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.REJECTED}),
    S.APPROVED: frozenset({S.EXECUTED, S.FAILED}),
}
_AUDIT: dict[ActionStatus, AuditAction] = {
    S.PROPOSED: AuditAction.WORKFLOW_ACTION_PROPOSED,
    S.AWAITING_APPROVAL: AuditAction.WORKFLOW_ACTION_AWAITING_APPROVAL,
    S.APPROVED: AuditAction.WORKFLOW_ACTION_APPROVED,
    S.REJECTED: AuditAction.WORKFLOW_ACTION_REJECTED,
    S.EXECUTED: AuditAction.WORKFLOW_ACTION_EXECUTED,
    S.FAILED: AuditAction.WORKFLOW_ACTION_FAILED,
}
_FINISHED = {
    WorkflowStatus.COMPLETED: AuditAction.WORKFLOW_COMPLETED,
    WorkflowStatus.REJECTED: AuditAction.WORKFLOW_REJECTED,
    WorkflowStatus.FAILED: AuditAction.WORKFLOW_FAILED,
}


class InvalidTransitionError(ConflictError):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def transition(
    session: AsyncSession,
    action: WorkflowAction,
    to: ActionStatus,
    *,
    previous: ActionStatus | None,
    actor: User | None,
    actor_type: ActorType,
    reason: str | None,
    meta: RequestMeta,
    details: dict[str, Any] | None = None,
) -> None:
    """Move `action` from `previous` (None: just created) to `to`, with history and audit."""
    if to not in TRANSITIONS.get(previous, frozenset()):
        msg = f"An action cannot go from {previous or 'new'} to {to.value}."
        raise InvalidTransitionError(msg)
    if previous == S.PROPOSED and to == S.APPROVED and action.requires_approval:
        msg = f"{action.action_type.value} needs a person's approval."
        raise InvalidTransitionError(msg)
    stamp = now()
    action.status = to
    action.updated_at = stamp
    session.add(
        WorkflowActionTransition(
            action_id=action.id,
            from_status=previous,
            to_status=to,
            actor_type=actor_type,
            actor_id=actor.id if actor else None,
            reason=reason[:1000] if reason else None,
            created_at=stamp,
        )
    )
    record_audit_event(
        session,
        action=_AUDIT[to],
        outcome=AuditOutcome.FAILURE if to == S.FAILED else AuditOutcome.SUCCESS,
        meta=meta,
        actor=actor,
        actor_type=actor_type,
        entity_type="workflow_action",
        entity_id=action.id,
        details={
            "workflow_id": str(action.workflow_id),
            "document_id": str(action.document_id),
            "action_type": action.action_type.value,
            "from": previous.value if previous else None,
            "to": to.value,
            # Reasons may quote the document: the audit keeps their size, the transition keeps
            # the text (readable by those who can read the workflow).
            "reason_chars": len(reason) if reason else 0,
            **(details or {}),
        },
    )


def step_of(workflow: Workflow, name: str) -> WorkflowStep | None:
    return next((step for step in workflow.steps if step.step_name == name), None)


def set_step(
    workflow: Workflow,
    name: str,
    status: StepStatus,
    *,
    output: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    step = step_of(workflow, name)
    if step is None:
        return
    stamp = now()
    if step.started_at is None:
        step.started_at = stamp
    step.status = status
    if output is not None:
        step.output = output
    step.error = error[:1000] if error else None
    if status != StepStatus.RUNNING:
        step.finished_at = stamp
    workflow.current_step = name
    workflow.updated_at = stamp


async def lock_document(session: AsyncSession, document_id: Any) -> Document | None:
    """Department, then document - the lock order matching and review use."""
    department_id = await session.scalar(
        select(Document.department_id).where(Document.id == document_id)
    )
    await lock_department(session, department_id)
    document: Document | None = await session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update(of=Document)
        .execution_options(populate_existing=True)
    )
    return document


async def carry_out(
    session: AsyncSession,
    settings: Settings,
    workflow: Workflow,
    action: WorkflowAction,
    *,
    actor: User,
    actor_type: ActorType,
    meta: RequestMeta,
) -> None:
    """Run the approved action's executor (inside a savepoint) and record EXECUTED or FAILED."""
    document = await lock_document(session, action.document_id)
    executor = EXECUTORS[action.action_type]
    error: str | None = None
    result: dict[str, Any] | None = None
    if document is None:
        error = "The document no longer exists."
    else:
        context = ExecutionContext(session, settings, workflow, action, document, actor, meta)
        try:
            async with session.begin_nested():
                result = await executor(context)
        except ExecutionRefusedError as exc:
            error = str(exc)
        except Exception:
            logger.exception("workflow.executor_failed", action_id=str(action.id))
            error = f"The action could not be carried out (reference: action {action.id})."
    stamp = now()
    if error is None:
        action.executed_at = stamp
        action.execution_result = result or {}
        action.error = None
        transition(
            session,
            action,
            S.EXECUTED,
            previous=S.APPROVED,
            actor=actor,
            actor_type=actor_type,
            reason=None,
            meta=meta,
        )
        set_step(workflow, EXECUTE_ACTION, StepStatus.COMPLETED, output=result or {})
    else:
        action.error = error[:1000]
        transition(
            session,
            action,
            S.FAILED,
            previous=S.APPROVED,
            actor=actor,
            actor_type=actor_type,
            reason=error,
            meta=meta,
        )
        set_step(workflow, EXECUTE_ACTION, StepStatus.FAILED, error=error)


async def finish(
    session: AsyncSession,
    settings: Settings,
    workflow: Workflow,
    *,
    action: WorkflowAction | None,
    actor: User,
    actor_type: ActorType,
    meta: RequestMeta,
) -> None:
    """Close the workflow after its action was decided (or when there is none): the report,
    the final status and outcome, the audit event."""
    from docintel.reports.service import ReportService  # reports read workflows

    if action is None:
        status, outcome, error = WorkflowStatus.COMPLETED, "NO_ACTION", None
    elif action.status == S.EXECUTED:
        status, outcome = WorkflowStatus.COMPLETED, ACTION_POLICIES[action.action_type].outcome
        error = None
    elif action.status == S.REJECTED:
        status, outcome, error = WorkflowStatus.REJECTED, REJECTED_OUTCOME, None
    else:
        status, outcome, error = WorkflowStatus.FAILED, FAILED_OUTCOME, action.error
    for name in (APPROVAL, EXECUTE_ACTION):
        step = step_of(workflow, name)
        if step is not None and step.status == StepStatus.PENDING:
            set_step(workflow, name, StepStatus.SKIPPED)
    # The workflow closes first (status, outcome and time together - a CHECK ties them), so
    # the report it generates records the final decision.
    stamp = now()
    workflow.status = status
    workflow.outcome = outcome
    workflow.error = error[:1000] if error else None
    workflow.finished_at = stamp
    set_step(workflow, REPORT, StepStatus.RUNNING)
    await session.flush()
    try:
        async with session.begin_nested():
            report = await ReportService(session, settings).generate(
                actor,
                definition_for(workflow.workflow_type).report_type,
                workflow.document_id,
                workflow_id=workflow.id,
                meta=meta,
                actor_type=actor_type,
            )
        set_step(
            workflow,
            REPORT,
            StepStatus.COMPLETED,
            output={"report_id": str(report.id), "sha256": report.content_sha256},
        )
    except Exception as exc:
        logger.warning(
            "workflow.report_failed", workflow_id=str(workflow.id), error=type(exc).__name__
        )
        set_step(workflow, REPORT, StepStatus.FAILED, error="The report could not be generated.")
    workflow.updated_at = now()
    workflow.current_step = None
    record_audit_event(
        session,
        action=_FINISHED[status],
        outcome=AuditOutcome.FAILURE if status == WorkflowStatus.FAILED else AuditOutcome.SUCCESS,
        meta=meta,
        actor=actor,
        actor_type=actor_type,
        entity_type="workflow",
        entity_id=workflow.id,
        details={
            "workflow_type": workflow.workflow_type.value,
            "document_id": str(workflow.document_id),
            "outcome": outcome,
            "action_id": str(action.id) if action else None,
        },
    )
