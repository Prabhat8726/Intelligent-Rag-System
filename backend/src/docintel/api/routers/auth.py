"""Authentication endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from docintel.api.deps import CurrentUser, RequestMetaDep, SessionDep, SettingsDep
from docintel.api.schemas.auth import (
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenRead,
    CurrentUserRead,
    LoginRequest,
    TokenResponse,
    UserRead,
)
from docintel.api.schemas.common import PROBLEM_RESPONSES
from docintel.auth.api_tokens import ApiTokenService
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


# ------------------------------------------------------------------------------ API tokens
@router.post(
    "/tokens",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiTokenCreated,
    summary="Create a personal API token for MCP clients (shown once)",
)
async def create_api_token(
    body: ApiTokenCreate,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> ApiTokenCreated:
    token, secret = await ApiTokenService(session, settings).create(
        user,
        name=body.name,
        scopes=body.scopes,
        expires_in_days=body.expires_in_days,
        meta=meta,
    )
    return ApiTokenCreated(**ApiTokenRead.model_validate(token).model_dump(), token=secret)


@router.get("/tokens", response_model=list[ApiTokenRead], summary="Your API tokens")
async def list_api_tokens(
    user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> list[ApiTokenRead]:
    tokens = await ApiTokenService(session, settings).tokens(user)
    return [ApiTokenRead.model_validate(token) for token in tokens]


@router.delete(
    "/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke one of your API tokens",
)
async def revoke_api_token(
    token_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> Response:
    await ApiTokenService(session, settings).revoke(user, token_id, meta)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
