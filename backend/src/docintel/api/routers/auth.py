"""Authentication endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response, status

from docintel.api.deps import CurrentUser, RequestMetaDep, SessionDep, SettingsDep
from docintel.api.rate_limit import LOGIN_LIMIT
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
from docintel.auth.sessions import (
    COOKIE_NAME,
    COOKIE_PATH,
    CSRF_HEADER,
    IssuedSession,
    SessionService,
)
from docintel.core.config import Settings
from docintel.core.errors import AuthenticationError, PermissionDeniedError

router = APIRouter(prefix="/auth", tags=["auth"], responses=PROBLEM_RESPONSES)


def _set_session_cookie(response: Response, issued: IssuedSession, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        issued.refresh_token,
        expires=issued.refresh_expires_at,
        path=COOKIE_PATH,
        secure=settings.effective_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path=COOKIE_PATH,
        secure=settings.effective_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _require_session_header(request: Request) -> None:
    """Cookie-authenticated calls must come from the SPA (CSRF defence in depth)."""
    if request.headers.get(CSRF_HEADER) != "1":
        raise PermissionDeniedError(f"This request needs the {CSRF_HEADER}: 1 header.")


@router.post(
    "/login",
    dependencies=[LOGIN_LIMIT],
    response_model=TokenResponse,
    summary="Exchange credentials for a JWT",
    description=f"With the header `{CSRF_HEADER}: 1` (the web app) the response also sets an "
    "httpOnly refresh cookie for POST /auth/refresh; API clients omit it and get the access "
    "token only.",
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> TokenResponse:
    result = await AuthService(session, settings).login(
        email=body.email,
        password=body.password,
        meta=meta,
        browser_session=request.headers.get(CSRF_HEADER) == "1",
    )
    if result.session is not None:
        _set_session_cookie(response, result.session, settings)
    return TokenResponse(
        access_token=result.token.token,
        expires_in=result.token.expires_in_seconds,
        user=UserRead.model_validate(result.user),
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="A new access token from the session cookie (rotates the cookie)",
    description="Each refresh token works once. Presenting one again ends the whole session "
    f"(a copied cookie). Needs the `{CSRF_HEADER}: 1` header.",
)
async def refresh(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    meta: RequestMetaDep,
) -> TokenResponse:
    _require_session_header(request)
    try:
        issued = await SessionService(session, settings).refresh(
            request.cookies.get(COOKIE_NAME), meta
        )
    except AuthenticationError as exc:
        # The browser forgets the dead cookie; the error is the usual 401 problem detail.
        exc.headers = {
            **(exc.headers or {}),
            "Set-Cookie": _expired_cookie_header(settings),
        }
        raise
    _set_session_cookie(response, issued, settings)
    return TokenResponse(
        access_token=issued.access.token,
        expires_in=issued.access.expires_in_seconds,
        user=UserRead.model_validate(issued.user),
    )


def _expired_cookie_header(settings: Settings) -> str:
    probe = Response()
    _clear_session_cookie(probe, settings)
    return probe.headers["set-cookie"]


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End the browser session (revokes the session cookie)",
)
async def logout(
    request: Request, session: SessionDep, settings: SettingsDep, meta: RequestMetaDep
) -> Response:
    _require_session_header(request)
    await SessionService(session, settings).logout(request.cookies.get(COOKIE_NAME), meta)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookie(response, settings)
    return response


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
