"""Browser sessions: refresh tokens in an httpOnly cookie, rotated on every use (ADR-063).

* A sign-in starts a token *family*. Each refresh marks the presented token replaced and issues
  a new one of the same family, so a token works exactly once.
* Presenting a replaced token means someone else holds a copy (theft or a replayed cookie): the
  whole family is revoked and audited, and the legitimate user signs in again. A token revoked
  by logout, deactivation or a password reset is simply refused.
* Two limits: a token unused for AUTH_REFRESH_IDLE_HOURS expires (idle timeout); a family ends
  AUTH_SESSION_MAX_HOURS after the sign-in, whatever its activity (absolute timeout).
* Only the token's SHA-256 is stored. Logout, deactivation and a password reset revoke tokens;
  the short-lived access token (JWT) then lapses within JWT_ACCESS_TOKEN_TTL_MINUTES, and a
  deactivated user is refused at once because the user is re-read on every request.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.tokens import IssuedToken, create_access_token
from docintel.core.config import Settings
from docintel.core.errors import AuthenticationError
from docintel.core.logging import get_logger
from docintel.db.models import ActorType, AuditOutcome, RefreshToken, User

logger = get_logger(__name__)

COOKIE_NAME = "docintel_refresh"
COOKIE_PATH = "/api/v1/auth"
# Sent by the SPA on cookie-authenticated calls. A cross-site page cannot add a custom header
# without a CORS preflight, which only the configured origins pass (CSRF defence in depth on
# top of SameSite=Strict).
CSRF_HEADER = "X-Docintel-Session"
SESSION_ENDED = "Your session has ended. Sign in again."
TOKEN_BYTES = 32


class RevokeReason:
    LOGOUT = "logout"
    REUSE = "reuse_detected"
    DEACTIVATED = "user_deactivated"
    PASSWORD_RESET = "password_reset"  # noqa: S105 - a reason, not a secret


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IssuedSession:
    user: User
    access: IssuedToken
    refresh_token: str
    refresh_expires_at: datetime


class SessionService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    def _new_token(
        self, user: User, *, family_id: uuid.UUID, session_expires_at: datetime, now: datetime
    ) -> tuple[RefreshToken, str]:
        secret = secrets.token_urlsafe(TOKEN_BYTES)
        idle = now + timedelta(hours=self._settings.auth_refresh_idle_hours)
        token = RefreshToken(
            id=uuid.uuid4(),
            user_id=user.id,
            family_id=family_id,
            token_hash=hash_refresh_token(secret),
            created_at=now,
            expires_at=min(idle, session_expires_at),
            session_expires_at=session_expires_at,
        )
        self._session.add(token)
        return token, secret

    def start(self, user: User, *, now: datetime | None = None) -> IssuedSession:
        """A new family for a successful sign-in. The caller commits."""
        now = now or datetime.now(UTC)
        session_end = now + timedelta(hours=self._settings.auth_session_max_hours)
        token, secret = self._new_token(
            user, family_id=uuid.uuid4(), session_expires_at=session_end, now=now
        )
        access = create_access_token(user_id=user.id, role=user.role, settings=self._settings)
        return IssuedSession(user, access, secret, token.expires_at)

    async def refresh(self, presented: str | None, meta: RequestMeta) -> IssuedSession:
        """Rotate the presented token. Commits; raises AuthenticationError (session over)."""
        now = datetime.now(UTC)
        token = (
            await self._session.scalar(
                select(RefreshToken)
                .where(RefreshToken.token_hash == hash_refresh_token(presented))
                .with_for_update(of=RefreshToken)
            )
            if presented
            else None
        )
        if token is None:
            raise AuthenticationError(SESSION_ENDED)
        if token.replaced_at is not None:
            await self._reuse(token, meta, now)
        if token.revoked_at is not None:  # logged out, deactivated, password reset
            raise AuthenticationError(SESSION_ENDED)
        if token.expires_at <= now:
            raise AuthenticationError(SESSION_ENDED)
        user = await self._session.get(User, token.user_id)
        # A login lockout does not end sessions: failing passwords must not sign a victim out.
        if user is None or not user.is_active:
            await self._revoke_family(token.family_id, RevokeReason.DEACTIVATED, now)
            await self._session.commit()
            raise AuthenticationError(SESSION_ENDED)
        token.replaced_at = now
        new_token, secret = self._new_token(
            user, family_id=token.family_id, session_expires_at=token.session_expires_at, now=now
        )
        access = create_access_token(user_id=user.id, role=user.role, settings=self._settings)
        await self._session.commit()
        return IssuedSession(user, access, secret, new_token.expires_at)

    async def _reuse(self, token: RefreshToken, meta: RequestMeta, now: datetime) -> NoReturn:
        """A replaced token was presented again: someone holds a copy. End the family."""
        revoked = await self._revoke_family(token.family_id, RevokeReason.REUSE, now)
        record_audit_event(
            self._session,
            action=AuditAction.AUTH_REFRESH_REUSED,
            outcome=AuditOutcome.DENIED,
            meta=meta,
            actor_type=ActorType.ANONYMOUS,
            entity_type="user",
            entity_id=token.user_id,
            details={"family": str(token.family_id)[:8], "tokens_revoked": revoked},
        )
        logger.warning("auth.refresh_reused", user_id=str(token.user_id))
        await self._session.commit()
        raise AuthenticationError(SESSION_ENDED)

    async def logout(self, presented: str | None, meta: RequestMeta) -> None:
        """Revoke the presented token's family (idempotent). Commits."""
        if not presented:
            return
        token = await self._session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(presented))
        )
        if token is None:
            return
        now = datetime.now(UTC)
        revoked = await self._revoke_family(token.family_id, RevokeReason.LOGOUT, now)
        if revoked:
            user = await self._session.get(User, token.user_id)
            record_audit_event(
                self._session,
                action=AuditAction.AUTH_LOGOUT,
                outcome=AuditOutcome.SUCCESS,
                meta=meta,
                actor=user,
                entity_type="user",
                entity_id=token.user_id,
            )
        await self._session.commit()

    async def _revoke_family(self, family_id: uuid.UUID, reason: str, now: datetime) -> int:
        result = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now, revoke_reason=reason)
            .returning(RefreshToken.id)
        )
        return len(result.all())


async def revoke_user_sessions(session: AsyncSession, user_id: uuid.UUID, reason: str) -> int:
    """End every browser session of a user (deactivation, password reset). The caller commits."""
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoke_reason=reason)
        .returning(RefreshToken.id)
    )
    return len(result.all())
