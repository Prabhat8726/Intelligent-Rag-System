"""Workflows and human approval (Modules 17, 18)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.api.deps import (
    CurrentUser,
    RequestMetaDep,
    SessionDep,
    SettingsDep,
    require_permission,
)
from docintel.api.schemas.analysis import AnalysisResult
from docintel.api.schemas.common import PROBLEM_RESPONSES, ProblemDetail
from docintel.api.schemas.workflows import (
    ActionRead,
    PendingAction,
    StepRead,
    TransitionRead,
    UserRef,
    WorkflowApprove,
    WorkflowCancel,
    WorkflowCounts,
    WorkflowCreate,
    WorkflowDocument,
    WorkflowPage,
    WorkflowRead,
    WorkflowReject,
    WorkflowSummary,
)
from docintel.auth.permissions import Permission
from docintel.db.models import (
    AgentRun,
    Document,
    Report,
    User,
    Workflow,
    WorkflowAction,
    WorkflowStatus,
    WorkflowType,
)
from docintel.workflows.definitions import definition_for
from docintel.workflows.policy import ACTION_POLICIES
from docintel.workflows.service import WorkflowService, decision_blockers, pending_action

router = APIRouter(
    prefix="/workflows",
    tags=["workflows"],
    responses={
        **PROBLEM_RESPONSES,
        404: {"model": ProblemDetail, "description": "Not found or not accessible"},
    },
)
Starter = Annotated[User, Depends(require_permission(Permission.WORKFLOWS_START))]
Reader = Annotated[User, Depends(require_permission(Permission.WORKFLOWS_READ))]
Approver = Annotated[User, Depends(require_permission(Permission.WORKFLOWS_APPROVE))]


async def _users(session: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, User]:
    wanted = {user_id for user_id in ids if user_id is not None}
    if not wanted:
        return {}
    rows = await session.scalars(select(User).where(User.id.in_(wanted)))
    return {row.id: row for row in rows}


def _user(users: dict[uuid.UUID, User], user_id: uuid.UUID | None) -> UserRef | None:
    user = users.get(user_id) if user_id else None
    return UserRef.model_validate(user) if user else None


def _summary(
    workflow: Workflow,
    documents: dict[uuid.UUID, Document],
    versions: dict[uuid.UUID, int],
    users: dict[uuid.UUID, User],
) -> WorkflowSummary:
    document = documents[workflow.document_id]
    pending = pending_action(workflow)
    initiator = _user(users, workflow.initiated_by_id)
    assert initiator is not None  # noqa: S101 - RESTRICT foreign key
    return WorkflowSummary(
        id=workflow.id,
        workflow_type=workflow.workflow_type,
        title=definition_for(workflow.workflow_type).title,
        status=workflow.status,
        outcome=workflow.outcome,
        trigger=workflow.trigger,
        document=WorkflowDocument(
            id=document.id,
            filename=document.display_filename,
            document_type=document.document_type,
            status=document.status,
            version_number=versions.get(workflow.document_version_id),
            is_current_version=document.current_version_id == workflow.document_version_id,
        ),
        initiated_by=initiator,
        current_step=workflow.current_step,
        pending_action=PendingAction(
            id=pending.id,
            action_type=pending.action_type,
            title=ACTION_POLICIES[pending.action_type].title,
            risk_level=pending.risk_level,
            required_role=pending.required_role,
        )
        if pending
        else None,
        error=workflow.error,
        created_at=workflow.created_at,
        started_at=workflow.started_at,
        finished_at=workflow.finished_at,
    )


def _action(
    action: WorkflowAction, workflow: Workflow, viewer: User, users: dict[uuid.UUID, User]
) -> ActionRead:
    blockers = decision_blockers(viewer, workflow, action)
    awaiting = action.status.value == "AWAITING_APPROVAL"
    return ActionRead(
        id=action.id,
        action_type=action.action_type,
        title=ACTION_POLICIES[action.action_type].title,
        status=action.status,
        risk_level=action.risk_level,
        requires_approval=action.requires_approval,
        required_role=action.required_role,
        proposed_by_type=action.proposed_by_type,
        rationale=action.rationale,
        confidence_level=action.confidence_level,
        confidence_score=action.confidence_score,
        payload=action.payload,
        decided_by=_user(users, action.decided_by_id),
        decided_at=action.decided_at,
        decision_reason=action.decision_reason,
        executed_at=action.executed_at,
        execution_result=action.execution_result,
        error=action.error,
        created_at=action.created_at,
        transitions=[
            TransitionRead(
                from_status=item.from_status,
                to_status=item.to_status,
                actor_type=item.actor_type,
                actor=_user(users, item.actor_id),
                reason=item.reason,
                created_at=item.created_at,
            )
            for item in action.transitions
        ],
        can_decide=awaiting and not blockers,
        blockers=blockers if awaiting else [],
    )


async def _page_items(
    service: WorkflowService, session: AsyncSession, workflows: list[Workflow]
) -> list[WorkflowSummary]:
    documents = await service.documents(workflows)
    versions = await service.version_numbers(workflows)
    users = await _users(session, {w.initiated_by_id for w in workflows})
    return [_summary(w, documents, versions, users) for w in workflows]


async def _detail(
    service: WorkflowService, session: AsyncSession, workflow: Workflow, viewer: User
) -> WorkflowRead:
    documents = await service.documents([workflow])
    versions = await service.version_numbers([workflow])
    ids: list[uuid.UUID | None] = [workflow.initiated_by_id]
    for action in workflow.actions:
        ids.append(action.decided_by_id)
        ids.extend(item.actor_id for item in action.transitions)
    users = await _users(session, ids)
    run = await session.get(AgentRun, workflow.agent_run_id) if workflow.agent_run_id else None
    reports = list(
        await session.scalars(
            select(Report.id)
            .where(Report.workflow_id == workflow.id)
            .order_by(Report.created_at, Report.id)
        )
    )
    return WorkflowRead(
        **_summary(workflow, documents, versions, users).model_dump(),
        definition_version=workflow.definition_version,
        steps=[StepRead.model_validate(step) for step in workflow.steps],
        actions=[_action(action, workflow, viewer, users) for action in workflow.actions],
        agent_run_id=workflow.agent_run_id,
        analysis=AnalysisResult.model_validate(run.result) if run and run.result else None,
        report_ids=reports,
    )


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WorkflowRead,
    summary="Start a workflow for a processed document (runs in the background)",
    description="INVOICE_PROCESSING checks the invoice, investigates it (rules, comparison "
    "with order and deliveries, policies) and proposes one action; CONTRACT_REVIEW does the "
    "same for a contract against the contract guidelines and its previous version. Low-risk "
    "actions (a review request) are carried out; payment, rejection, vendor clarification and "
    "contract approval wait for a person other than you and the uploader.",
    responses={409: {"model": ProblemDetail, "description": "Not processed, or already active"}},
)
async def start_workflow(
    body: WorkflowCreate,
    response: Response,
    user: Starter,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> WorkflowRead:
    service = WorkflowService(session, settings)
    workflow = await service.start(user, body.workflow_type, body.document_id, meta=meta)
    await session.commit()
    workflow = await service.get(user, workflow.id)
    response.headers["Location"] = f"/api/v1/workflows/{workflow.id}"
    return await _detail(service, session, workflow, user)


@router.get(
    "/summary",
    response_model=WorkflowCounts,
    summary="How many workflow actions wait for your decision",
)
async def workflow_counts(
    user: Reader, session: SessionDep, settings: SettingsDep
) -> WorkflowCounts:
    count = await WorkflowService(session, settings).awaiting_count(user)
    return WorkflowCounts(awaiting_my_decision=count)


@router.get("", response_model=WorkflowPage, summary="Workflows on documents you can see")
async def list_workflows(
    user: Reader,
    session: SessionDep,
    settings: SettingsDep,
    workflow_status: Annotated[WorkflowStatus | None, Query(alias="status")] = None,
    workflow_type: WorkflowType | None = None,
    document_id: uuid.UUID | None = None,
    awaiting_me: Annotated[
        bool, Query(description="Only workflows with an action you may decide now")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> WorkflowPage:
    service = WorkflowService(session, settings)
    workflows, total = await service.workflows(
        user,
        status=workflow_status,
        workflow_type=workflow_type,
        document_id=document_id,
        awaiting_me=awaiting_me,
        limit=limit,
        offset=offset,
    )
    items = await _page_items(service, session, workflows)
    return WorkflowPage(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/{workflow_id}",
    response_model=WorkflowRead,
    summary="A workflow: steps, investigation, proposed action, decisions and history",
)
async def get_workflow(
    workflow_id: uuid.UUID, user: Reader, session: SessionDep, settings: SettingsDep
) -> WorkflowRead:
    service = WorkflowService(session, settings)
    return await _detail(service, session, await service.get(user, workflow_id), user)


@router.post(
    "/{workflow_id}/approve",
    response_model=WorkflowRead,
    summary="Approve the proposed action; it is carried out at once",
    description="Needs workflows:approve, the action's required role (or a higher one), and "
    "not having started the workflow or uploaded the document (maker-checker). The executor "
    "re-checks the document first; if it no longer qualifies, the action FAILS.",
    responses={409: {"model": ProblemDetail, "description": "Nothing awaits approval"}},
)
async def approve_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowApprove,
    user: Approver,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> WorkflowRead:
    service = WorkflowService(session, settings)
    workflow = await service.decide(user, workflow_id, approve=True, reason=body.reason, meta=meta)
    return await _detail(service, session, workflow, user)


@router.post(
    "/{workflow_id}/reject",
    response_model=WorkflowRead,
    summary="Reject the proposed action (reason required)",
    description="The document is handed back to the review queue with your reason.",
    responses={409: {"model": ProblemDetail, "description": "Nothing awaits approval"}},
)
async def reject_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowReject,
    user: Approver,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> WorkflowRead:
    service = WorkflowService(session, settings)
    workflow = await service.decide(user, workflow_id, approve=False, reason=body.reason, meta=meta)
    return await _detail(service, session, workflow, user)


@router.post(
    "/{workflow_id}/cancel",
    response_model=WorkflowRead,
    summary="Cancel a queued or running workflow",
    responses={409: {"model": ProblemDetail, "description": "Not cancellable"}},
)
async def cancel_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowCancel,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> WorkflowRead:
    service = WorkflowService(session, settings)
    workflow = await service.cancel(user, workflow_id, reason=body.reason, meta=meta)
    return await _detail(service, session, workflow, user)
