"""FastAPI dependencies: settings, DB session, request metadata, authentication, RBAC."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.permissions import Permission, has_permission
from docintel.auth.tokens import InvalidTokenError, decode_access_token
from docintel.core.config import Settings
from docintel.core.errors import AuthenticationError, PermissionDeniedError
from docintel.core.logging import get_logger
from docintel.db.models import AuditOutcome, User
from docintel.storage import DocumentStorage

logger = get_logger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False, description="JWT from POST /api/v1/auth/login")


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session


def get_storage(request: Request) -> DocumentStorage:
    storage: DocumentStorage = request.app.state.storage
    return storage


def get_request_meta(request: Request) -> RequestMeta:
    return RequestMeta(
        request_id=getattr(request.state, "request_id", None),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RequestMetaDep = Annotated[RequestMeta, Depends(get_request_meta)]
StorageDep = Annotated[DocumentStorage, Depends(get_storage)]


async def get_current_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError()
    try:
        claims = decode_access_token(credentials.credentials, settings)
    except InvalidTokenError as exc:
        logger.info("auth.token.rejected", reason=type(exc.__cause__).__name__)
        raise AuthenticationError("Invalid or expired token.") from exc

    # Reload on every request: deactivation and role changes take effect immediately.
    user = await session.scalar(select(User).where(User.id == claims.subject))
    if user is None or not user.is_active:
        raise AuthenticationError("Invalid or expired token.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_permission(permission: Permission) -> Callable[..., Awaitable[User]]:
    """Dependency factory: authenticated user holding `permission`, else 403 (audited)."""

    async def _dependency(user: CurrentUser, session: SessionDep, meta: RequestMetaDep) -> User:
        if has_permission(user.role, permission):
            return user
        record_audit_event(
            session,
            action=AuditAction.AUTHZ_DENIED,
            outcome=AuditOutcome.DENIED,
            meta=meta,
            actor=user,
            details={"permission": permission.value},
        )
        await session.commit()
        logger.warning("authz.denied", user_id=str(user.id), permission=permission.value)
        raise PermissionDeniedError()

    return _dependency
