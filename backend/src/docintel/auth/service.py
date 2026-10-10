"""Authentication use-cases: login with lockout, and user creation.

Security properties (tested in tests/security):
* Unknown email, wrong password, inactive and locked accounts all raise the same
  AuthenticationError message - no account enumeration through responses.
* Unknown emails still pay for one argon2 verification - no enumeration through timing.
* Failed attempts are counted in the database (row-locked), so lockout holds across replicas.
* Every outcome is written to the append-only audit log in the same transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.passwords import (
    burn_verification_time_async,
    hash_password_async,
    validate_password_policy,
    verify_password_async,
)
from docintel.auth.sessions import IssuedSession, SessionService
from docintel.auth.tokens import IssuedToken, create_access_token
from docintel.core.config import Settings
from docintel.core.errors import AuthenticationError, ConflictError
from docintel.core.logging import get_logger
from docintel.db.models import ActorType, AuditOutcome, Department, Role, User

logger = get_logger(__name__)

INVALID_CREDENTIALS = "Invalid email or password."
_AUDIT_EMAIL_MAX = 320


def normalize_email(email: str) -> str:
    return email.strip().lower()


@dataclass(frozen=True, slots=True)
class LoginResult:
    user: User
    token: IssuedToken
    session: IssuedSession | None = None  # a browser session's refresh token (ADR-063)


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def login(
        self, *, email: str, password: str, meta: RequestMeta, browser_session: bool = False
    ) -> LoginResult:
        normalized = normalize_email(email)
        now = datetime.now(UTC)
        user = await self._session.scalar(
            select(User).where(User.email == normalized).with_for_update(of=User)
        )

        if user is None:
            await burn_verification_time_async(password)
            await self._fail(meta, user=None, email=normalized, reason="unknown_email")

        if not user.is_active:
            await burn_verification_time_async(password)
            await self._fail(meta, user=user, email=normalized, reason="inactive")

        if user.locked_until is not None and user.locked_until > now:
            await burn_verification_time_async(password)
            await self._fail(
                meta, user=user, email=normalized, reason="locked", outcome=AuditOutcome.DENIED
            )

        valid, replacement_hash = await verify_password_async(password, user.password_hash)
        if not valid:
            await self._register_failed_attempt(user, now=now, meta=meta)
            await self._fail(meta, user=user, email=normalized, reason="bad_password")

        if replacement_hash is not None:
            user.password_hash = replacement_hash
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        record_audit_event(
            self._session,
            action=AuditAction.AUTH_LOGIN_SUCCEEDED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=user,
            entity_type="user",
            entity_id=user.id,
            details={"browser_session": browser_session},
        )
        issued = (
            SessionService(self._session, self._settings).start(user, now=now)
            if browser_session
            else None
        )
        await self._session.commit()
        logger.info("auth.login.succeeded", user_id=str(user.id))
        if issued is not None:
            return LoginResult(user=user, token=issued.access, session=issued)
        token = create_access_token(user_id=user.id, role=user.role, settings=self._settings)
        return LoginResult(user=user, token=token)

    async def _register_failed_attempt(
        self, user: User, *, now: datetime, meta: RequestMeta
    ) -> None:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= self._settings.auth_max_failed_logins:
            user.locked_until = now + timedelta(minutes=self._settings.auth_lockout_minutes)
            user.failed_login_attempts = 0
            record_audit_event(
                self._session,
                action=AuditAction.AUTH_ACCOUNT_LOCKED,
                outcome=AuditOutcome.SUCCESS,
                meta=meta,
                actor_type=ActorType.SYSTEM,
                entity_type="user",
                entity_id=user.id,
                details={"locked_until": user.locked_until.isoformat()},
            )
            logger.warning("auth.account.locked", user_id=str(user.id))

    async def _fail(
        self,
        meta: RequestMeta,
        *,
        user: User | None,
        email: str,
        reason: str,
        outcome: AuditOutcome = AuditOutcome.FAILURE,
    ) -> NoReturn:
        record_audit_event(
            self._session,
            action=AuditAction.AUTH_LOGIN_FAILED,
            outcome=outcome,
            meta=meta,
            actor_type=ActorType.ANONYMOUS,
            entity_type="user" if user else None,
            entity_id=user.id if user else None,
            details={"email": email[:_AUDIT_EMAIL_MAX], "reason": reason},
        )
        await self._session.commit()
        logger.info("auth.login.failed", reason=reason)
        raise AuthenticationError(INVALID_CREDENTIALS)


class UserService:
    """User administration used by the CLI now and the admin API in Phase 8."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_department(self, name: str, *, meta: RequestMeta) -> Department:
        department = await self._session.scalar(select(Department).where(Department.name == name))
        if department is None:
            department = Department(id=uuid.uuid4(), name=name)
            self._session.add(department)
            record_audit_event(
                self._session,
                action=AuditAction.DEPARTMENT_CREATED,
                outcome=AuditOutcome.SUCCESS,
                meta=meta,
                actor_type=ActorType.SYSTEM,
                entity_type="department",
                entity_id=department.id,
                details={"name": name},
            )
        return department

    async def create_user(
        self,
        *,
        email: str,
        full_name: str,
        password: str,
        role: Role,
        department: Department | None,
        meta: RequestMeta,
        actor: User | None = None,
    ) -> User:
        validate_password_policy(password)
        normalized = normalize_email(email)
        if await self._session.scalar(select(User.id).where(User.email == normalized)):
            msg = f"A user with email {normalized} already exists."
            raise ConflictError(msg)
        user = User(
            id=uuid.uuid4(),
            email=normalized,
            full_name=full_name.strip(),
            password_hash=await hash_password_async(password),
            role=role,
            department_id=department.id if department else None,
        )
        self._session.add(user)
        record_audit_event(
            self._session,
            action=AuditAction.USER_CREATED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            actor_type=ActorType.USER if actor else ActorType.SYSTEM,
            entity_type="user",
            entity_id=user.id,
            details={"role": role.value, "department": department.name if department else None},
        )
        return user
