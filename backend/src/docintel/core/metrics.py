"""Prometheus metrics (Module 24, NFR-07; ADR-073).

Process-local counters and histograms, recorded where the event happens: HTTP requests (by
route template, never the raw path), database statements, worker jobs and pipeline stages,
OCR pages, model calls (tokens, latency, estimated cost), agent runs and tool calls, and
extraction confidence. The API serves them at `/metrics` together with gauges read from the
database when scraped (queue depth, review backlog), which hold across replicas; a worker serves
its own on `WORKER_METRICS_PORT`. Labels are closed sets (route templates, enum values), so a
request can never create a new time series from user input, and no label carries document
content, names or identifiers.
"""

from __future__ import annotations

import hmac
import threading
import time
import weakref
from collections.abc import Mapping
from http.server import ThreadingHTTPServer
from typing import Any

from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.exposition import MetricsHandler
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import AsyncEngine

NAMESPACE = "docintel"

_FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_DB = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
_JOBS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
_OCR = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 120.0)
_MODEL = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 60.0, 120.0)
_SHARE = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0)

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests by route template, method and status code.",
    ["method", "route", "status"],
    namespace=NAMESPACE,
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "Time to the end of the response body, by route template and method.",
    ["method", "route"],
    namespace=NAMESPACE,
    buckets=_FAST,
)
DB_STATEMENTS = Histogram(
    "db_statement_duration_seconds",
    "Database statement execution time, by SQL verb.",
    ["operation"],
    namespace=NAMESPACE,
    buckets=_DB,
)
JOBS = Counter(
    "worker_jobs_total",
    "Job attempts by outcome: completed, retried, failed (for good), skipped, lease_lost, error.",
    ["job_type", "outcome"],
    namespace=NAMESPACE,
)
JOB_DURATION = Histogram(
    "worker_job_duration_seconds",
    "Time from claim to the end of a job attempt, by job type and outcome.",
    ["job_type", "outcome"],
    namespace=NAMESPACE,
    buckets=_JOBS,
)
STAGE_DURATION = Histogram(
    "pipeline_stage_duration_seconds",
    "Duration of each document-processing stage.",
    ["stage"],
    namespace=NAMESPACE,
    buckets=_FAST,
)
OCR_PAGES = Histogram(
    "ocr_page_duration_seconds",
    "Tesseract time per page image, by outcome.",
    ["outcome"],
    namespace=NAMESPACE,
    buckets=_OCR,
)
MODEL_CALLS = Counter(
    "llm_calls_total",
    "Model calls by provider, purpose and status (failed calls count too).",
    ["provider", "purpose", "status"],
    namespace=NAMESPACE,
)
MODEL_TOKENS = Counter(
    "llm_tokens_total",
    "Tokens reported by the provider: input, output, thinking.",
    ["provider", "kind"],
    namespace=NAMESPACE,
)
MODEL_COST = Counter(
    "llm_estimated_cost_usd_total",
    "Estimated cost at the configured list prices (0 for local models).",
    ["provider"],
    namespace=NAMESPACE,
)
MODEL_LATENCY = Histogram(
    "llm_call_duration_seconds",
    "Model call latency by provider and purpose.",
    ["provider", "purpose"],
    namespace=NAMESPACE,
    buckets=_MODEL,
)
AGENT_RUNS = Histogram(
    "agent_run_duration_seconds",
    "Investigation run time from creation to the end, by final status.",
    ["status"],
    namespace=NAMESPACE,
    buckets=_JOBS,
)
TOOL_CALLS = Counter(
    "agent_tool_calls_total",
    "Agent and MCP tool calls by tool, channel and status (FAILED, DENIED, INVALID: failures).",
    ["tool", "channel", "status"],
    namespace=NAMESPACE,
)
RATE_LIMITED = Counter(
    "rate_limited_requests_total",
    "Requests refused with 429 by the per-caller limits, by scope.",
    ["scope"],
    namespace=NAMESPACE,
)
EXTRACTION_CONFIDENCE = Histogram(
    "extraction_confidence",
    "Overall confidence of each stored extraction, by review routing.",
    ["review_level"],
    namespace=NAMESPACE,
    buckets=_SHARE,
)

# Purposes are dotted names ("agent.plan", "extraction.invoice"); their first part becomes the
# label, from a closed set.
KNOWN_PURPOSES = frozenset({"classification", "extraction", "rag", "agent", "diagnostics"})


def purpose_label(purpose: str) -> str:
    head = purpose.split(".", 1)[0].strip().lower()
    return head if head in KNOWN_PURPOSES else "other"


def observe_model_call(
    *,
    provider: str,
    purpose: str,
    status: str,
    latency_ms: float,
    tokens: Mapping[str, int | None],
    cost_usd: float | None,
) -> None:
    label = purpose_label(purpose)
    MODEL_CALLS.labels(provider, label, status).inc()
    MODEL_LATENCY.labels(provider, label).observe(latency_ms / 1000)
    for kind, count in tokens.items():
        if count:
            MODEL_TOKENS.labels(provider, kind).inc(count)
    if cost_usd:
        MODEL_COST.labels(provider).inc(cost_usd)


def observe_stages(timings_ms: Mapping[str, Any]) -> None:
    for stage, value in timings_ms.items():
        if isinstance(value, int | float):
            STAGE_DURATION.labels(stage).observe(value / 1000)


_VERBS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"})


def _operation(statement: str) -> str:
    verb = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
    return verb.lower() if verb in _VERBS else "other"


_INSTRUMENTED: weakref.WeakSet[Engine] = weakref.WeakSet()


def instrument_engine(engine: AsyncEngine) -> None:
    """Time every statement of an engine (idempotent per engine)."""
    sync_engine = engine.sync_engine
    if sync_engine in _INSTRUMENTED:
        return
    _INSTRUMENTED.add(sync_engine)

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _before(conn: Connection, *_: Any) -> None:
        conn.info.setdefault("docintel_statement_started", []).append(time.perf_counter())

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _after(conn: Connection, _cursor: Any, statement: str, *_: Any) -> None:
        started = conn.info.get("docintel_statement_started")
        if started:
            DB_STATEMENTS.labels(_operation(statement)).observe(time.perf_counter() - started.pop())

    @event.listens_for(sync_engine, "handle_error")
    def _failed(context: Any) -> None:
        connection = context.connection
        started = connection.info.get("docintel_statement_started") if connection else None
        if started:
            DB_STATEMENTS.labels("error").observe(time.perf_counter() - started.pop())


def bearer_matches(authorization: str | None, token: str) -> bool:
    """True if an Authorization header carries `Bearer <token>` (constant-time comparison)."""
    scheme, _, credentials = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(
        credentials.strip().encode(), token.encode()
    )


def serve_metrics(
    port: int,
    *,
    token: str | None,
    host: str = "0.0.0.0",  # noqa: S104 (inside the container; compose does not publish it)
) -> ThreadingHTTPServer:
    """Serve this process's metrics over HTTP from a daemon thread (the worker's endpoint).

    With a token, scrapes without `Authorization: Bearer <token>` get 401. Returns the server;
    `shutdown()` stops it.
    """

    class Handler(MetricsHandler):
        registry = REGISTRY

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler naming)
            if token is not None and not bearer_matches(self.headers.get("Authorization"), token):
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            super().do_GET()

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
    return server
