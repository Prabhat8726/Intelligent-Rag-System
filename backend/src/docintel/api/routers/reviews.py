"""Review queue endpoints (Module 26): list, claim, release, resolve."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from docintel.api.deps import RequestMetaDep, SessionDep, SettingsDep, require_permission
from docintel.api.schemas.common import PROBLEM_RESPONSES, ProblemDetail
from docintel.api.schemas.matching import (
    ReviewDocument,
    ReviewResolve,
    ReviewTaskListItem,
    ReviewTaskPage,
    ReviewTaskRead,
)
from docintel.auth.permissions import Permission
from docintel.db.models import Document, ReviewPriority, ReviewTask, ReviewTaskType, User
from docintel.review.queue import ReviewFilters, ReviewQueue
from docintel.review.service import ReviewService

router = APIRouter(
    prefix="/review-tasks",
    tags=["review"],
    responses={
        **PROBLEM_RESPONSES,
        404: {"model": ProblemDetail, "description": "Not found or not accessible"},
        409: {"model": ProblemDetail, "description": "Closed, or claimed by someone else"},
    },
)
Worker = Annotated[User, Depends(require_permission(Permission.REVIEWS_WORK))]


def _item(task: ReviewTask, document: Document) -> ReviewTaskListItem:
    return ReviewTaskListItem(
        **ReviewTaskRead.model_validate(task).model_dump(exclude={"overdue"}),
        document=ReviewDocument.model_validate(document),
    )


@router.get("", response_model=ReviewTaskPage, summary="Review tasks, most urgent first")
async def list_review_tasks(
    user: Worker,
    session: SessionDep,
    state: Literal["open", "closed", "all"] = "open",
    task_type: ReviewTaskType | None = None,
    priority: ReviewPriority | None = None,
    assigned: Literal["any", "me", "unassigned"] = "any",
    document_id: uuid.UUID | None = None,
    overdue: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> ReviewTaskPage:
    rows, total = await ReviewQueue(session).tasks(
        user,
        ReviewFilters(state, task_type, priority, assigned, document_id, overdue),
        limit=limit,
        offset=offset,
    )
    return ReviewTaskPage(
        items=[_item(task, document) for task, document in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{task_id}", response_model=ReviewTaskListItem, summary="One review task")
async def get_review_task(
    task_id: uuid.UUID, user: Worker, session: SessionDep
) -> ReviewTaskListItem:
    task, document = await ReviewQueue(session).get(user, task_id)
    return _item(task, document)


async def _after(session: SessionDep, user: User, task_id: uuid.UUID) -> ReviewTaskListItem:
    await session.commit()
    task, document = await ReviewQueue(session).get(user, task_id, fresh=True)
    return _item(task, document)


@router.post("/{task_id}/claim", response_model=ReviewTaskListItem, summary="Take a task")
async def claim_review_task(
    task_id: uuid.UUID,
    user: Worker,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> ReviewTaskListItem:
    await ReviewQueue(session).get(user, task_id)  # scope check (404 outside it)
    await ReviewService(session, settings).claim(user, task_id, meta)
    return await _after(session, user, task_id)


@router.post("/{task_id}/release", response_model=ReviewTaskListItem, summary="Give a task back")
async def release_review_task(
    task_id: uuid.UUID,
    user: Worker,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> ReviewTaskListItem:
    await ReviewQueue(session).get(user, task_id)
    await ReviewService(session, settings).release(user, task_id, meta)
    return await _after(session, user, task_id)


@router.post(
    "/{task_id}/resolve",
    response_model=ReviewTaskListItem,
    summary="Record the review decision (the document leaves the review queue)",
    description="APPROVED: the findings are accepted as they are. CORRECTED: values were fixed "
    "(corrections re-run matching on their own). REJECTED: the document must not be processed "
    "further; a note is required. A finding resolved here does not reopen a task for this "
    "version; a new finding does.",
)
async def resolve_review_task(
    task_id: uuid.UUID,
    body: ReviewResolve,
    user: Worker,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> ReviewTaskListItem:
    await ReviewQueue(session).get(user, task_id)
    await ReviewService(session, settings).resolve(user, task_id, body.resolution, body.note, meta)
    return await _after(session, user, task_id)
