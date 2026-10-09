"""Personal API tokens for MCP clients (Module 16).

* The token is shown once; only its SHA-256 is stored (a database leak does not leak tokens).
* Scopes are a subset of TOKEN_SCOPES and of the owner's permissions at creation; at use, the
  owner's current role is intersected with them, so a demotion takes effect immediately.
* Tokens expire (API_TOKEN_MAX_DAYS at most) and can be revoked; a deactivated owner's tokens
  stop working.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.permissions import Permission, permissions_for
from docintel.core.config import Settings
from docintel.core.errors import NotFoundError, UnprocessableContentError
from docintel.db.models import ApiToken, AuditOutcome, User

TOKEN_PREFIX = "dit_"  # noqa: S105 - the public prefix of every token, not a secret
MAX_ACTIVE_TOKENS = 10
# What a token may be scoped to: the tools' permissions. Never administration.
TOKEN_SCOPES: tuple[Permission, ...] = (
    Permission.DOCUMENTS_READ,
    Permission.KNOWLEDGE_READ,
    Permission.COMPARISONS_CREATE,
    Permission.REVIEWS_WORK,
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TokenIdentity:
    user: User
    token_id: uuid.UUID
    scopes: frozenset[str]


class ApiTokenService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def create(
        self,
        actor: User,
        *,
        name: str,
        scopes: list[str],
        expires_in_days: int,
        meta: RequestMeta,
    ) -> tuple[ApiToken, str]:
        if expires_in_days > self._settings.api_token_max_days:
            msg = f"Tokens expire after at most {self._settings.api_token_max_days} days."
            raise UnprocessableContentError(msg)
        allowed = {p.value for p in TOKEN_SCOPES} & {p.value for p in permissions_for(actor.role)}
        wanted = list(dict.fromkeys(scopes))
        refused = [scope for scope in wanted if scope not in allowed]
        if refused or not wanted:
            msg = (
                f"Scopes not available to you: {', '.join(refused)}."
                if refused
                else "Choose at least one scope."
            )
            raise UnprocessableContentError(msg)
        active = await self._session.scalars(
            select(ApiToken.id).where(
                ApiToken.user_id == actor.id,
                ApiToken.revoked_at.is_(None),
                ApiToken.expires_at > datetime.now(UTC),
            )
        )
        if len(list(active)) >= MAX_ACTIVE_TOKENS:
            msg = f"You already have {MAX_ACTIVE_TOKENS} active tokens; revoke one first."
            raise UnprocessableContentError(msg)
        secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        token = ApiToken(
            id=uuid.uuid4(),
            user_id=actor.id,
            name=" ".join(name.split())[:100],
            prefix=secret[:12],
            token_hash=hash_token(secret),
            scopes=wanted,
            created_at=now,
            expires_at=now + timedelta(days=expires_in_days),
        )
        self._session.add(token)
        await self._session.flush()
        record_audit_event(
            self._session,
            action=AuditAction.API_TOKEN_CREATED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="api_token",
            entity_id=token.id,
            details={"scopes": wanted, "expires_at": token.expires_at.isoformat()},
        )
        await self._session.commit()
        return token, secret

    async def tokens(self, actor: User) -> list[ApiToken]:
        rows = await self._session.scalars(
            select(ApiToken)
            .where(ApiToken.user_id == actor.id)
            .order_by(ApiToken.created_at.desc())
        )
        return list(rows)

    async def revoke(self, actor: User, token_id: uuid.UUID, meta: RequestMeta) -> None:
        token = await self._session.scalar(
            select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == actor.id)
        )
        if token is None:
            raise NotFoundError("Token not found.")
        if token.revoked_at is None:
            token.revoked_at = datetime.now(UTC)
            record_audit_event(
                self._session,
                action=AuditAction.API_TOKEN_REVOKED,
                outcome=AuditOutcome.SUCCESS,
                meta=meta,
                actor=actor,
                entity_type="api_token",
                entity_id=token.id,
            )
        await self._session.commit()

    async def authenticate(self, secret: str) -> TokenIdentity | None:
        """The active token's owner and scopes (None: unknown, expired, revoked or inactive)."""
        if not secret.startswith(TOKEN_PREFIX) or len(secret) > 200:
            return None
        now = datetime.now(UTC)
        row = (
            await self._session.execute(
                select(ApiToken, User)
                .join(User, User.id == ApiToken.user_id)
                .where(
                    ApiToken.token_hash == hash_token(secret),
                    ApiToken.revoked_at.is_(None),
                    ApiToken.expires_at > now,
                    User.is_active.is_(True),
                )
            )
        ).first()
        if row is None:
            return None
        token, user = row
        await self._session.execute(
            update(ApiToken).where(ApiToken.id == token.id).values(last_used_at=now)
        )
        await self._session.commit()
        return TokenIdentity(user=user, token_id=token.id, scopes=frozenset(token.scopes))
