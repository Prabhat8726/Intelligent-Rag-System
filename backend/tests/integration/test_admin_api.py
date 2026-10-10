"""User administration (users:manage), departments and the audit-log API (audit:read)."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from docintel.auth.admin import UserAdminService
from docintel.auth.passwords import hash_password
from docintel.db.models import ApiToken, AuditLog, User
from tests.conftest import auth_headers
from tests.integration.conftest import Env

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


async def login(env: Env, email: str, password: str = PASSWORD) -> Any:
    return await env.client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def create_user(env: Env, **body: Any) -> Any:
    payload = {
        "email": f"new-{uuid.uuid4().hex[:8]}@example.test",
        "full_name": "New Analyst",
        "role": "ANALYST",
        "department_id": str(env.analyst.department_id),
        "password": PASSWORD,
        **body,
    }
    return await env.client.post("/api/v1/users", json=payload, headers=auth_headers(env.admin))


async def test_administrators_manage_users(env: Env) -> None:
    email = f"mixed.case.{uuid.uuid4().hex[:6]}@example.test"
    for user in (env.manager, env.analyst, env.viewer):
        listing = await env.client.get("/api/v1/users", headers=auth_headers(user))
        assert listing.status_code == 403
    created = await create_user(env, email=f"  {email.replace('mixed.case', 'Mixed.Case')} ")
    assert created.status_code == 201, created.text
    user = created.json()
    assert user["email"] == email
    assert user["department"]["id"] == str(env.analyst.department_id)
    assert "password" not in str(user)
    assert "hash" not in str(user)
    assert (await login(env, email)).status_code == 200

    assert (await create_user(env, email=email)).status_code == 409
    assert (await create_user(env, password="short")).status_code == 422
    assert (await create_user(env, email="not-an-email")).status_code == 422
    assert (await create_user(env, department_id=None)).status_code == 422  # needs a department
    assert (await create_user(env, department_id=str(uuid.uuid4()))).status_code == 422
    assert (await create_user(env, role="SUPERUSER")).status_code == 422

    found = await env.client.get(
        "/api/v1/users", params={"q": email.split("@")[0]}, headers=auth_headers(env.admin)
    )
    assert [item["email"] for item in found.json()["items"]] == [email]

    url = f"/api/v1/users/{user['id']}"
    promoted = await env.client.patch(
        url, json={"role": "MANAGER", "full_name": "Promoted"}, headers=auth_headers(env.admin)
    )
    assert promoted.status_code == 200, promoted.text
    assert (promoted.json()["role"], promoted.json()["full_name"]) == ("MANAGER", "Promoted")

    # Deactivation: their API tokens are revoked and their next request is refused.
    session_token = (await login(env, email)).json()["access_token"]
    token = await env.client.post(
        "/api/v1/auth/tokens",
        json={"name": "laptop", "scopes": ["documents:read"], "expires_in_days": 5},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert token.status_code == 201, token.text
    deactivated = await env.client.patch(
        url, json={"is_active": False}, headers=auth_headers(env.admin)
    )
    assert deactivated.json()["is_active"] is False
    me = await env.client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {session_token}"}
    )
    assert me.status_code == 401
    assert (await login(env, email)).status_code == 401
    async with env.maker() as session:
        revoked = await session.scalar(
            select(ApiToken.revoked_at).where(ApiToken.id == token.json()["id"])
        )
        assert revoked is not None

    # A password reset unlocks the account; the audit never holds the password.
    reactivated = await env.client.patch(
        url, json={"is_active": True}, headers=auth_headers(env.admin)
    )
    assert reactivated.status_code == 200
    reset = await env.client.post(
        f"{url}/password",
        json={"password": "another long passphrase"},
        headers=auth_headers(env.admin),
    )
    assert reset.status_code == 204
    assert (await login(env, email, "another long passphrase")).status_code == 200
    async with env.maker() as session:
        events = list(
            await session.scalars(
                select(AuditLog).where(AuditLog.entity_id == user["id"]).order_by(AuditLog.id)
            )
        )
        actions = [event.action for event in events]
        assert actions[0] == "user.created"
        assert "user.updated" in actions
        assert "user.password_reset" in actions
        assert all("passphrase" not in str(event.details) for event in events)
        update = next(event for event in events if event.action == "user.updated")
        assert update.details["changes"]["role"] == ["ANALYST", "MANAGER"]
        assert update.details["changes"]["full_name"] == ["(changed)", "(changed)"]


async def test_administrators_cannot_lock_themselves_or_everyone_out(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = f"/api/v1/users/{env.admin.id}"
    for change in ({"role": "VIEWER"}, {"is_active": False}):
        response = await env.client.patch(own, json=change, headers=auth_headers(env.admin))
        assert response.status_code == 409
        assert "your own role" in response.json()["detail"]
    other = (await create_user(env, role="ADMIN", department_id=None)).json()
    assert other["department"] is None
    # As if `other` were the only other active administrator: it cannot be demoted.
    monkeypatch.setattr(UserAdminService, "_active_admins", AsyncMock(return_value=0))
    for change in (
        {"is_active": False},
        {"role": "MANAGER", "department_id": str(env.analyst.department_id)},
    ):
        response = await env.client.patch(
            f"/api/v1/users/{other['id']}", json=change, headers=auth_headers(env.admin)
        )
        assert response.status_code == 409
        assert "last active administrator" in response.json()["detail"]


async def test_departments(env: Env) -> None:
    listing = await env.client.get("/api/v1/departments", headers=auth_headers(env.viewer))
    assert listing.status_code == 200
    assert str(env.analyst.department_id) in {item["id"] for item in listing.json()}
    name = f"Treasury {uuid.uuid4().hex[:6]}"
    created = await env.client.post(
        "/api/v1/departments", json={"name": name}, headers=auth_headers(env.admin)
    )
    assert created.status_code == 201, created.text
    duplicate = await env.client.post(
        "/api/v1/departments", json={"name": name.upper()}, headers=auth_headers(env.admin)
    )
    assert duplicate.status_code == 409
    forbidden = await env.client.post(
        "/api/v1/departments", json={"name": "Shadow IT"}, headers=auth_headers(env.manager)
    )
    assert forbidden.status_code == 403


async def test_the_audit_trail_is_scoped_to_the_department(env: Env) -> None:
    # Events: the analyst (Finance) and the outsider (Legal) each sign in; both are audited.
    for user in (env.analyst, env.outsider):
        async with env.maker() as session, session.begin():
            stored = await session.get(User, user.id)
            assert stored is not None
            stored.password_hash = hash_password(PASSWORD)
        assert (await login(env, user.email)).status_code == 200
    for user in (env.analyst, env.reviewer, env.viewer):
        assert (
            await env.client.get("/api/v1/audit-logs", headers=auth_headers(user))
        ).status_code == 403

    def actors(page: dict[str, Any]) -> set[str]:
        return {item["actor"]["email"] for item in page["items"] if item["actor"]}

    manager = await env.client.get(
        "/api/v1/audit-logs",
        params={"action": "auth.", "limit": 200},
        headers=auth_headers(env.manager),
    )
    assert manager.status_code == 200, manager.text
    seen = manager.json()
    assert env.analyst.email in actors(seen)
    assert env.outsider.email not in actors(seen)  # another department
    assert all(item["ip_address"] is None for item in seen["items"])  # administrators only

    admin = await env.client.get(
        "/api/v1/audit-logs",
        params={"action": "auth.login.succeeded", "actor_id": str(env.outsider.id)},
        headers=auth_headers(env.admin),
    )
    (event,) = admin.json()["items"]
    assert event["actor"]["email"] == env.outsider.email
    assert event["ip_address"] == "203.0.113.10"

    # Keyset paging: newest first, no overlaps.
    first = await env.client.get(
        "/api/v1/audit-logs", params={"limit": 2}, headers=auth_headers(env.admin)
    )
    page = first.json()
    assert len(page["items"]) == 2
    assert page["items"][0]["id"] > page["items"][1]["id"]
    following = await env.client.get(
        "/api/v1/audit-logs",
        params={"limit": 2, "before_id": page["next_before_id"]},
        headers=auth_headers(env.admin),
    )
    assert following.json()["items"][0]["id"] < page["items"][1]["id"]
