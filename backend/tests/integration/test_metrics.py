"""GET /metrics: access control, process metrics and the gauges read from the database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from prometheus_client import REGISTRY
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.api.app import create_app
from docintel.api.problems import PROBLEM_CONTENT_TYPE
from docintel.core.config import Settings
from docintel.db.models import JobStatus, JobType, LLMCall, LLMCallStatus, ProcessingJob
from docintel.db.session import create_engine
from tests.conftest import PRODUCTION_SECRET, ClientFactory, make_settings

pytestmark = pytest.mark.integration


def values(text: str) -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    return {
        (sample.name, frozenset(sample.labels.items())): sample.value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
    }


def value(text: str, name: str, **labels: str) -> float:
    return values(text).get((name, frozenset(labels.items())), 0.0)


async def scrape(client: httpx.AsyncClient) -> str:
    response = await client.get("/metrics")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    return response.text


async def test_metrics_carry_process_and_database_gauges(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    before = await scrape(client)
    assert value(before, "docintel_snapshot_success") == 1
    # Zero-filled: every job type and active status has a series even when nothing waits.
    for job_type in JobType:
        for status in ("QUEUED", "PROCESSING"):
            assert ("docintel_jobs", frozenset({("job_type", job_type), ("status", status)})) in (
                values(before)
            )
    assert value(before, "docintel_review_tasks_overdue", priority="URGENT") >= 0
    assert "process_cpu_seconds_total" in before

    now = datetime.now(UTC)
    db_session.add_all(
        [
            ProcessingJob(
                job_type=JobType.WORKFLOW,
                status=JobStatus.QUEUED,
                max_attempts=3,
                run_after=now - timedelta(minutes=10),
            ),
            ProcessingJob(job_type=JobType.WORKFLOW, status=JobStatus.QUEUED, max_attempts=3),
            # Waiting for a retry: queued, but not yet runnable, so it does not age the queue.
            ProcessingJob(
                job_type=JobType.WORKFLOW,
                status=JobStatus.QUEUED,
                max_attempts=3,
                run_after=now + timedelta(hours=1),
            ),
            LLMCall(
                provider="gemini",
                model="test-model",
                purpose="extraction.invoice",
                status=LLMCallStatus.SUCCEEDED,
                latency_ms=Decimal(900),
                estimated_cost_usd=Decimal("0.125"),
            ),
            LLMCall(
                provider="gemini",
                model="test-model",
                purpose="rag",
                status=LLMCallStatus.FAILED,
                latency_ms=Decimal(100),
            ),
        ]
    )
    await db_session.flush()

    after = await scrape(client)
    queued = {"job_type": "WORKFLOW", "status": "QUEUED"}
    assert value(after, "docintel_jobs", **queued) == value(before, "docintel_jobs", **queued) + 3
    assert value(after, "docintel_jobs_oldest_ready_age_seconds", job_type="WORKFLOW") >= 600
    for status, added in (("SUCCEEDED", 1), ("FAILED", 1)):
        labels = {"provider": "gemini", "status": status}
        assert value(after, "docintel_llm_calls_today", **labels) == (
            value(before, "docintel_llm_calls_today", **labels) + added
        )
    assert value(after, "docintel_llm_estimated_cost_usd_today", provider="gemini") == (
        pytest.approx(
            value(before, "docintel_llm_estimated_cost_usd_today", provider="gemini") + 0.125
        )
    )


async def test_metrics_requests_are_counted_by_route_template(client: httpx.AsyncClient) -> None:
    await client.get("/health")
    text = await scrape(client)
    assert (
        value(text, "docintel_http_requests_total", method="GET", route="/health", status="200")
        >= 1
    )


async def test_a_configured_token_is_required(client_factory: ClientFactory) -> None:
    client = await client_factory(metrics_token=PRODUCTION_SECRET)

    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": PRODUCTION_SECRET}):
        refused = await client.get("/metrics", headers=headers)
        assert refused.status_code == 401
        assert refused.headers["content-type"] == PROBLEM_CONTENT_TYPE
        assert refused.headers["WWW-Authenticate"] == "Bearer"
        assert "docintel_" not in refused.text

    allowed = await client.get("/metrics", headers={"Authorization": f"Bearer {PRODUCTION_SECRET}"})
    assert allowed.status_code == 200
    assert "docintel_snapshot_success 1.0" in allowed.text


async def test_metrics_can_be_switched_off(client_factory: ClientFactory) -> None:
    client = await client_factory(metrics_enabled=False)
    assert (await client.get("/metrics")).status_code == 404


async def test_metrics_are_not_part_of_the_public_api(client: httpx.AsyncClient) -> None:
    assert "/metrics" not in (await client.get("/openapi.json")).json()["paths"]
    assert (await client.get("/api/v1/metrics")).status_code == 404


async def test_scrape_succeeds_without_the_database_gauges_when_it_is_down() -> None:
    app = create_app(make_settings(database_url="postgresql+psycopg://u:p@127.0.0.1:1/none"))
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as http,
    ):
        text = await scrape(http)
    assert value(text, "docintel_snapshot_success") == 0
    assert "docintel_jobs{" not in text
    assert "docintel_http_requests_total" in text
    assert "127.0.0.1" not in text


async def test_database_statements_are_timed_by_verb(settings: Settings) -> None:
    def count(operation: str) -> float:
        name = "docintel_db_statement_duration_seconds_count"
        return REGISTRY.get_sample_value(name, {"operation": operation}) or 0.0

    selects, errors = count("select"), count("error")
    engine = create_engine(settings)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            with pytest.raises(ProgrammingError):
                await conn.execute(text("SELECT * FROM no_such_table"))
    finally:
        await engine.dispose()
    assert count("select") >= selects + 1
    assert count("error") == errors + 1
