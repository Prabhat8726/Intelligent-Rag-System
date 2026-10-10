"""Per-caller request limits counted in the database (ADR-074)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from docintel.api import rate_limit
from docintel.api.problems import PROBLEM_CONTENT_TYPE
from docintel.db.models import RateLimitCounter, Role
from tests.conftest import ClientFactory, auth_headers, make_user

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
WRONG = {"email": "nobody@example.test", "password": "not the password"}


@pytest.fixture
async def counters(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """An empty counter table, at least 15 s before the end of the current window."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM rate_limit_counters"))
        elapsed = await conn.scalar(
            text(
                "SELECT EXTRACT(EPOCH FROM clock_timestamp() "
                "- date_bin('1 minute', clock_timestamp(), TIMESTAMPTZ '2000-01-01'))"
            )
        )
    if float(elapsed) > 45:  # the requests below must land in one window
        await asyncio.sleep(60.5 - float(elapsed))
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM rate_limit_counters"))


def limited(scope: str) -> float:
    return REGISTRY.get_sample_value("docintel_rate_limited_requests_total", {"scope": scope}) or 0


async def test_logins_are_limited_per_client_address(
    client_factory: ClientFactory, counters: AsyncEngine
) -> None:
    client = await client_factory(rate_limit_enabled=True, rate_limit_login_per_minute=3)
    before = limited("login")

    for _ in range(3):  # failed logins count too
        assert (await client.post(LOGIN, json=WRONG)).status_code == 401
    refused = await client.post(LOGIN, json=WRONG)

    assert refused.status_code == 429
    assert refused.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert 1 <= int(refused.headers["Retry-After"]) <= 60
    assert refused.json()["detail"].startswith("Too many requests; try again in")
    assert limited("login") == before + 1
    # Counted per address: another client starts its own window.
    assert (await rate_limit.hit(counters, "login:198.51.100.7"))[0] == 1


async def test_searches_are_limited_per_user(
    client_factory: ClientFactory, counters: AsyncEngine, db_session: AsyncSession
) -> None:
    client = await client_factory(rate_limit_enabled=True, rate_limit_search_per_minute=2)
    first = await make_user(db_session, role=Role.ANALYST)
    second = await make_user(db_session, role=Role.ANALYST)
    body = {"query": "invoices over 1,000"}

    for _ in range(2):
        response = await client.post("/api/v1/search", json=body, headers=auth_headers(first))
        assert response.status_code == 200, response.text
    over = await client.post("/api/v1/search", json=body, headers=auth_headers(first))
    assert over.status_code == 429
    assert "Retry-After" in over.headers

    other = await client.post("/api/v1/search", json=body, headers=auth_headers(second))
    assert other.status_code == 200
    # One counter per user and scope.
    async with counters.connect() as conn:
        keys = set((await conn.execute(select(RateLimitCounter.key))).scalars())
    assert keys == {f"search:{first.id}", f"search:{second.id}"}


async def test_anonymous_requests_are_refused_before_they_are_counted(
    client_factory: ClientFactory, counters: AsyncEngine
) -> None:
    client = await client_factory(rate_limit_enabled=True, rate_limit_ai_per_minute=1)
    for _ in range(3):
        response = await client.post("/api/v1/analysis", json={"request": "check invoice"})
        assert response.status_code == 401
    async with counters.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(RateLimitCounter)) == 0


async def test_limits_can_be_switched_off(
    client_factory: ClientFactory, counters: AsyncEngine
) -> None:
    client = await client_factory(rate_limit_enabled=False, rate_limit_login_per_minute=1)
    for _ in range(3):
        assert (await client.post(LOGIN, json=WRONG)).status_code == 401


async def test_expired_windows_are_deleted(
    counters: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    async with counters.begin() as conn:
        await conn.execute(
            insert(RateLimitCounter).values(
                key="search:old", window_start=past, expires_at=past + timedelta(minutes=1), hits=5
            )
        )
    monkeypatch.setattr(rate_limit, "CLEANUP_ONE_IN", 1)

    hits, remaining = await rate_limit.hit(counters, "search:new")
    assert hits == 1
    assert 0 < remaining <= 60

    async with counters.connect() as conn:
        keys = set((await conn.execute(select(RateLimitCounter.key))).scalars())
    assert keys == {"search:new"}
