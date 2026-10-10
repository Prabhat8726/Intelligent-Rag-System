"""Browser sessions (ADR-063): the refresh cookie, rotation, reuse detection (and the lost
response allowance), logout and revocation on deactivation and password reset."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.auth.sessions import COOKIE_NAME, CSRF_HEADER, hash_refresh_token
from docintel.db.models import AuditLog, Department, RefreshToken, Role, User
from tests.conftest import TEST_PASSWORD, auth_headers, make_user

pytestmark = pytest.mark.integration

SPA = {CSRF_HEADER: "1"}


async def sign_in(client: httpx.AsyncClient, user: User) -> tuple[str, str]:
    """The web app's login: (access token, refresh cookie)."""
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": TEST_PASSWORD}, headers=SPA
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"], response.cookies[COOKIE_NAME]


async def refresh(client: httpx.AsyncClient, cookie: str | None) -> httpx.Response:
    """Present exactly `cookie` (the client's jar holds nothing else)."""
    client.cookies.clear()
    if cookie:
        client.cookies.set(COOKIE_NAME, cookie)
    return await client.post("/api/v1/auth/refresh", headers=SPA)


async def post_with(
    client: httpx.AsyncClient, path: str, cookie: str | None, headers: dict[str, str]
) -> httpx.Response:
    client.cookies.clear()
    if cookie:
        client.cookies.set(COOKIE_NAME, cookie)
    return await client.post(path, headers=headers)


async def events(session: AsyncSession, action: str, since: int) -> list[AuditLog]:
    return list(
        await session.scalars(
            select(AuditLog).where(AuditLog.action == action, AuditLog.id > since)
        )
    )


async def last_audit_id(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.max(AuditLog.id))) or 0)


async def test_the_web_app_gets_an_httponly_cookie_and_api_clients_do_not(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    user = await make_user(db_session, role=Role.ANALYST, department=department)
    plain = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
    )
    assert plain.status_code == 200
    assert "set-cookie" not in plain.headers
    assert (
        await db_session.scalar(
            select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user.id)
        )
        == 0
    )

    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": TEST_PASSWORD}, headers=SPA
    )
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/api/v1/auth" in lowered
    assert response.cookies[COOKIE_NAME] not in response.text  # never in the body
    (stored,) = await db_session.scalars(
        select(RefreshToken).where(RefreshToken.user_id == user.id)
    )
    assert stored.token_hash != response.cookies[COOKIE_NAME]  # only the hash is kept
    assert stored.expires_at <= stored.session_expires_at


async def test_refresh_rotates_the_cookie_and_returns_a_working_token(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    user = await make_user(db_session, role=Role.REVIEWER, department=department)
    _, cookie = await sign_in(client, user)
    refreshed = await refresh(client, cookie)
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["user"]["email"] == user.email
    rotated = refreshed.cookies[COOKIE_NAME]
    assert rotated != cookie
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.json()["role"] == "REVIEWER"
    # The rotated cookie works once more; the family carries on.
    assert (await refresh(client, rotated)).status_code == 200
    family = {
        token.family_id
        for token in await db_session.scalars(
            select(RefreshToken).where(RefreshToken.user_id == user.id)
        )
    }
    assert len(family) == 1


async def test_a_replayed_cookie_ends_the_whole_session(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    user = await make_user(db_session, role=Role.ANALYST, department=department)
    _, stolen = await sign_in(client, user)
    since = await last_audit_id(db_session)
    rotated = (await refresh(client, stolen)).cookies[COOKIE_NAME]
    legitimate = (await refresh(client, rotated)).cookies[COOKIE_NAME]
    # The copy is presented after the legitimate rotations: theft. Everything is revoked.
    replay = await refresh(client, stolen)
    assert replay.status_code == 401
    assert replay.json()["detail"] == "Your session has ended. Sign in again."
    assert f"{COOKIE_NAME}=" in replay.headers["set-cookie"]  # the browser drops the cookie
    assert (await refresh(client, legitimate)).status_code == 401
    (alarm,) = await events(db_session, "auth.refresh_reused", since)
    assert alarm.entity_id == str(user.id)
    assert alarm.outcome.value == "DENIED"
    reasons = set(
        await db_session.scalars(
            select(RefreshToken.revoke_reason).where(RefreshToken.user_id == user.id)
        )
    )
    assert reasons == {"reuse_detected"}


async def test_a_lost_refresh_response_is_not_theft(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    """A reload aborts a refresh after the server rotated the cookie: the browser presents the
    replaced cookie again. Within the grace window, with the successor unused, that works."""
    user = await make_user(db_session, role=Role.ANALYST, department=department)
    _, cookie = await sign_in(client, user)
    since = await last_audit_id(db_session)
    lost = (await refresh(client, cookie)).cookies[COOKIE_NAME]  # never reaches the browser
    retried = await refresh(client, cookie)
    assert retried.status_code == 200, retried.text
    again = await refresh(client, cookie)  # lost twice: still the same window
    assert again.status_code == 200, again.text
    current = again.cookies[COOKIE_NAME]
    assert (await refresh(client, current)).status_code == 200
    assert await events(db_session, "auth.refresh_reused", since) == []
    reasons = sorted(
        str(reason)
        for reason in await db_session.scalars(
            select(RefreshToken.revoke_reason).where(RefreshToken.user_id == user.id)
        )
    )
    # Sign-in, retried and current tokens were replaced in turn; the two lost ones superseded.
    assert reasons == ["None", "None", "None", "superseded", "superseded"]

    # A superseded cookie presented later means two holders after all: reuse.
    replay = await refresh(client, lost)
    assert replay.status_code == 401
    assert len(await events(db_session, "auth.refresh_reused", since)) == 1
    tokens = list(
        await db_session.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id))
    )
    assert all(token.revoked_at is not None for token in tokens)


async def test_the_grace_window_ends_and_a_used_successor_closes_it(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    user = await make_user(db_session, role=Role.ANALYST, department=department)
    # Too late: the replacement is older than AUTH_REFRESH_REUSE_GRACE_SECONDS.
    _, late = await sign_in(client, user)
    since = await last_audit_id(db_session)
    assert (await refresh(client, late)).status_code == 200
    await db_session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_refresh_token(late))
        .values(replaced_at=datetime.now(UTC) - timedelta(seconds=11))
    )
    await db_session.flush()
    assert (await refresh(client, late)).status_code == 401
    assert len(await events(db_session, "auth.refresh_reused", since)) == 1

    # In time, but the successor has been used: the response was not lost.
    _, early = await sign_in(client, user)
    since = await last_audit_id(db_session)
    successor = (await refresh(client, early)).cookies[COOKIE_NAME]
    assert (await refresh(client, successor)).status_code == 200
    assert (await refresh(client, early)).status_code == 401
    assert len(await events(db_session, "auth.refresh_reused", since)) == 1


async def test_logout_revokes_the_session_without_an_alarm(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    user = await make_user(db_session, role=Role.ANALYST, department=department)
    _, cookie = await sign_in(client, user)
    since = await last_audit_id(db_session)
    unguarded = await post_with(client, "/api/v1/auth/logout", cookie, {})
    assert unguarded.status_code == 403  # no CSRF header: nothing happens
    still = await refresh(client, cookie)  # still signed in; the cookie rotates
    assert still.status_code == 200
    current = still.cookies[COOKIE_NAME]
    out = await post_with(client, "/api/v1/auth/logout", current, SPA)
    assert out.status_code == 204
    assert (
        'docintel_refresh=""' in out.headers["set-cookie"]
        or "Max-Age=0" in out.headers["set-cookie"]
    )
    # The tab that still holds the cookie is refused, quietly.
    assert (await refresh(client, current)).status_code == 401
    assert [e.action for e in await events(db_session, "auth.logout", since)] == ["auth.logout"]
    assert await events(db_session, "auth.refresh_reused", since) == []
    # Logging out again, or without a cookie, is harmless.
    client.cookies.clear()
    assert (await client.post("/api/v1/auth/logout", headers=SPA)).status_code == 204


async def test_refresh_needs_the_header_and_a_live_cookie(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    user = await make_user(db_session, role=Role.ANALYST, department=department)
    _, cookie = await sign_in(client, user)
    no_header = await post_with(client, "/api/v1/auth/refresh", cookie, {})
    assert no_header.status_code == 403
    assert (await refresh(client, None)).status_code == 401
    assert (await refresh(client, "not-a-token")).status_code == 401
    # Idle for longer than the limit: expired.
    await db_session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id)
        .values(
            created_at=datetime.now(UTC) - timedelta(hours=13),
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await db_session.flush()
    assert (await refresh(client, cookie)).status_code == 401


async def test_deactivation_and_password_reset_end_sessions(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    admin = await make_user(db_session, role=Role.ADMIN)
    user = await make_user(db_session, role=Role.ANALYST, department=department)
    _, first = await sign_in(client, user)
    reset = await client.post(
        f"/api/v1/users/{user.id}/password",
        json={"password": "a brand new passphrase 2026"},
        headers=auth_headers(admin),
    )
    assert reset.status_code == 204, reset.text
    assert (await refresh(client, first)).status_code == 401

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "a brand new passphrase 2026"},
        headers=SPA,
    )
    second = login.cookies[COOKIE_NAME]
    deactivated = await client.patch(
        f"/api/v1/users/{user.id}", json={"is_active": False}, headers=auth_headers(admin)
    )
    assert deactivated.status_code == 200, deactivated.text
    assert (await refresh(client, second)).status_code == 401
    reasons = list(
        await db_session.scalars(
            select(RefreshToken.revoke_reason)
            .where(RefreshToken.user_id == user.id)
            .order_by(RefreshToken.created_at)
        )
    )
    assert reasons == ["password_reset", "user_deactivated"]
