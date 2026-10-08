from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel import cli
from docintel.auth.passwords import PasswordPolicyError
from docintel.auth.seed import SEED_USERS, seed_demo_identities
from docintel.core.errors import ConflictError
from docintel.db.models import AuditLog, Department, User
from tests.conftest import PRODUCTION_SECRET, make_settings

pytestmark = pytest.mark.integration

SEED_PASSWORD = "demo passphrase for local use"


async def test_seed_creates_departments_and_users_idempotently(db_session: AsyncSession) -> None:
    first = await seed_demo_identities(db_session, password=SEED_PASSWORD)
    assert sorted(first.created_users) == sorted(u.email for u in SEED_USERS)
    assert first.existing_users == []

    second = await seed_demo_identities(db_session, password=SEED_PASSWORD)
    assert second.created_users == []
    assert sorted(second.existing_users) == sorted(u.email for u in SEED_USERS)

    admin = await db_session.scalar(select(User).where(User.email == "admin@docintel.local"))
    assert admin is not None
    assert admin.department_id is None
    analyst = await db_session.scalar(select(User).where(User.email == "analyst@docintel.local"))
    assert analyst is not None
    assert analyst.department is not None
    assert analyst.department.name == "Finance"
    department_count = await db_session.scalar(select(func.count()).select_from(Department))
    assert (department_count or 0) >= 4
    created_events = await db_session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "user.created")
    )
    assert created_events == len(SEED_USERS)


async def test_seed_rejects_weak_password(db_session: AsyncSession) -> None:
    with pytest.raises(PasswordPolicyError):
        await seed_demo_identities(db_session, password="short")


async def test_create_user_rejects_duplicate_email(db_session: AsyncSession) -> None:
    from docintel.audit.service import SYSTEM_REQUEST
    from docintel.auth.service import UserService
    from docintel.db.models import Role

    service = UserService(db_session)
    await service.create_user(
        email="dup@example.test",
        full_name="First",
        password=SEED_PASSWORD,
        role=Role.VIEWER,
        department=None,
        meta=SYSTEM_REQUEST,
    )
    with pytest.raises(ConflictError):
        await service.create_user(
            email="DUP@example.test",
            full_name="Second",
            password=SEED_PASSWORD,
            role=Role.VIEWER,
            department=None,
            meta=SYSTEM_REQUEST,
        )


async def test_cli_seed_refuses_in_production(capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(
        app_env="production",
        jwt_secret_key=PRODUCTION_SECRET,
        seed_user_password=SEED_PASSWORD,
    )
    assert await cli._seed(settings) == cli.EXIT_USAGE
    assert "Refusing" in capsys.readouterr().out


async def test_cli_seed_requires_password(capsys: pytest.CaptureFixture[str]) -> None:
    assert await cli._seed(make_settings(seed_user_password=None)) == cli.EXIT_USAGE
    assert "SEED_USER_PASSWORD" in capsys.readouterr().out


async def test_cli_check_ai_without_key_fails_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    assert await cli._check_ai(make_settings(gemini_api_key=None)) == cli.EXIT_FAILURE
    output = capsys.readouterr().out
    assert "GEMINI_API_KEY is not set" in output
    assert "synthetic" in output
