"""Metric helpers, the worker's metrics endpoint and the HTTP metrics middleware."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from prometheus_client import REGISTRY

from docintel.api.app import create_app
from docintel.core.metrics import (
    _operation,
    bearer_matches,
    observe_model_call,
    observe_stages,
    purpose_label,
    serve_metrics,
)
from tests.conftest import PRODUCTION_SECRET, make_settings


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_purpose_label_is_a_closed_set() -> None:
    assert purpose_label("agent.plan") == "agent"
    assert purpose_label("extraction.invoice") == "extraction"
    assert purpose_label("RAG") == "rag"
    assert purpose_label("diagnostics") == "diagnostics"
    assert purpose_label("anything-a-caller-sends") == "other"
    assert purpose_label("") == "other"


def test_statement_operation_is_the_sql_verb() -> None:
    assert _operation("  select 1") == "select"
    assert _operation("INSERT INTO t VALUES (1)") == "insert"
    assert _operation("WITH x AS (SELECT 1) SELECT * FROM x") == "with"
    assert _operation("BEGIN") == "other"
    assert _operation("") == "other"


def test_model_calls_count_tokens_cost_and_latency() -> None:
    before = (
        sample("docintel_llm_calls_total", provider="ollama", purpose="agent", status="SUCCEEDED"),
        sample("docintel_llm_tokens_total", provider="ollama", kind="input"),
        sample("docintel_llm_tokens_total", provider="ollama", kind="thinking"),
        sample("docintel_llm_estimated_cost_usd_total", provider="ollama"),
        sample("docintel_llm_call_duration_seconds_count", provider="ollama", purpose="agent"),
    )
    observe_model_call(
        provider="ollama",
        purpose="agent.plan",
        status="SUCCEEDED",
        latency_ms=1500,
        tokens={"input": 120, "output": 30, "thinking": None},
        cost_usd=0.25,
    )
    after = (
        sample("docintel_llm_calls_total", provider="ollama", purpose="agent", status="SUCCEEDED"),
        sample("docintel_llm_tokens_total", provider="ollama", kind="input"),
        sample("docintel_llm_tokens_total", provider="ollama", kind="thinking"),
        sample("docintel_llm_estimated_cost_usd_total", provider="ollama"),
        sample("docintel_llm_call_duration_seconds_count", provider="ollama", purpose="agent"),
    )
    assert [b - a for a, b in zip(before, after, strict=True)] == [1, 120, 0, 0.25, 1]


def test_stage_timings_skip_values_that_are_not_numbers() -> None:
    before = sample("docintel_pipeline_stage_duration_seconds_count", stage="inspect")
    observe_stages({"inspect": 12.5, "note": "not a timing", "flags": None})
    assert sample("docintel_pipeline_stage_duration_seconds_count", stage="inspect") == before + 1
    assert sample("docintel_pipeline_stage_duration_seconds_count", stage="note") == 0


def test_bearer_token_comparison() -> None:
    token = "t" * 40
    assert bearer_matches(f"Bearer {token}", token)
    assert bearer_matches(f"bearer {token}", token)
    assert not bearer_matches(f"Bearer {token}x", token)
    assert not bearer_matches(f"Basic {token}", token)
    assert not bearer_matches(token, token)
    assert not bearer_matches(None, token)


@pytest.fixture
def worker_endpoint() -> Iterator[str]:
    server = serve_metrics(0, token=PRODUCTION_SECRET, host="127.0.0.1")
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/metrics"
    finally:
        server.shutdown()
        server.server_close()


def test_worker_endpoint_requires_the_token(worker_endpoint: str) -> None:
    assert httpx.get(worker_endpoint).status_code == 401
    wrong = httpx.get(worker_endpoint, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert wrong.headers["WWW-Authenticate"] == "Bearer"
    ok = httpx.get(worker_endpoint, headers={"Authorization": f"Bearer {PRODUCTION_SECRET}"})
    assert ok.status_code == 200
    assert "docintel_worker_jobs_total" in ok.text
    assert "process_cpu_seconds_total" in ok.text


async def test_requests_are_labelled_by_route_template_never_the_raw_path() -> None:
    # No database is touched: unauthenticated requests stop before any query.
    app = create_app(make_settings(database_url="postgresql+psycopg://u:p@127.0.0.1:1/none"))
    route = "/api/v1/documents/{document_id}"
    before = {
        "401": sample("docintel_http_requests_total", method="GET", route=route, status="401"),
        "404": sample(
            "docintel_http_requests_total", method="GET", route="unmatched", status="404"
        ),
        "other": sample("docintel_http_requests_total", method="other", route=route, status="405"),
    }
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as http,
    ):
        for _ in range(2):
            assert (await http.get(f"/api/v1/documents/{uuid.uuid4()}")).status_code == 401
        assert (await http.get(f"/no/such/{uuid.uuid4()}")).status_code == 404
        assert (await http.request("BREW", f"/api/v1/documents/{uuid.uuid4()}")).status_code == 405

    assert sample("docintel_http_requests_total", method="GET", route=route, status="401") == (
        before["401"] + 2
    )
    assert sample(
        "docintel_http_requests_total", method="GET", route="unmatched", status="404"
    ) == (before["404"] + 1)
    assert sample("docintel_http_requests_total", method="other", route=route, status="405") == (
        before["other"] + 1
    )
    # Raw paths never become label values.
    assert not any(
        "/no/such" in sample.labels.get("route", "")
        for metric in REGISTRY.collect()
        for sample in metric.samples
    )
