from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.api.deps import require_permission
from docintel.auth.permissions import Permission
from docintel.db.models import AuditLog, Department, Role, User
from tests.conftest import TEST_PASSWORD, login, make_settings, make_user

pytestmark = pytest.mark.integration


_floor: dict[str, int] = {"id": 0}


@pytest.fixture(autouse=True)
async def _only_this_tests_events(db_session: AsyncSession) -> None:
    """Other tests commit audit events too (real logins): count only this test's."""
    _floor["id"] = int(await db_session.scalar(select(func.max(AuditLog.id))) or 0)


async def _audit(session: AsyncSession, action: str) -> list[AuditLog]:
    result = await session.scalars(
        select(AuditLog)
        .where(AuditLog.action == action, AuditLog.id > _floor["id"])
        .order_by(AuditLog.id)
    )
    return list(result)


async def test_login_returns_token_and_user(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    user = await make_user(db_session, role=Role.ANALYST, department=department)
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == make_settings().jwt_access_token_ttl_minutes * 60
    assert body["user"]["email"] == user.email
    assert body["user"]["role"] == "ANALYST"
    assert body["user"]["department"]["name"] == department.name
    assert "password_hash" not in response.text

    await db_session.refresh(user)
    assert user.last_login_at is not None
    events = await _audit(db_session, "auth.login.succeeded")
    assert [e.actor_id for e in events] == [user.id]
    assert str(events[0].ip_address) == "203.0.113.10"
    assert events[0].request_id == response.headers["X-Request-ID"]


async def test_email_is_case_and_whitespace_insensitive(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session, email="mixed.case@example.test")
    token = await login(client, "  Mixed.Case@Example.TEST ")
    assert token
    assert user.email == "mixed.case@example.test"


async def test_me_returns_user_and_effective_permissions(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session, role=Role.VIEWER)
    token = await login(client, user.email)
    response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(user.id)
    assert "documents:read" in body["permissions"]
    assert "documents:upload" not in body["permissions"]


async def test_me_requires_bearer_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["content-type"] == "application/problem+json"

    basic = await client.get("/api/v1/auth/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert basic.status_code == 401


async def test_wrong_password_is_rejected_and_audited(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "not the password"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."
    await db_session.refresh(user)
    assert user.failed_login_attempts == 1
    events = await _audit(db_session, "auth.login.failed")
    assert events[-1].details == {"email": user.email, "reason": "bad_password"}
    assert events[-1].entity_id == str(user.id)


async def test_lockout_after_repeated_failures_then_recovery(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    for _ in range(3):  # auth_max_failed_logins=3 in test settings
        await client.post("/api/v1/auth/login", json={"email": user.email, "password": "wrong!"})

    await db_session.refresh(user)
    assert user.locked_until is not None
    assert user.locked_until > datetime.now(UTC)
    assert len(await _audit(db_session, "auth.account.locked")) == 1

    locked = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
    )
    assert locked.status_code == 401
    assert locked.json()["detail"] == "Invalid email or password."
    assert (await _audit(db_session, "auth.login.failed"))[-1].details["reason"] == "locked"

    user.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert await login(client, user.email)
    await db_session.refresh(user)
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


async def test_deactivated_user_token_stops_working(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    token = await login(client, user.email)
    user.is_active = False
    await db_session.commit()
    response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_permission_dependency_allows_and_denies_with_audit(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    @app.get(
        "/api/v1/_test/approve",
        dependencies=[Depends(require_permission(Permission.WORKFLOWS_APPROVE))],
    )
    async def _approve_probe() -> dict[str, str]:
        return {"status": "allowed"}

    analyst = await make_user(db_session, role=Role.ANALYST)
    reviewer = await make_user(db_session, role=Role.REVIEWER)

    denied = await client.get(
        "/api/v1/_test/approve",
        headers={"Authorization": f"Bearer {await login(client, analyst.email)}"},
    )
    assert denied.status_code == 403
    denial = (await _audit(db_session, "authz.denied"))[-1]
    assert denial.actor_id == analyst.id
    assert denial.outcome == "DENIED"
    assert denial.details == {"permission": "workflows:approve"}

    allowed = await client.get(
        "/api/v1/_test/approve",
        headers={"Authorization": f"Bearer {await login(client, reviewer.email)}"},
    )
    assert allowed.status_code == 200


async def test_role_change_applies_to_existing_tokens(
    app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    @app.get(
        "/api/v1/_test/upload",
        dependencies=[Depends(require_permission(Permission.DOCUMENTS_UPLOAD))],
    )
    async def _upload_probe() -> dict[str, str]:
        return {"status": "allowed"}

    user: User = await make_user(db_session, role=Role.ANALYST)
    headers = {"Authorization": f"Bearer {await login(client, user.email)}"}
    assert (await client.get("/api/v1/_test/upload", headers=headers)).status_code == 200

    user.role = Role.VIEWER  # demotion must not wait for token expiry
    await db_session.commit()
    assert (await client.get("/api/v1/_test/upload", headers=headers)).status_code == 403


async def test_non_ip_client_host_does_not_break_auditing(
    app: FastAPI, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    transport = httpx.ASGITransport(app=app, client=("unix-socket-client", 0))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        assert await login(http, user.email)
    event = (await _audit(db_session, "auth.login.succeeded"))[-1]
    assert event.ip_address is None
