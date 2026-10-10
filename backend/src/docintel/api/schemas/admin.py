"""User administration and audit-log API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from docintel.api.schemas.auth import DepartmentRead, UserRead
from docintel.api.schemas.common import RequestModel, ResponseModel
from docintel.auth.passwords import PASSWORD_MAX_LENGTH
from docintel.db.models import ActorType, AuditOutcome, Role


class UserCreate(RequestModel):
    email: str = Field(min_length=3, max_length=320)
    full_name: str = Field(min_length=1, max_length=200)
    role: Role
    department_id: uuid.UUID | None = Field(
        default=None, description="Required for every role but ADMIN"
    )
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class UserUpdate(RequestModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    department_id: uuid.UUID | None = Field(
        default=None, description="Send null to remove the department (administrators only)"
    )
    is_active: bool | None = None


class PasswordReset(RequestModel):
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class AdminUserRead(UserRead):
    failed_login_attempts: int
    locked_until: datetime | None
    created_at: datetime


class UserPage(ResponseModel):
    items: list[AdminUserRead]
    total: int
    limit: int
    offset: int


class DepartmentCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100)


DepartmentList = list[DepartmentRead]


class AuditActor(ResponseModel):
    id: uuid.UUID
    email: str
    full_name: str


class AuditEventRead(ResponseModel):
    id: int
    occurred_at: datetime
    actor: AuditActor | None
    actor_type: ActorType
    actor_role: str | None
    action: str
    entity_type: str | None
    entity_id: str | None
    outcome: AuditOutcome
    request_id: str | None
    ip_address: str | None = Field(description="Administrators only")
    user_agent: str | None = Field(description="Administrators only")
    details: dict[str, Any]


class AuditEventPage(ResponseModel):
    items: list[AuditEventRead]
    next_before_id: int | None = Field(description="Pass as before_id for the next page")
