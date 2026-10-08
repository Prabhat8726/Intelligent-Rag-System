"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from docintel.api.deps import CurrentUser, RequestMetaDep, SessionDep, SettingsDep
from docintel.api.schemas.auth import CurrentUserRead, LoginRequest, TokenResponse, UserRead
from docintel.api.schemas.common import PROBLEM_RESPONSES
from docintel.auth.permissions import permissions_for
from docintel.auth.service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"], responses=PROBLEM_RESPONSES)


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for a JWT")
async def login(
    body: LoginRequest, session: SessionDep, settings: SettingsDep, meta: RequestMetaDep
) -> TokenResponse:
    result = await AuthService(session, settings).login(
        email=body.email, password=body.password, meta=meta
    )
    return TokenResponse(
        access_token=result.token.token,
        expires_in=result.token.expires_in_seconds,
        user=UserRead.model_validate(result.user),
    )


@router.get("/me", response_model=CurrentUserRead, summary="Current user and effective permissions")
async def me(user: CurrentUser) -> CurrentUserRead:
    base = UserRead.model_validate(user)
    return CurrentUserRead(
        **base.model_dump(),
        permissions=sorted(permission.value for permission in permissions_for(user.role)),
    )
