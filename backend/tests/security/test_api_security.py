"""Security behaviour of the HTTP surface (docs/architecture/09-security-architecture.md §3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.auth import service as auth_service
from docintel.auth.tokens import create_access_token
from docintel.db.models import Role, User
from tests.conftest import TEST_JWT_SECRET, TEST_PASSWORD, make_settings, make_user

pytestmark = pytest.mark.integration


async def _login_raw(client: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _comparable(response: httpx.Response) -> tuple[int, str, str, str | None]:
    body = response.json()
    return (
        response.status_code,
        body["title"],
        body["detail"],
        response.headers.get("WWW-Authenticate"),
    )


async def test_login_failures_are_indistinguishable(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    active = await make_user(db_session)
    inactive = await make_user(db_session, is_active=False)

    wrong_password = await _login_raw(client, active.email, "wrong password")
    unknown_user = await _login_raw(client, "nobody@example.test", "wrong password")
    inactive_user = await _login_raw(client, inactive.email, TEST_PASSWORD)

    assert _comparable(wrong_password) == _comparable(unknown_user) == _comparable(inactive_user)
    assert _comparable(wrong_password)[0] == 401


async def test_unknown_email_still_pays_for_password_hashing(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def _spy(password: str) -> None:
        calls.append(password)

    monkeypatch.setattr(auth_service, "burn_verification_time_async", _spy)
    await _login_raw(client, "ghost@example.test", "some password")
    assert calls == ["some password"]


@pytest.mark.parametrize(
    "make_token",
    [
        pytest.param(
            lambda user: jwt.encode(
                {"sub": str(user.id), "role": "ADMIN"}, TEST_JWT_SECRET, algorithm="HS256"
            ),
            id="missing-claims",
        ),
        pytest.param(
            lambda user: (
                create_access_token(
                    user_id=user.id,
                    role=user.role,
                    settings=make_settings(
                        jwt_secret_key="a-completely-different-signing-key-xyz-123"
                    ),
                ).token
            ),
            id="wrong-key",
        ),
        pytest.param(
            lambda user: (
                create_access_token(
                    user_id=user.id,
                    role=user.role,
                    settings=make_settings(),
                    now=datetime.now(UTC) - timedelta(days=1),
                ).token
            ),
            id="expired",
        ),
        pytest.param(
            lambda user: (
                create_access_token(
                    user_id=user.id,
                    role=user.role,
                    settings=make_settings(jwt_audience="other-api"),
                ).token
            ),
            id="wrong-audience",
        ),
        pytest.param(
            lambda user: (
                create_access_token(
                    user_id=uuid.uuid4(), role=Role.ADMIN, settings=make_settings()
                ).token
            ),
            id="unknown-subject",
        ),
    ],
)
async def test_invalid_tokens_are_rejected(
    client: httpx.AsyncClient, db_session: AsyncSession, make_token: object
) -> None:
    user: User = await make_user(db_session, role=Role.VIEWER)
    token = make_token(user)  # type: ignore[operator]
    response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired token."


async def test_token_role_claim_cannot_escalate(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Authorization uses the role stored in the database, not the role claimed in the token."""
    viewer = await make_user(db_session, role=Role.VIEWER)
    forged_role_token = create_access_token(
        user_id=viewer.id, role=Role.ADMIN, settings=make_settings()
    ).token
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {forged_role_token}"}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "VIEWER"
    assert "users:manage" not in response.json()["permissions"]


async def test_validation_errors_never_echo_submitted_values(client: httpx.AsyncClient) -> None:
    secret = "Sup3r-Secret-Value-That-Must-Not-Leak"
    extra_field = await client.post(
        "/api/v1/auth/login",
        json={"email": "a@example.test", "password": secret, "unexpected": secret},
    )
    too_long = await client.post(
        "/api/v1/auth/login", json={"email": "a@example.test", "password": secret * 20}
    )
    for response in (extra_field, too_long):
        assert response.status_code == 422
        assert response.headers["content-type"] == "application/problem+json"
        assert secret not in response.text
        assert response.json()["errors"]


async def test_unhandled_errors_are_generic_problem_responses(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    @app.get("/api/v1/_test/crash")
    async def _crash() -> None:
        msg = "internal detail: connection string postgres://admin:hunter2@db"
        raise RuntimeError(msg)

    response = await client.get("/api/v1/_test/crash")
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    body = response.json()
    assert body["detail"] == "An unexpected error occurred."
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert "hunter2" not in response.text
    assert "Traceback" not in response.text


async def test_cors_is_closed_by_default(client: httpx.AsyncClient) -> None:
    response = await client.options(
        "/api/v1/auth/login",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in response.headers
