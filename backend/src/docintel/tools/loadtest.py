"""Load test of a running stack through its public entry point (Phase 11, ADR-080).

`docintel loadtest` measures what NFR-09 asks of a deployment, over HTTP like a browser:

* reads: N virtual users, each in a loop over the pages people open most (inbox, a document,
  its extraction and findings, the review queue, the dashboard) for a fixed time, with a
  warm-up that is not recorded. Latency is measured at the client, so it includes the proxy.
* processing (optional, `--dataset`): a directory of documents is uploaded through the API while
  the readers run, and each document's processing time is read back from its job. Native
  documents are reported per document, scanned ones per page (the NFR-09 units).

Everything shares one machine unless the stack runs elsewhere; the report records the client
side (CPU count, users, duration) and the caller adds the stack's shape (`--label replicas=2`).
"""

from __future__ import annotations

import asyncio
import os
import platform
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from docintel.tools.http import RetryAfterTransport
from docintel.tools.ingest import IngestItem, ingest_directory

READ_ENDPOINTS = ("inbox", "document", "extraction", "findings", "review_queue", "dashboard")
TARGETS_MS = {"read_p95": 300.0}
TARGETS_S = {"native_document_p95": 1.0, "scanned_page_p95": 5.0}


class LoadTestError(Exception):
    pass


def percentile(values: Sequence[float], share: float) -> float:
    """Nearest-rank percentile (0 for no values)."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return ordered[index]


@dataclass
class Samples:
    latencies_ms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    statuses: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)


def _summary(values: Sequence[float]) -> dict[str, Any]:
    return {
        "requests": len(values),
        "p50_ms": round(percentile(values, 0.50), 1),
        "p95_ms": round(percentile(values, 0.95), 1),
        "p99_ms": round(percentile(values, 0.99), 1),
        "max_ms": round(max(values), 1) if values else 0.0,
    }


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if response.status_code != httpx.codes.OK:
        msg = f"login as {email} failed ({response.status_code})"
        raise LoadTestError(msg)
    token: str = response.json()["access_token"]
    return token


async def _paths(client: httpx.AsyncClient) -> dict[str, str]:
    inbox = await client.get("/api/v1/documents", params={"limit": 25})
    inbox.raise_for_status()
    documents = inbox.json()["items"]
    processed = [d for d in documents if d["status"] in ("COMPLETED", "REVIEW_REQUIRED")]
    if not processed:
        msg = "no processed document to read; ingest a dataset first (make process)"
        raise LoadTestError(msg)
    document_id = processed[0]["id"]
    return {
        "inbox": "/api/v1/documents?limit=25",
        "document": f"/api/v1/documents/{document_id}",
        "extraction": f"/api/v1/documents/{document_id}/extraction",
        "findings": f"/api/v1/documents/{document_id}/findings",
        "review_queue": "/api/v1/review-tasks",
        "dashboard": "/api/v1/dashboard/summary?days=30",
    }


async def _reader(
    client: httpx.AsyncClient,
    paths: dict[str, str],
    samples: Samples,
    *,
    offset: int,
    record_after: float,
    stop_at: float,
) -> None:
    index = offset
    while (now := time.monotonic()) < stop_at:
        name = READ_ENDPOINTS[index % len(READ_ENDPOINTS)]
        index += 1
        started = time.perf_counter()
        try:
            response = await client.get(paths[name])
        except httpx.HTTPError as exc:
            if now >= record_after:
                samples.errors[type(exc).__name__] += 1
            continue
        elapsed_ms = (time.perf_counter() - started) * 1000
        if now < record_after:
            continue  # warm-up
        samples.statuses[str(response.status_code)] += 1
        if response.status_code == httpx.codes.OK:
            samples.latencies_ms[name].append(elapsed_ms)


def _processing(items: list[IngestItem]) -> dict[str, Any]:
    native = [
        item.processing_ms / 1000
        for item in items
        if item.processing_ms is not None and item.inspection_kind == "native_pdf"
    ]
    scanned_pages = [
        item.processing_ms / 1000 / max(item.page_count or 1, 1)
        for item in items
        if item.processing_ms is not None and item.inspection_kind != "native_pdf"
    ]
    failed = [item.file for item in items if item.status == "FAILED"]
    unfinished = [item.file for item in items if item.status in ("PENDING", "PROCESSING")]
    return {
        "documents": len(items),
        "native_documents": len(native),
        "scanned_documents": len(scanned_pages),
        "native_document_p50_s": round(percentile(native, 0.50), 3),
        "native_document_p95_s": round(percentile(native, 0.95), 3),
        "scanned_page_p50_s": round(percentile(scanned_pages, 0.50), 3),
        "scanned_page_p95_s": round(percentile(scanned_pages, 0.95), 3),
        "failed": len(failed),
        "failed_files": failed,
        "unfinished": len(unfinished),
    }


async def run_load_test(
    *,
    api_url: str,
    email: str,
    password: str,
    users: int,
    duration_seconds: float,
    warmup_seconds: float = 5.0,
    dataset: Path | None = None,
    labels: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    limits = httpx.Limits(max_connections=users + 4, max_keepalive_connections=users + 4)
    async with httpx.AsyncClient(
        base_url=api_url, timeout=60.0, limits=limits, transport=RetryAfterTransport(transport)
    ) as client:
        client.headers["Authorization"] = f"Bearer {await _login(client, email, password)}"
        paths = await _paths(client)
        samples = Samples()
        started = time.monotonic()
        record_after = started + warmup_seconds
        stop_at = record_after + duration_seconds

        ingest: asyncio.Task[list[IngestItem]] | None = None
        if dataset is not None:
            ingest = asyncio.create_task(
                ingest_directory(
                    dataset,
                    api_url=api_url,
                    email=email,
                    password=password,
                    timeout_seconds=max(600.0, duration_seconds * 4),
                    transport=transport,
                )
            )
        await asyncio.gather(
            *(
                _reader(
                    client,
                    paths,
                    samples,
                    offset=user,
                    record_after=record_after,
                    stop_at=stop_at,
                )
                for user in range(users)
            )
        )
        items = await ingest if ingest is not None else None

    every = [value for values in samples.latencies_ms.values() for value in values]
    reads = {name: _summary(samples.latencies_ms[name]) for name in READ_ENDPOINTS}
    reads["all"] = _summary(every)
    report: dict[str, Any] = {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "api_url": api_url,
        "labels": dict(labels or {}),
        "client": {"cpus": os.cpu_count(), "platform": platform.platform(terse=True)},
        "users": users,
        "duration_s": duration_seconds,
        "warmup_s": warmup_seconds,
        "reads": reads,
        "throughput_rps": round(len(every) / duration_seconds, 1),
        "statuses": dict(sorted(samples.statuses.items())),
        "transport_errors": dict(samples.errors),
        "processing": _processing(items) if items is not None else None,
    }
    report["targets"] = _targets(report)
    return report


def _targets(report: dict[str, Any]) -> dict[str, Any]:
    reads = report["reads"]["all"]
    result: dict[str, Any] = {
        "read_p95_ms": {
            "target": TARGETS_MS["read_p95"],
            "measured": reads["p95_ms"],
            "met": reads["requests"] > 0 and reads["p95_ms"] <= TARGETS_MS["read_p95"],
        }
    }
    processing = report["processing"]
    if processing:
        for key, target in TARGETS_S.items():
            count = processing[
                "native_documents" if key.startswith("native") else "scanned_documents"
            ]
            measured = processing[key.replace("_p95", "_p95_s")]
            result[f"{key}_s"] = {
                "target": target,
                "measured": measured if count else None,
                "met": bool(count) and measured <= target,
            }
        result["no_failed_jobs"] = {"met": processing["failed"] == 0}
    return result


def render_markdown(report: dict[str, Any]) -> str:
    labels = ", ".join(f"{key}={value}" for key, value in report["labels"].items()) or "none"
    lines = [
        f"# Load test {report['run_at']}",
        "",
        f"Target {report['api_url']} ({labels}); {report['users']} virtual users for "
        f"{report['duration_s']:g} s after a {report['warmup_s']:g} s warm-up; load generator on "
        f"{report['client']['cpus']} CPUs ({report['client']['platform']}).",
        "",
        "| Endpoint | Requests | p50 ms | p95 ms | p99 ms | max ms |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, row in report["reads"].items():
        lines.append(
            f"| {name} | {row['requests']} | {row['p50_ms']} | {row['p95_ms']} | "
            f"{row['p99_ms']} | {row['max_ms']} |"
        )
    lines += [
        "",
        f"Throughput {report['throughput_rps']} successful reads/s; status codes "
        f"{report['statuses']}; transport errors {report['transport_errors'] or 'none'}.",
    ]
    if report["processing"]:
        p = report["processing"]
        lines += [
            "",
            f"Processing during the run: {p['documents']} documents uploaded "
            f"({p['native_documents']} native, {p['scanned_documents']} scanned); native p50/p95 "
            f"{p['native_document_p50_s']}/{p['native_document_p95_s']} s per document, scanned "
            f"p50/p95 {p['scanned_page_p50_s']}/{p['scanned_page_p95_s']} s per page; "
            f"{p['failed']} failed, {p['unfinished']} unfinished.",
        ]
    lines += ["", "| NFR-09 target | Target | Measured | Met |", "|---|---:|---:|---|"]
    for name, check in report["targets"].items():
        measured = check.get("measured")
        lines.append(
            f"| {name} | {check.get('target', '-')} | "
            f"{'not measured' if measured is None and 'target' in check else measured or '-'} | "
            f"{'yes' if check['met'] else 'no'} |"
        )
    return "\n".join(lines) + "\n"
