"""Auth request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from docintel.api.schemas.common import RequestModel, ResponseModel
from docintel.auth.passwords import PASSWORD_MAX_LENGTH
from docintel.db.models import Role


class LoginRequest(RequestModel):
    # Plain strings (not EmailStr): any malformed credential gets the same 401 as a wrong one.
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class DepartmentRead(ResponseModel):
    id: uuid.UUID
    name: str


class UserRead(ResponseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    department: DepartmentRead | None
    is_active: bool
    last_login_at: datetime | None


class CurrentUserRead(UserRead):
    permissions: list[str]


class TokenResponse(ResponseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (OAuth2 token type, not a secret)
    expires_in: int = Field(description="Seconds until the access token expires")
    user: UserRead


class ApiTokenCreate(RequestModel):
    name: str = Field(min_length=1, max_length=100, description="e.g. 'Laptop MCP client'")
    scopes: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Permissions the token may use (a subset of yours): documents:read, "
        "knowledge:read, comparisons:create, reviews:work",
    )
    expires_in_days: int = Field(default=30, ge=1, le=365)


class ApiTokenRead(ResponseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiTokenCreated(ApiTokenRead):
    token: str = Field(description="Shown once: store it now; only its hash is kept")
