"""Demo identity data for local/staging-free environments (`docintel seed`)."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import SYSTEM_REQUEST
from docintel.auth.passwords import validate_password_policy
from docintel.auth.service import UserService
from docintel.db.models import Department, Role, User

SEED_DEPARTMENTS: tuple[str, ...] = ("Finance", "Procurement", "Legal", "Operations")


@dataclass(frozen=True, slots=True)
class SeedUser:
    email: str
    full_name: str
    role: Role
    department: str | None


SEED_USERS: tuple[SeedUser, ...] = (
    SeedUser("admin@docintel.local", "Platform Administrator", Role.ADMIN, None),
    SeedUser("manager@docintel.local", "Finance Manager", Role.MANAGER, "Finance"),
    SeedUser("analyst@docintel.local", "Finance Analyst", Role.ANALYST, "Finance"),
    SeedUser("reviewer@docintel.local", "Finance Reviewer", Role.REVIEWER, "Finance"),
    SeedUser("viewer@docintel.local", "Finance Viewer", Role.VIEWER, "Finance"),
    SeedUser(
        "procurement.analyst@docintel.local", "Procurement Analyst", Role.ANALYST, "Procurement"
    ),
    SeedUser("legal.reviewer@docintel.local", "Legal Reviewer", Role.REVIEWER, "Legal"),
)


@dataclass(slots=True)
class SeedReport:
    created_users: list[str] = field(default_factory=list)
    existing_users: list[str] = field(default_factory=list)


async def seed_demo_identities(session: AsyncSession, *, password: str) -> SeedReport:
    """Idempotently create demo departments and users. Existing users are left untouched."""
    validate_password_policy(password)
    service = UserService(session)
    departments: dict[str, Department] = {}
    for name in SEED_DEPARTMENTS:
        departments[name] = await service.get_or_create_department(name, meta=SYSTEM_REQUEST)
    await session.flush()

    report = SeedReport()
    existing = set(
        await session.scalars(
            select(User.email).where(User.email.in_([u.email for u in SEED_USERS]))
        )
    )
    for seed_user in SEED_USERS:
        if seed_user.email in existing:
            report.existing_users.append(seed_user.email)
            continue
        await service.create_user(
            email=seed_user.email,
            full_name=seed_user.full_name,
            password=password,
            role=seed_user.role,
            department=departments[seed_user.department] if seed_user.department else None,
            meta=SYSTEM_REQUEST,
        )
        report.created_users.append(seed_user.email)
    await session.commit()
    return report
