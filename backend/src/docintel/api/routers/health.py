"""Liveness and readiness probes (unauthenticated, minimal information)."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from docintel import __version__
from docintel.api.schemas.health import CheckResult, LivenessResponse, ReadinessResponse
from docintel.core.logging import get_logger
from docintel.db.migrations_runner import head_revisions

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

_DB_CHECK_TIMEOUT_SECONDS = 3.0


@router.get("/health", response_model=LivenessResponse, summary="Liveness probe")
async def liveness() -> LivenessResponse:
    return LivenessResponse()


async def _check_database(engine: AsyncEngine) -> tuple[CheckResult, set[str] | None]:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(_DB_CHECK_TIMEOUT_SECONDS), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            revisions = {
                row[0]
                for row in await conn.execute(text("SELECT version_num FROM alembic_version"))
            }
    except Exception as exc:  # any failure means "not ready"; details go to logs only
        logger.warning("readiness.database_failed", error_type=type(exc).__name__)
        return CheckResult(status="fail", detail="database unavailable"), None
    latency = round((time.perf_counter() - started) * 1000, 2)
    return CheckResult(status="ok", latency_ms=latency), revisions


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe: database reachable and schema at the expected migration",
    responses={503: {"model": ReadinessResponse}},
)
async def readiness(request: Request, response: Response) -> ReadinessResponse:
    engine: AsyncEngine = request.app.state.engine
    database, revisions = await _check_database(engine)
    checks = {"database": database}

    if revisions is None:
        checks["migrations"] = CheckResult(status="fail", detail="unknown (database unavailable)")
    elif revisions == set(head_revisions()):
        checks["migrations"] = CheckResult(status="ok")
    else:
        checks["migrations"] = CheckResult(status="fail", detail="schema is not at migration head")

    ready = all(check.status == "ok" for check in checks.values())
    if not ready:
        response.status_code = 503
    return ReadinessResponse(
        status="ready" if ready else "not_ready", version=__version__, checks=checks
    )
