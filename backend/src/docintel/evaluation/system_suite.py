"""System performance: latency, throughput and failure rate (Module 25; NFR-09 baseline).

The synthetic dataset - native PDFs, the generator's own scans, and native PDFs re-rendered as
light scans - is uploaded through the production upload service into a scratch database and
processed by the production worker (`Worker.run`, LISTEN/NOTIFY and claim loops) with 1 and with
4 claim loops in one process. Everything is measured from the jobs' own timestamps and stage
timings:

* throughput: documents (and pages) per minute from the first claim to the last finish, with
  the whole dataset queued at once (the worker is never idle);
* processing time per document (claim to finish) and per pipeline stage, p50 / p95, native and
  scanned documents apart;
* failure and retry rates;
* API latency in process (the ASGI app and the database; no network, nginx or TLS): p50 / p95 of
  the read endpoints people use most, one request at a time, and the inbox under 8 concurrent
  clients.

Deterministic mode: no LLM is configured, so model latency and cost are not in these numbers.
The machine decides them; the report records it.
"""

from __future__ import annotations

import asyncio
import io
import os
import platform
import secrets
import tempfile
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import UploadFile
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.datastructures import Headers

from docintel.ai.local_embeddings import HashingEmbeddingProvider
from docintel.api.app import create_app
from docintel.audit.service import SYSTEM_REQUEST
from docintel.auth.tokens import create_access_token
from docintel.core.config import Settings
from docintel.db.models import (
    Department,
    Document,
    DocumentStatus,
    DocumentType,
    JobStatus,
    JobType,
    LLMCall,
    ProcessingJob,
    Role,
    Sensitivity,
    User,
)
from docintel.documents.service import DocumentService
from docintel.evaluation.common import scan_pdf
from docintel.evaluation.report import Report, environment
from docintel.evaluation.retrieval_suite import scratch_database
from docintel.knowledge.embedding import ChunkEmbedder
from docintel.processing.ocr import TesseractOCRProvider
from docintel.processing.services import build_processing_services
from docintel.storage import LocalStorage
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.scenarios import Scenario
from docintel.vendors.seed import seed_demo_vendors
from docintel.workers.runner import Worker

SEED = 23
CONCURRENCY = (1, 4)
QUICK_SCENARIOS = [Scenario.CLEAN_MATCH, Scenario.SCANNED_DOCUMENTS]
RESCANS = {False: 20, True: 2}  # native PDFs also uploaded as light scans
API_REPEATS = {False: 30, True: 5}
API_CLIENTS = 8
API_REQUESTS_PER_CLIENT = {False: 10, True: 3}
PROCESSING_TIMEOUT_SECONDS = 1800.0
STAGES = ("integrity", "inspect", "extract", "previews", "classify", "fields", "index")


@dataclass(frozen=True, slots=True)
class Item:
    path: Path
    mime_type: str
    variant: str  # "native" or "scanned"


@dataclass(slots=True)
class PipelineRun:
    concurrency: int
    documents: int
    pages: int
    wall_seconds: float
    completed: int
    failed: int
    retried: int
    review_required: int
    job_seconds: dict[str, list[float]]  # by variant, and "all"
    stage_ms: dict[str, dict[str, list[float]]]  # stage -> variant -> values
    upload_ms: list[float]
    llm_calls: int
    api: dict[str, Any] = field(default_factory=dict)


def percentile(values: Sequence[float], share: float) -> float | None:
    """Nearest-rank percentile (no interpolation between samples)."""
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return round(ordered[index], 2)


def spread(values: Sequence[float]) -> dict[str, float | int | None]:
    return {"n": len(values), "p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}


def build_dataset(root: Path, *, quick: bool) -> list[Item]:
    manifest = generate_dataset(
        root / "core",
        seed=SEED,
        bundles_per_scenario=1 if quick else 2,
        scenarios=QUICK_SCENARIOS if quick else None,
    )
    items = [
        Item(
            root / "core" / entry["file"],
            entry["mime_type"],
            "scanned" if entry["variant"] == "scanned" else "native",
        )
        for entry in manifest["documents"]
    ]
    natives = [item for item in items if item.variant == "native"]
    (root / "rescans").mkdir()
    for index, item in enumerate(natives[: RESCANS[quick]]):
        target = root / "rescans" / f"{item.path.stem}-scan.pdf"
        target.write_bytes(scan_pdf(item.path.read_bytes(), profile="light", dpi=150, seed=index))
        items.append(Item(target, "application/pdf", "scanned"))
    return items


def _settings(database_url: str, storage_root: Path, concurrency: int) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": database_url,
        "jwt_secret_key": secrets.token_urlsafe(48),
        "storage_local_root": storage_root,
        "embedding_provider": "hashing",
        "gemini_api_key": None,
        "seed_user_password": None,
        "log_level": "WARNING",
        "worker_concurrency": concurrency,
    }
    return Settings(_env_file=None, **values)


async def _analyst(maker: async_sessionmaker[AsyncSession]) -> User:
    async with maker() as session:
        await seed_demo_vendors(session)
        department = Department(id=uuid.uuid4(), name="Finance")
        user = User(
            id=uuid.uuid4(),
            email="analyst@eval.invalid",
            full_name="Evaluation analyst",
            password_hash="not-used",  # noqa: S106  (no login in the evaluation)
            role=Role.ANALYST,
            department_id=department.id,
        )
        session.add_all([department, user])
        await session.commit()
    return user


async def _upload(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: LocalStorage,
    user: User,
    items: list[Item],
) -> tuple[dict[uuid.UUID, str], list[float]]:
    variants: dict[uuid.UUID, str] = {}
    timings: list[float] = []
    for item in items:
        upload = UploadFile(
            io.BytesIO(item.path.read_bytes()),
            filename=item.path.name,
            headers=Headers({"content-type": item.mime_type}),
        )
        started = time.perf_counter()
        async with maker() as session:
            document = await DocumentService(session, storage, settings).upload(
                actor=user,
                upload=upload,
                sensitivity=Sensitivity.INTERNAL,
                department_id=None,
                meta=SYSTEM_REQUEST,
            )
        timings.append((time.perf_counter() - started) * 1000)
        variants[document.id] = item.variant
    return variants, timings


async def _drain(maker: async_sessionmaker[AsyncSession], worker: Worker) -> None:
    """Run the worker until no document job is queued or running."""
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    deadline = time.monotonic() + PROCESSING_TIMEOUT_SECONDS
    try:
        while True:
            await asyncio.sleep(0.25)
            async with maker() as session:
                pending = await session.scalar(
                    select(func.count())
                    .select_from(ProcessingJob)
                    .where(
                        ProcessingJob.job_type == JobType.DOCUMENT_PROCESSING,
                        ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING]),
                    )
                )
            if not pending:
                return
            if time.monotonic() > deadline:
                msg = f"documents still processing after {PROCESSING_TIMEOUT_SECONDS:.0f} s"
                raise TimeoutError(msg)
    finally:
        stop.set()
        await task


async def _measure_api(
    settings: Settings, user: User, maker: async_sessionmaker[AsyncSession], *, quick: bool
) -> dict[str, Any]:
    async with maker() as session:
        invoice_id = await session.scalar(
            select(Document.id)
            .where(Document.document_type == DocumentType.INVOICE)
            .order_by(Document.created_at)
            .limit(1)
        )
    endpoints: list[tuple[str, str, str, dict[str, Any] | None]] = [
        ("inbox", "GET", "/api/v1/documents?limit=25", None),
        ("document", "GET", f"/api/v1/documents/{invoice_id}", None),
        ("extraction", "GET", f"/api/v1/documents/{invoice_id}/extraction", None),
        ("findings", "GET", f"/api/v1/documents/{invoice_id}/findings", None),
        ("review_queue", "GET", "/api/v1/review-tasks", None),
        ("dashboard", "GET", "/api/v1/dashboard/summary?days=30", None),
        ("search", "POST", "/api/v1/search", {"query": "invoices over 1,000"}),
    ]
    token = create_access_token(user_id=user.id, role=user.role, settings=settings).token
    application = create_app(settings)
    results: dict[str, Any] = {}
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://evaluation",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:

            async def call(method: str, path: str, body: dict[str, Any] | None) -> float:
                started = time.perf_counter()
                response = await client.request(method, path, json=body)
                elapsed = (time.perf_counter() - started) * 1000
                if response.status_code != httpx.codes.OK:
                    msg = f"{method} {path}: HTTP {response.status_code}"
                    raise RuntimeError(msg)
                return elapsed

            for name, method, path, body in endpoints:
                for _ in range(2):  # warm-up: first-use imports and query plans
                    await call(method, path, body)
                samples = [await call(method, path, body) for _ in range(API_REPEATS[quick])]
                results[name] = spread(samples)

            per_client = API_REQUESTS_PER_CLIENT[quick]

            async def client_run() -> list[float]:
                return [
                    await call("GET", "/api/v1/documents?limit=25", None) for _ in range(per_client)
                ]

            started = time.perf_counter()
            batches = await asyncio.gather(*(client_run() for _ in range(API_CLIENTS)))
            wall = time.perf_counter() - started
            samples = [value for batch in batches for value in batch]
            results["inbox_concurrent"] = {
                **spread(samples),
                "clients": API_CLIENTS,
                "requests_per_second": round(len(samples) / wall, 1),
            }
    return results


async def _run_pipeline(
    database_url: str, items: list[Item], concurrency: int, *, quick: bool, with_api: bool
) -> PipelineRun:
    with tempfile.TemporaryDirectory(prefix="docintel-system-eval-") as tmp:
        async with scratch_database(database_url) as url:
            engine = create_async_engine(url, pool_size=concurrency + 4)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            try:
                settings = _settings(url, Path(tmp) / "storage", concurrency)
                storage = LocalStorage(Path(tmp) / "storage")
                user = await _analyst(maker)
                variants, upload_ms = await _upload(maker, settings, storage, user, items)
                embedder = ChunkEmbedder(
                    HashingEmbeddingProvider(), max_sensitivity=Sensitivity.INTERNAL
                )
                services = build_processing_services(
                    settings, sessionmaker=maker, embedder=embedder
                )
                worker = Worker(
                    settings=settings, sessionmaker=maker, storage=storage, services=services
                )
                await _drain(maker, worker)
                run = await _collect(maker, variants, concurrency, upload_ms)
                if with_api:
                    run.api = await _measure_api(settings, user, maker, quick=quick)
            finally:
                await engine.dispose()
    return run


async def _collect(
    maker: async_sessionmaker[AsyncSession],
    variants: dict[uuid.UUID, str],
    concurrency: int,
    upload_ms: list[float],
) -> PipelineRun:
    async with maker() as session:
        jobs = list(
            await session.scalars(
                select(ProcessingJob).where(ProcessingJob.job_type == JobType.DOCUMENT_PROCESSING)
            )
        )
        documents = {document.id: document for document in await session.scalars(select(Document))}
        pages = int(
            await session.scalar(
                text(
                    "SELECT coalesce(sum(v.page_count), 0) FROM documents d "
                    "JOIN document_versions v ON v.id = d.current_version_id"
                )
            )
            or 0
        )
        llm_calls = int(await session.scalar(select(func.count()).select_from(LLMCall)) or 0)
    started = [job.started_at for job in jobs if job.started_at]
    finished = [job.finished_at for job in jobs if job.finished_at]
    wall = (max(finished) - min(started)).total_seconds() if started and finished else 0.0
    job_seconds: dict[str, list[float]] = defaultdict(list)
    stage_ms: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for job in jobs:
        if job.status != JobStatus.COMPLETED or not job.started_at or not job.finished_at:
            continue
        variant = variants.get(job.document_id or uuid.uuid4(), "native")
        seconds = (job.finished_at - job.started_at).total_seconds()
        job_seconds[variant].append(seconds)
        job_seconds["all"].append(seconds)
        for stage, value in (job.stage_timings or {}).items():
            if isinstance(value, int | float):
                stage_ms[stage][variant].append(float(value))
                stage_ms[stage]["all"].append(float(value))
    return PipelineRun(
        concurrency=concurrency,
        documents=len(jobs),
        pages=pages,
        wall_seconds=round(wall, 2),
        completed=sum(job.status == JobStatus.COMPLETED for job in jobs),
        failed=sum(job.status == JobStatus.FAILED for job in jobs),
        retried=sum(job.attempts > 1 for job in jobs),
        review_required=sum(
            document.status == DocumentStatus.REVIEW_REQUIRED for document in documents.values()
        ),
        job_seconds=dict(job_seconds),
        stage_ms={stage: dict(values) for stage, values in stage_ms.items()},
        upload_ms=upload_ms,
        llm_calls=llm_calls,
    )


def _per_minute(count: int, seconds: float) -> float | None:
    return round(count / seconds * 60, 1) if seconds > 0 else None


def summarize(run: PipelineRun) -> dict[str, Any]:
    return {
        "worker_claim_loops": run.concurrency,
        "documents": run.documents,
        "pages": run.pages,
        "wall_seconds": run.wall_seconds,
        "documents_per_minute": _per_minute(run.completed, run.wall_seconds),
        "pages_per_minute": _per_minute(run.pages, run.wall_seconds),
        "completed": run.completed,
        "failed": run.failed,
        "failure_rate": round(run.failed / run.documents, 4) if run.documents else None,
        "retried": run.retried,
        "review_required": run.review_required,
        "processing_seconds": {
            variant: spread(values) for variant, values in sorted(run.job_seconds.items())
        },
        "stage_ms": {
            stage: {variant: spread(values) for variant, values in sorted(by_variant.items())}
            for stage, by_variant in sorted(run.stage_ms.items())
        },
        "upload_ms": spread(run.upload_ms),
        "llm_calls": run.llm_calls,
    }


def _fmt(value: float | int | None, unit: str = "", digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}{unit}" if isinstance(value, float) else f"{value:,}{unit}"


def _pair(values: dict[str, Any], unit: str) -> str:
    """ "p50 / p95" of a spread, or n/a."""
    return f"{_fmt(values.get('p50'), unit)} / {_fmt(values.get('p95'), unit)}"


def _pipeline_rows(metrics: dict[str, Any]) -> list[list[str]]:
    rows = []
    for key in sorted(metrics["pipeline"], key=lambda name: int(name.split("_")[0])):
        run = metrics["pipeline"][key]
        processing = run["processing_seconds"]
        rows.append(
            [
                str(run["worker_claim_loops"]),
                f"{run['documents']} ({run['pages']} pages)",
                _fmt(run["wall_seconds"], " s"),
                _fmt(run["documents_per_minute"], digits=1),
                _fmt(run["pages_per_minute"], digits=1),
                _pair(processing.get("native", {}), " s"),
                _pair(processing.get("scanned", {}), " s"),
                f"{run['failed']} ({_fmt((run['failure_rate'] or 0) * 100, digits=1)}%)",
                str(run["retried"]),
            ]
        )
    return rows


def _stage_rows(run: dict[str, Any]) -> list[list[str]]:
    rows = []
    for stage in [*STAGES, *sorted(set(run["stage_ms"]) - set(STAGES))]:
        values = run["stage_ms"].get(stage)
        if not values:
            continue
        rows.append(
            [
                stage,
                *(
                    _fmt(values.get(variant, {}).get(share), " ms")
                    for variant in ("native", "scanned")
                    for share in ("p50", "p95")
                ),
            ]
        )
    return rows


API_LABELS = {
    "inbox": "Inbox, 25 documents (GET /documents)",
    "document": "Document detail",
    "extraction": "Extracted fields with evidence",
    "findings": "Checks (findings)",
    "review_queue": "Review queue",
    "dashboard": "Dashboard summary, 30 days",
    "search": "Document search (structured + text)",
    "inbox_concurrent": f"Inbox, {API_CLIENTS} concurrent clients",
}


def _api_rows(api: dict[str, Any]) -> list[list[str]]:
    rows = []
    for name, label in API_LABELS.items():
        values = api.get(name)
        if not values:
            continue
        rate = values.get("requests_per_second")
        rows.append(
            [
                label,
                str(values["n"]),
                _fmt(values["p50"], " ms"),
                _fmt(values["p95"], " ms"),
                _fmt(rate, " req/s", digits=1) if rate is not None else "",
            ]
        )
    return rows


async def run_system_suite(output: Path, *, database_url: str, quick: bool = False) -> Report:
    started = time.perf_counter()
    async with asyncio.timeout(PROCESSING_TIMEOUT_SECONDS * 3):
        with tempfile.TemporaryDirectory(prefix="docintel-system-data-") as data:
            items = build_dataset(Path(data), quick=quick)
            runs = [
                await _run_pipeline(
                    database_url, items, concurrency, quick=quick, with_api=concurrency == 1
                )
                for concurrency in CONCURRENCY
            ]
    pipeline = {f"{run.concurrency}_claim_loops": summarize(run) for run in runs}
    api = runs[0].api
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            postgres = str(await connection.scalar(text("SHOW server_version")))
    finally:
        await engine.dispose()
    tesseract = await TesseractOCRProvider().version()
    variants: dict[str, int] = defaultdict(int)
    for item in items:
        variants[item.variant] += 1
    metrics = {"pipeline": pipeline, "api_ms": api}
    first = pipeline[f"{CONCURRENCY[0]}_claim_loops"]
    report = Report(
        quick=quick,
        suite="system",
        title="System performance (latency, throughput, failure rate)",
        dataset={
            "name": "synthetic-core + light re-scans",
            "seed": SEED,
            "bundles_per_scenario": 1 if quick else 2,
            "scenarios": [s.value for s in QUICK_SCENARIOS] if quick else "all",
            "documents": len(items),
            "native": variants["native"],
            "scanned": variants["scanned"],
        },
        config={
            "mode": "deterministic (no LLM)",
            "worker": "one process, Worker.run with N claim loops (WORKER_CONCURRENCY)",
            "claim_loops": list(CONCURRENCY),
            "ocr_concurrency": Settings.model_fields["ocr_concurrency"].default,
            "api": "in process: httpx ASGITransport, no network, nginx or TLS",
            "api_repeats": API_REPEATS[quick],
            "seconds": round(time.perf_counter() - started, 1),
        },
        metrics=metrics,
        environment=environment(
            {
                "tesseract": tesseract,
                "postgresql": postgres,
                "cpus": str(os.cpu_count()),
                "machine": platform.machine(),
                "platform": platform.platform(terse=True),
            }
        ),
        notes=[
            "Throughput is measured with the whole dataset queued at once, from the first claim "
            "to the last finish: the worker is never idle. Processing time is claim to finish "
            "of each document's job and includes matching, rules and the review queue.",
            "Scanned documents are OCR'd page by page with Tesseract; native PDFs use their text "
            "layer. More claim loops in one process overlap I/O and Tesseract subprocesses; "
            "PDF rendering is serialized per process, so CPU-bound work scales with more worker "
            "processes, which this suite does not run.",
            "API numbers are in process (the application and its database queries); a deployment "
            "adds the network, nginx and TLS. Authentication is a bearer token; the dashboard "
            "and search read the processed dataset.",
            f"No LLM is configured: {first['llm_calls']} model calls. Model latency and cost per "
            "document are not measured here.",
            "These numbers describe this machine (see Provenance) and this dataset; they are a "
            "baseline for regressions, not a capacity promise.",
        ],
        tables=[
            (
                "Document pipeline",
                [
                    "Claim loops",
                    "Documents",
                    "Wall time",
                    "Documents / min",
                    "Pages / min",
                    "Native p50 / p95",
                    "Scanned p50 / p95",
                    "Failed",
                    "Retried",
                ],
                _pipeline_rows(metrics),
            ),
            (
                f"Pipeline stages ({CONCURRENCY[0]} claim loop)",
                ["Stage", "Native p50", "Native p95", "Scanned p50", "Scanned p95"],
                _stage_rows(first),
            ),
            (
                "API latency (in process)",
                ["Request", "Samples", "p50", "p95", "Throughput"],
                _api_rows(api),
            ),
            (
                "Upload (validation, storage, database; in process)",
                ["Samples", "p50", "p95"],
                [
                    [
                        str(first["upload_ms"]["n"]),
                        _fmt(first["upload_ms"]["p50"], " ms"),
                        _fmt(first["upload_ms"]["p95"], " ms"),
                    ]
                ],
            ),
        ],
    )
    report.write(output)
    return report
