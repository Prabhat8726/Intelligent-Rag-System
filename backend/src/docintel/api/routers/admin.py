"""User administration (users:manage), departments and the audit trail (audit:read)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from docintel.api.deps import CurrentUser, RequestMetaDep, SessionDep, require_permission
from docintel.api.schemas.admin import (
    AdminUserRead,
    AuditActor,
    AuditEventPage,
    AuditEventRead,
    DepartmentCreate,
    PasswordReset,
    UserCreate,
    UserPage,
    UserUpdate,
)
from docintel.api.schemas.auth import DepartmentRead
from docintel.api.schemas.common import PROBLEM_RESPONSES, ProblemDetail
from docintel.audit.queries import AuditFilters, AuditLogReader
from docintel.auth.admin import UserAdminService, UserChanges
from docintel.auth.permissions import Permission
from docintel.db.models import AuditOutcome, Role, User

router = APIRouter(
    tags=["administration"],
    responses={
        **PROBLEM_RESPONSES,
        404: {"model": ProblemDetail, "description": "Not found"},
        409: {"model": ProblemDetail, "description": "Not allowed in the current state"},
    },
)
Admin = Annotated[User, Depends(require_permission(Permission.USERS_MANAGE))]
AuditReader = Annotated[User, Depends(require_permission(Permission.AUDIT_READ))]


# ------------------------------------------------------------------------------ users
@router.get("/users", response_model=UserPage, summary="Users (administrators)")
async def list_users(
    _: Admin,
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=100, description="Email or name contains")] = None,
    role: Role | None = None,
    department_id: uuid.UUID | None = None,
    active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> UserPage:
    users, total = await UserAdminService(session).users(
        query=q, role=role, department_id=department_id, active=active, limit=limit, offset=offset
    )
    return UserPage(
        items=[AdminUserRead.model_validate(user) for user in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    response_model=AdminUserRead,
    summary="Create a user (administrators)",
)
async def create_user(
    body: UserCreate, response: Response, actor: Admin, session: SessionDep, meta: RequestMetaDep
) -> AdminUserRead:
    service = UserAdminService(session)
    user = await service.create(
        actor,
        email=body.email,
        full_name=body.full_name,
        role=body.role,
        department_id=body.department_id,
        password=body.password,
        meta=meta,
    )
    await session.commit()
    response.headers["Location"] = f"/api/v1/users/{user.id}"
    return AdminUserRead.model_validate(await service.get(user.id))


@router.get("/users/{user_id}", response_model=AdminUserRead, summary="A user (administrators)")
async def get_user(user_id: uuid.UUID, _: Admin, session: SessionDep) -> AdminUserRead:
    return AdminUserRead.model_validate(await UserAdminService(session).get(user_id))


@router.patch(
    "/users/{user_id}",
    response_model=AdminUserRead,
    summary="Change a user's name, role, department or active state (administrators)",
    description="You cannot change your own role or deactivate yourself, and the last active "
    "administrator stays an active administrator. Deactivation revokes the user's API tokens "
    "and takes effect on their next request.",
)
async def update_user(
    user_id: uuid.UUID, body: UserUpdate, actor: Admin, session: SessionDep, meta: RequestMetaDep
) -> AdminUserRead:
    changes = UserChanges(full_name=body.full_name, role=body.role, is_active=body.is_active)
    if "department_id" in body.model_fields_set:
        changes.department_id = body.department_id
    service = UserAdminService(session)
    await service.update(actor, user_id, changes, meta)
    await session.commit()
    return AdminUserRead.model_validate(await service.get(user_id))


@router.post(
    "/users/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a new password (administrators); unlocks the account",
)
async def reset_password(
    user_id: uuid.UUID,
    body: PasswordReset,
    actor: Admin,
    session: SessionDep,
    meta: RequestMetaDep,
) -> Response:
    await UserAdminService(session).reset_password(actor, user_id, body.password, meta)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------ departments
@router.get(
    "/departments", response_model=list[DepartmentRead], summary="Departments (any signed-in user)"
)
async def list_departments(_: CurrentUser, session: SessionDep) -> list[DepartmentRead]:
    return [
        DepartmentRead.model_validate(row) for row in await UserAdminService(session).departments()
    ]


@router.post(
    "/departments",
    status_code=status.HTTP_201_CREATED,
    response_model=DepartmentRead,
    summary="Create a department (administrators)",
)
async def create_department(
    body: DepartmentCreate, actor: Admin, session: SessionDep, meta: RequestMetaDep
) -> DepartmentRead:
    department = await UserAdminService(session).create_department(actor, body.name, meta)
    await session.commit()
    return DepartmentRead.model_validate(department)


# ------------------------------------------------------------------------------ audit trail
@router.get(
    "/audit-logs",
    response_model=AuditEventPage,
    summary="The audit trail, newest first",
    description="Administrators read every event; managers the events of their department "
    "(caused by its members, or about its documents). `action` matches exactly, or as a "
    "prefix when it ends with '.' (e.g. 'workflow.').",
)
async def list_audit_logs(
    actor: AuditReader,
    session: SessionDep,
    actor_id: uuid.UUID | None = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    entity_type: Annotated[str | None, Query(max_length=50)] = None,
    entity_id: Annotated[str | None, Query(max_length=64)] = None,
    outcome: AuditOutcome | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditEventPage:
    filters = AuditFilters(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        outcome=outcome,
        since=since,
        until=until,
    )
    page, next_before = await AuditLogReader(session).events(
        actor, filters, limit=limit, before_id=before_id
    )
    admin = actor.role == Role.ADMIN
    return AuditEventPage(
        items=[
            AuditEventRead(
                id=event.id,
                occurred_at=event.occurred_at,
                actor=AuditActor.model_validate(user) if user else None,
                actor_type=event.actor_type,
                actor_role=event.actor_role,
                action=event.action,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                outcome=event.outcome,
                request_id=event.request_id,
                ip_address=str(event.ip_address) if admin and event.ip_address else None,
                user_agent=event.user_agent if admin else None,
                details=event.details,
            )
            for event, user in page
        ],
        next_before_id=next_before,
    )
