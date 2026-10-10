"""Request rate limits shared by every API replica (Phase 11, ADR-074).

A fixed one-minute window per key, counted with one upsert in `rate_limit_counters`, in its own
committed transaction: a request that fails afterwards (a wrong password) still counts, and the
count holds across replicas, which in-process counters would not. Login is counted per client
IP (the account lockout already counts per account); the other scopes per user. Over the
limit, the request is refused with 429 and `Retry-After` before the endpoint runs.

The web container adds coarse per-IP limits at the edge (nginx `limit_req`); these are the
per-caller limits that need to know who the caller is.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Awaitable, Callable
from datetime import timedelta
from enum import StrEnum

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from docintel.api.deps import CurrentUser, SettingsDep
from docintel.core.config import Settings
from docintel.core.errors import TooManyRequestsError
from docintel.core.logging import get_logger
from docintel.core.metrics import RATE_LIMITED

logger = get_logger(__name__)

WINDOW = timedelta(minutes=1)
# Expired counters are deleted by about one hit in this many.
CLEANUP_ONE_IN = 50

_HIT = text(
    """
    INSERT INTO rate_limit_counters (key, window_start, expires_at, hits)
    SELECT :key, start, start + :window, 1
    FROM (SELECT date_bin(:window, clock_timestamp(), TIMESTAMPTZ '2000-01-01') AS start) AS w
    ON CONFLICT (key, window_start)
        DO UPDATE SET hits = rate_limit_counters.hits + 1
    RETURNING hits, EXTRACT(EPOCH FROM expires_at - clock_timestamp())
    """
)
_CLEANUP = text("DELETE FROM rate_limit_counters WHERE expires_at < clock_timestamp()")


class LimitScope(StrEnum):
    LOGIN = "login"
    UPLOAD = "upload"
    SEARCH = "search"
    AI = "ai"


def limit_for(scope: LimitScope, settings: Settings) -> int:
    return {
        LimitScope.LOGIN: settings.rate_limit_login_per_minute,
        LimitScope.UPLOAD: settings.rate_limit_uploads_per_minute,
        LimitScope.SEARCH: settings.rate_limit_search_per_minute,
        LimitScope.AI: settings.rate_limit_ai_per_minute,
    }[scope]


async def hit(engine: AsyncEngine, key: str) -> tuple[int, float]:
    """Count one request for `key`; returns (requests in this window, seconds left in it)."""
    async with engine.begin() as conn:
        hits, remaining = (await conn.execute(_HIT, {"key": key, "window": WINDOW})).one()
        if secrets.randbelow(CLEANUP_ONE_IN) == 0:
            await conn.execute(_CLEANUP)
    return int(hits), float(remaining)


async def check(request: Request, settings: Settings, scope: LimitScope, caller: str) -> None:
    if not settings.rate_limit_enabled:
        return
    hits, remaining = await hit(request.app.state.engine, f"{scope.value}:{caller}")
    if hits > limit_for(scope, settings):
        RATE_LIMITED.labels(scope.value).inc()
        retry_after = max(1, math.ceil(remaining))
        logger.info("http.rate_limited", scope=scope.value, hits=hits)
        raise TooManyRequestsError(
            f"Too many requests; try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


def client_ip(request: Request) -> str:
    # Behind the web container this is the address nginx saw (it overwrites X-Forwarded-For).
    return request.client.host if request.client else "unknown"


def per_ip(scope: LimitScope) -> Callable[..., Awaitable[None]]:
    async def dependency(request: Request, settings: SettingsDep) -> None:
        await check(request, settings, scope, client_ip(request))

    return dependency


def per_user(scope: LimitScope) -> Callable[..., Awaitable[None]]:
    async def dependency(request: Request, settings: SettingsDep, user: CurrentUser) -> None:
        await check(request, settings, scope, str(user.id))

    return dependency


LOGIN_LIMIT = Depends(per_ip(LimitScope.LOGIN))
UPLOAD_LIMIT = Depends(per_user(LimitScope.UPLOAD))
SEARCH_LIMIT = Depends(per_user(LimitScope.SEARCH))
AI_LIMIT = Depends(per_user(LimitScope.AI))
