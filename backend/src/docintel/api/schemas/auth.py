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
