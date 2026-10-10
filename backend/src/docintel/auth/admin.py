"""User and department administration (the /users API, `users:manage`: administrators).

Guards: nobody changes their own role or deactivates themselves; the last active administrator
cannot be demoted or deactivated; every role but ADMIN belongs to a department (department
scoping depends on it). Deactivation takes effect on the next request (users are reloaded per
request) and revokes the user's API tokens. Every change is audited without secrets.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import AuditAction, RequestMeta, record_audit_event
from docintel.auth.passwords import (
    PasswordPolicyError,
    hash_password_async,
    validate_password_policy,
)
from docintel.auth.service import UserService, normalize_email
from docintel.core.errors import ConflictError, NotFoundError, UnprocessableContentError
from docintel.db.models import ApiToken, AuditOutcome, Department, Role, User

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UNSET: Any = object()


@dataclass(slots=True)
class UserChanges:
    full_name: str | None = None
    role: Role | None = None
    department_id: uuid.UUID | None = _UNSET  # None: no department (administrators only)
    is_active: bool | None = None


class UserAdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def users(
        self,
        *,
        query: str | None = None,
        role: Role | None = None,
        department_id: uuid.UUID | None = None,
        active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[User], int]:
        conditions: list[ColumnElement[bool]] = []
        if query:
            pattern = f"%{query.strip().lower()}%"
            conditions.append(
                or_(User.email.like(pattern), func.lower(User.full_name).like(pattern))
            )
        if role is not None:
            conditions.append(User.role == role)
        if department_id is not None:
            conditions.append(User.department_id == department_id)
        if active is not None:
            conditions.append(User.is_active.is_(active))
        total = await self._session.scalar(
            select(func.count()).select_from(User).where(*conditions)
        )
        rows = await self._session.scalars(
            select(User).where(*conditions).order_by(User.email).limit(limit).offset(offset)
        )
        return list(rows), int(total or 0)

    async def get(self, user_id: uuid.UUID) -> User:
        user = await self._session.get(User, user_id, populate_existing=True)
        if user is None:
            raise NotFoundError("User not found.")
        return user

    async def _department(self, department_id: uuid.UUID | None) -> Department | None:
        if department_id is None:
            return None
        department = await self._session.get(Department, department_id)
        if department is None:
            raise UnprocessableContentError("Unknown department.")
        return department

    @staticmethod
    def _require_department(role: Role, department: Department | None) -> None:
        if role != Role.ADMIN and department is None:
            msg = f"A {role.value.lower()} belongs to a department (department scoping)."
            raise UnprocessableContentError(msg)

    async def create(
        self,
        actor: User,
        *,
        email: str,
        full_name: str,
        role: Role,
        department_id: uuid.UUID | None,
        password: str,
        meta: RequestMeta,
    ) -> User:
        if not _EMAIL.match(normalize_email(email)):
            raise UnprocessableContentError("Enter a valid email address.")
        if not full_name.strip():
            raise UnprocessableContentError("A user needs a name.")
        department = await self._department(department_id)
        self._require_department(role, department)
        try:
            user = await UserService(self._session).create_user(
                email=email,
                full_name=full_name,
                password=password,
                role=role,
                department=department,
                meta=meta,
                actor=actor,
            )
        except PasswordPolicyError as exc:
            raise UnprocessableContentError(str(exc)) from exc
        await self._session.flush()
        return user

    async def _active_admins(self, excluding: uuid.UUID) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == Role.ADMIN, User.is_active.is_(True), User.id != excluding)
        )
        return int(count or 0)

    async def update(
        self, actor: User, user_id: uuid.UUID, changes: UserChanges, meta: RequestMeta
    ) -> User:
        user = await self._session.scalar(
            select(User).where(User.id == user_id).with_for_update(of=User)
        )
        if user is None:
            raise NotFoundError("User not found.")
        role = changes.role or user.role
        active = user.is_active if changes.is_active is None else changes.is_active
        department_id = (
            user.department_id if changes.department_id is _UNSET else changes.department_id
        )
        if user.id == actor.id and (role != user.role or not active):
            raise ConflictError("You cannot change your own role or deactivate yourself.")
        demoted = role != Role.ADMIN or not active
        if (
            user.role == Role.ADMIN
            and user.is_active
            and demoted
            and await self._active_admins(excluding=user.id) == 0
        ):
            msg = "The last active administrator cannot be demoted or deactivated."
            raise ConflictError(msg)
        department = await self._department(department_id)
        self._require_department(role, department)
        before = {
            "full_name": user.full_name,
            "role": user.role.value,
            "department_id": str(user.department_id) if user.department_id else None,
            "is_active": user.is_active,
        }
        if changes.full_name is not None:
            if not changes.full_name.strip():
                raise UnprocessableContentError("A user needs a name.")
            user.full_name = changes.full_name.strip()
        user.role = role
        user.department_id = department.id if department else None
        user.is_active = active
        after = {
            "full_name": user.full_name,
            "role": user.role.value,
            "department_id": str(user.department_id) if user.department_id else None,
            "is_active": user.is_active,
        }
        changed = {key: [before[key], after[key]] for key in before if before[key] != after[key]}
        revoked = 0
        if before["is_active"] and not active:
            result = await self._session.execute(
                update(ApiToken)
                .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
                .returning(ApiToken.id)
            )
            revoked = len(result.all())
        if changed:
            record_audit_event(
                self._session,
                action=AuditAction.USER_UPDATED,
                outcome=AuditOutcome.SUCCESS,
                meta=meta,
                actor=actor,
                entity_type="user",
                entity_id=user.id,
                details={
                    # Names are personal data: the audit records that they changed, not to what.
                    "changes": {
                        key: value if key != "full_name" else ["(changed)", "(changed)"]
                        for key, value in changed.items()
                    },
                    "api_tokens_revoked": revoked,
                },
            )
        await self._session.flush()
        return user

    async def reset_password(
        self, actor: User, user_id: uuid.UUID, password: str, meta: RequestMeta
    ) -> None:
        try:
            validate_password_policy(password)
        except PasswordPolicyError as exc:
            raise UnprocessableContentError(str(exc)) from exc
        user = await self._session.scalar(
            select(User).where(User.id == user_id).with_for_update(of=User)
        )
        if user is None:
            raise NotFoundError("User not found.")
        user.password_hash = await hash_password_async(password)
        user.failed_login_attempts = 0
        user.locked_until = None
        record_audit_event(
            self._session,
            action=AuditAction.USER_PASSWORD_RESET,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="user",
            entity_id=user.id,
            details={"by_self": user.id == actor.id},
        )

    async def departments(self) -> list[Department]:
        return list(await self._session.scalars(select(Department).order_by(Department.name)))

    async def create_department(self, actor: User, name: str, meta: RequestMeta) -> Department:
        name = " ".join(name.split())
        if not name:
            raise UnprocessableContentError("A department needs a name.")
        exists = await self._session.scalar(
            select(Department.id).where(func.lower(Department.name) == name.lower())
        )
        if exists is not None:
            raise ConflictError(f"A department named {name} already exists.")
        department = Department(id=uuid.uuid4(), name=name)
        self._session.add(department)
        record_audit_event(
            self._session,
            action=AuditAction.DEPARTMENT_CREATED,
            outcome=AuditOutcome.SUCCESS,
            meta=meta,
            actor=actor,
            entity_type="department",
            entity_id=department.id,
            details={"name": name},
        )
        await self._session.flush()
        return department
