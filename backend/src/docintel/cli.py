"""Management CLI.

docintel seed                      demo departments + one user per role (not in staging/prod)
docintel create-user --email ...   create a user (password prompted or read from stdin)
docintel check-ai                  verify the configured AI provider end-to-end
docintel check-ocr                 verify the OCR engine and languages with a sample image
docintel worker [--until-idle]     run the background job worker
docintel worker-health             exit 0 if the worker heartbeat is fresh (container probe)
docintel generate-documents        synthetic POs/invoices/delivery notes with ground truth
docintel ingest DIR                upload a directory through the REST API and wait for results
docintel evaluate --suite ...      OCR / classification / table / extraction / discrepancy /
                                   version-comparison metrics
docintel match                     re-run matching for every processed document
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import math
import os
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select

from docintel.ai.base import EmbeddingTask, LLMRequest, ModelTier
from docintel.ai.errors import ProviderError
from docintel.ai.gemini import GeminiLLMProvider
from docintel.ai.ollama import OllamaLLMProvider
from docintel.ai.registry import build_embedding_provider, build_llm_provider
from docintel.audit.service import SYSTEM_REQUEST
from docintel.auth.passwords import PasswordPolicyError
from docintel.auth.seed import seed_demo_identities
from docintel.auth.service import UserService
from docintel.core.config import LLMProviderName, LogFormat, Settings, get_settings
from docintel.core.errors import ConflictError
from docintel.core.logging import configure_logging, get_logger
from docintel.db.models import (
    OPEN_TASK_STATUSES,
    Document,
    LLMCall,
    LLMCallStatus,
    ReviewTask,
    Role,
)
from docintel.db.session import create_engine, create_sessionmaker
from docintel.matching.service import PROCESSED, rematch
from docintel.processing.ocr import OCRUnavailableError, TesseractOCRProvider
from docintel.processing.services import build_processing_services, load_corrections
from docintel.storage import build_storage
from docintel.vendors.seed import seed_demo_vendors
from docintel.workers.runner import Worker, heartbeat_age_seconds

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


def _ok(message: str) -> None:
    print(f"[ OK ] {message}")


def _fail(message: str) -> None:
    print(f"[FAIL] {message}")


# ---------------------------------------------------------------------------- seed
async def _seed(settings: Settings) -> int:
    if settings.is_deployed:
        _fail(f"Refusing to seed demo users in APP_ENV={settings.app_env.value}.")
        return EXIT_USAGE
    if settings.seed_user_password is None:
        _fail("SEED_USER_PASSWORD is not set (add it to .env; minimum 12 characters).")
        return EXIT_USAGE
    engine = create_engine(settings)
    try:
        async with create_sessionmaker(engine)() as session:
            report = await seed_demo_identities(
                session, password=settings.seed_user_password.get_secret_value()
            )
            vendors_created, _ = await seed_demo_vendors(session)
    except PasswordPolicyError as exc:
        _fail(f"SEED_USER_PASSWORD rejected: {exc}")
        return EXIT_USAGE
    finally:
        await engine.dispose()
    for email in report.created_users:
        _ok(f"created {email}")
    for email in report.existing_users:
        print(f"[SKIP] {email} already exists")
    for name in vendors_created:
        _ok(f"created vendor {name}")
    print("Demo users share the password in SEED_USER_PASSWORD.")
    return EXIT_OK


# ---------------------------------------------------------------------------- create-user
def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        msg = "Passwords do not match."
        raise PasswordPolicyError(msg)
    return first


async def _create_user(settings: Settings, args: argparse.Namespace) -> int:
    try:
        password = _read_password(args.password_stdin)
    except PasswordPolicyError as exc:
        _fail(str(exc))
        return EXIT_USAGE
    engine = create_engine(settings)
    try:
        async with create_sessionmaker(engine)() as session:
            service = UserService(session)
            department = (
                await service.get_or_create_department(args.department, meta=SYSTEM_REQUEST)
                if args.department
                else None
            )
            user = await service.create_user(
                email=args.email,
                full_name=args.full_name,
                password=password,
                role=Role(args.role),
                department=department,
                meta=SYSTEM_REQUEST,
            )
            await session.commit()
    except (PasswordPolicyError, ConflictError) as exc:
        _fail(str(exc))
        return EXIT_USAGE
    finally:
        await engine.dispose()
    _ok(f"created {user.email} ({user.role.value})")
    return EXIT_OK


# ---------------------------------------------------------------------------- check-ai
class _ArithmeticProbe(BaseModel):
    status: Literal["ok"]
    total: int


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


async def _check_llm(settings: Settings) -> bool:
    try:
        llm = build_llm_provider(settings)
    except ProviderError as exc:
        _fail(str(exc))
        return False
    healthy = True
    try:
        if isinstance(llm, OllamaLLMProvider):
            pulled = await llm.list_models()
            _ok(f"Ollama reachable at {settings.ollama_base_url}; {len(pulled)} model(s) pulled")
            for tier in ModelTier:
                model = llm.model_for(tier)
                if model in pulled or f"{model}:latest" in pulled:
                    _ok(f"{tier.value} model '{model}' is pulled")
                else:
                    healthy = False
                    _fail(f"{tier.value} model '{model}' is not pulled (run: ollama pull {model})")
        if isinstance(llm, GeminiLLMProvider):
            available = await llm.list_generation_models()
            _ok(f"API key accepted; {len(available)} generation models available")
            for tier in ModelTier:
                model = llm.model_for(tier)
                if model in available:
                    _ok(f"{tier.value} model '{model}' is available")
                else:
                    healthy = False
                    _fail(f"{tier.value} model '{model}' is NOT available to this key")
        for tier in ModelTier:
            request = LLMRequest(
                prompt="Return JSON with status set to 'ok' and total set to 17 + 25.",
                tier=tier,
                purpose="diagnostics.check_ai",
            )
            result = await llm.generate_structured(request, _ArithmeticProbe)
            usage = result.usage
            verdict = (
                "correct" if result.data.total == 42 else f"unexpected total {result.data.total}"
            )
            _ok(
                f"structured output via {usage.model}: {verdict}; "
                f"{usage.latency_ms:.0f} ms, tokens in/out/thinking = "
                f"{usage.input_tokens}/{usage.output_tokens}/{usage.thinking_tokens}"
            )
    except ProviderError as exc:
        _fail(f"{type(exc).__name__}: {exc}")
        healthy = False
    finally:
        await llm.aclose()
    return healthy


async def _check_embeddings(settings: Settings) -> bool:
    try:
        embedder = build_embedding_provider(settings)
    except ProviderError as exc:
        _fail(str(exc))
        return False
    texts = [
        "The invoice total exceeds the purchase order amount.",
        "The billed amount is higher than what the PO authorised.",
        "The office cafeteria opens at eight in the morning.",
    ]
    try:
        result = await embedder.embed(texts, EmbeddingTask.SEMANTIC_SIMILARITY)
    except ProviderError as exc:
        _fail(f"{type(exc).__name__}: {exc}")
        return False
    finally:
        await embedder.aclose()
    norms = [math.sqrt(_cosine(v, v)) for v in result.vectors]
    related = _cosine(result.vectors[0], result.vectors[1])
    unrelated = _cosine(result.vectors[0], result.vectors[2])
    _ok(
        f"embeddings via {result.model}: {len(result.vectors)} x {result.dimensions}-d, "
        f"norms {min(norms):.3f}-{max(norms):.3f}, {result.latency_ms:.0f} ms"
    )
    if related > unrelated:
        _ok(f"semantic sanity: related {related:.3f} > unrelated {unrelated:.3f}")
        return True
    _fail(f"semantic sanity: related {related:.3f} <= unrelated {unrelated:.3f}")
    return False


async def _check_ai(settings: Settings) -> int:
    if settings.llm_provider == LLMProviderName.GEMINI:
        print(
            "NOTE: on the Gemini free tier, Google may use prompts for product improvement and\n"
            "human review. Only send synthetic or non-sensitive data\n"
            "(see docs/architecture/01-requirements.md, finding C1).\n"
        )
    llm_ok = await _check_llm(settings)
    if settings.gemini_api_key is None and settings.llm_provider == LLMProviderName.OLLAMA:
        print("[SKIP] embeddings: GEMINI_API_KEY not set (embeddings are needed from Phase 6)")
        embeddings_ok = True
    else:
        embeddings_ok = await _check_embeddings(settings)
    return EXIT_OK if llm_ok and embeddings_ok else EXIT_FAILURE


async def _match(settings: Settings) -> int:
    """Re-run comparisons, duplicate detection, rules and review tasks for every processed
    document (after an upgrade, or after rule changes), one short transaction each."""
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    try:
        async with sessionmaker() as session:
            ids = list(
                await session.scalars(
                    select(Document.id)
                    .where(Document.deleted_at.is_(None), Document.status.in_(PROCESSED))
                    .order_by(Document.created_at)
                )
            )
        done = 0
        for document_id in ids:
            async with sessionmaker() as session:
                done += await rematch(session, settings, document_id)
                await session.commit()
        async with sessionmaker() as session:
            open_tasks = await session.scalar(
                select(func.count())
                .select_from(ReviewTask)
                .where(ReviewTask.status.in_(OPEN_TASK_STATUSES))
            )
        _ok(f"matched {done} processed document(s); {open_tasks} open review task(s)")
        return EXIT_OK
    finally:
        await engine.dispose()


async def _reembed(settings: Settings, *, force: bool) -> int:
    """Embed chunks without vectors of the configured model (knowledge and business documents)."""
    from docintel.ai.errors import ProviderError
    from docintel.knowledge.embedding import build_chunk_embedder
    from docintel.knowledge.reembed import reembed

    if not settings.embedding_configured:
        _fail("No embedding provider is configured (EMBEDDING_PROVIDER / GEMINI_API_KEY).")
        return EXIT_FAILURE
    engine = create_engine(settings)
    embedder = build_chunk_embedder(settings)
    try:
        summary = await reembed(create_sessionmaker(engine), embedder, force=force)
    except ProviderError as exc:
        _fail(f"embedding provider error ({type(exc).__name__}); rerun to continue: {exc}")
        return EXIT_FAILURE
    finally:
        await embedder.aclose()
        await engine.dispose()
    _ok(
        f"model {embedder.model}: {summary.knowledge_documents} knowledge and "
        f"{summary.business_documents} business document(s); {summary.chunks_embedded} chunk(s) "
        f"embedded, {summary.chunks_blocked} kept full-text only by the sensitivity gate"
    )
    return EXIT_OK


async def _llm_usage(settings: Settings, days: int) -> int:
    """Calls, tokens and estimated cost per day, provider, model and purpose (llm_calls)."""
    since = datetime.now(UTC) - timedelta(days=days)
    engine = create_engine(settings)
    try:
        async with create_sessionmaker(engine)() as session:
            day = func.date_trunc("day", LLMCall.created_at)
            rows = (
                await session.execute(
                    select(
                        day,
                        LLMCall.provider,
                        LLMCall.model,
                        LLMCall.purpose,
                        func.count(),
                        func.count().filter(LLMCall.status == LLMCallStatus.FAILED),
                        func.coalesce(func.sum(LLMCall.input_tokens), 0),
                        func.coalesce(func.sum(LLMCall.output_tokens), 0),
                        func.sum(LLMCall.estimated_cost_usd),
                    )
                    .where(LLMCall.created_at >= since)
                    .group_by(day, LLMCall.provider, LLMCall.model, LLMCall.purpose)
                    .order_by(day.desc(), LLMCall.provider, LLMCall.model, LLMCall.purpose)
                )
            ).all()
    finally:
        await engine.dispose()
    if not rows:
        print(f"No LLM calls in the last {days} day(s).")
        return EXIT_OK
    print(
        f"{'day':<10} {'provider':<9} {'model':<28} {'purpose':<21} {'calls':>5} {'failed':>6}  "
        f"{'tokens in/out':<17} est. cost USD"
    )
    for when, provider, model, purpose, calls, failed, tokens_in, tokens_out, cost in rows:
        estimate = "not configured" if cost is None else f"{cost:.6f}"
        print(
            f"{when:%Y-%m-%d} {provider:<9} {model:<28} {purpose:<21} {calls:>5} {failed:>6}  "
            f"{tokens_in:>8}/{tokens_out:<8} {estimate}"
        )
    if settings.llm_daily_request_budget:
        print(
            f"Daily request budget: {settings.llm_daily_request_budget} (LLM_DAILY_REQUEST_BUDGET)"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------- worker
def _ocr_provider(settings: Settings) -> TesseractOCRProvider:
    return TesseractOCRProvider(
        command=settings.tesseract_cmd,
        languages=settings.ocr_languages,
        timeout_seconds=settings.ocr_page_timeout_seconds,
    )


async def _check_ocr(settings: Settings) -> int:
    """Verify the OCR engine and languages, then read a rendered sample line."""
    from PIL import Image, ImageDraw, ImageFont

    provider = _ocr_provider(settings)
    try:
        version = await provider.verify()
    except OCRUnavailableError as exc:
        _fail(str(exc))
        return EXIT_FAILURE
    _ok(f"{version}, languages: {settings.ocr_languages}")
    image = Image.new("L", (1400, 140), 255)
    ImageDraw.Draw(image).text(
        (40, 30), "Invoice 1001 Total 250.00", fill=0, font=ImageFont.load_default(size=56)
    )
    result = await provider.recognize(image, dpi=300)
    text = " ".join(word.text for word in result.words)
    if "1001" not in text:
        _fail(f"OCR sample read as {text!r}")
        return EXIT_FAILURE
    _ok(f"sample read as {text!r} in {result.latency_ms:.0f} ms")
    return EXIT_OK


async def _worker(settings: Settings, *, until_idle: bool) -> int:
    try:
        version = await _ocr_provider(settings).verify()
    except OCRUnavailableError as exc:
        _fail(f"{exc}. The worker needs Tesseract to process scanned documents.")
        return EXIT_FAILURE
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    services = None
    try:
        async with sessionmaker() as session:
            corrections = await load_corrections(session)
        services = build_processing_services(
            settings, sessionmaker=sessionmaker, corrections=corrections
        )
        logger.info("worker.ocr_ready", engine=version, languages=settings.ocr_languages)
        worker = Worker(
            settings=settings,
            sessionmaker=sessionmaker,
            storage=build_storage(settings),
            services=services,
        )
        if until_idle:
            processed = await worker.run_until_idle()
            _ok(f"processed {processed} job(s); queue idle")
            return EXIT_OK
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        await worker.run(stop)
        return EXIT_OK
    finally:
        if services is not None:
            await services.aclose()
        await engine.dispose()


def _worker_health(settings: Settings) -> int:
    path = settings.worker_heartbeat_file
    if path is None:
        _fail("WORKER_HEARTBEAT_FILE is not configured")
        return EXIT_FAILURE
    age = heartbeat_age_seconds(path)
    # A healthy worker touches the file at least once per poll interval or lease heartbeat.
    limit = max(60.0, settings.worker_poll_interval_seconds * 3, settings.job_lease_seconds / 2)
    if age is None or age > limit:
        _fail(f"worker heartbeat is stale (age={age}, limit={limit})")
        return EXIT_FAILURE
    return EXIT_OK


# ---------------------------------------------------------------------------- synthetic data
def _generate_documents(args: argparse.Namespace) -> int:
    try:
        from docintel.synthetic.generator import generate_dataset
    except ImportError:  # reportlab lives in the optional `synthetic` dependency group
        _fail("The synthetic generator needs reportlab: run `uv sync` (default groups).")
        return EXIT_USAGE
    manifest = generate_dataset(
        Path(args.output), seed=args.seed, bundles_per_scenario=args.bundles_per_scenario
    )
    scenarios = len(manifest["scenarios"])
    _ok(
        f"generated {manifest['document_count']} documents across {scenarios} scenarios "
        f"in {args.output} (seed {args.seed})"
    )
    return EXIT_OK


async def _ingest(args: argparse.Namespace) -> int:
    """HTTP client: needs only the API URL and credentials, not the server's configuration."""
    from docintel.tools.ingest import IngestError, ingest_directory, summarize

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    elif os.environ.get("SEED_USER_PASSWORD"):
        password = os.environ["SEED_USER_PASSWORD"]
    else:
        _fail("Provide the password with --password-stdin or SEED_USER_PASSWORD.")
        return EXIT_USAGE
    try:
        items = await ingest_directory(
            Path(args.directory),
            api_url=args.api_url,
            email=args.email,
            password=password,
            timeout_seconds=args.timeout,
        )
    except IngestError as exc:
        _fail(str(exc))
        return EXIT_FAILURE
    for item in items:
        detail = item.error or " ".join(
            part
            for part in (
                item.inspection_kind,
                item.document_type,
                ",".join(item.review_reasons or []),
            )
            if part
        )
        print(f"  {item.status or item.http_status!s:<16} {item.file:<40} {detail}")
    summary = summarize(items)
    _ok(f"summary: {summary} (report: {Path(args.directory) / 'ingest-report.json'})")
    unfinished = sum(
        1 for item in items if item.document_id and item.status in {"PENDING", "PROCESSING"}
    )
    if unfinished:
        _fail(f"{unfinished} document(s) unfinished after {args.timeout}s; is a worker running?")
    # REVIEW_REQUIRED is a successful outcome (a person decides), unless --require-completed.
    accepted = {"COMPLETED"} if args.require_completed else {"COMPLETED", "REVIEW_REQUIRED"}
    not_done = [item.file for item in items if item.status not in accepted]
    if not_done:
        _fail(
            f"{len(not_done)} document(s) not {'/'.join(sorted(accepted))}: {', '.join(not_done)}"
        )
        return EXIT_FAILURE
    return EXIT_OK


# ---------------------------------------------------------------------------- evaluation
SUITES = (
    "ocr",
    "classification",
    "tables",
    "extraction",
    "discrepancies",
    "versions",
    "retrieval",
)


async def _evaluate(args: argparse.Namespace) -> int:
    """Offline evaluation with production defaults; needs the dev/synthetic dependency groups."""
    try:
        from docintel.evaluation.classification_suite import run_classification_suite
        from docintel.evaluation.discrepancy_suite import run_discrepancy_suite
        from docintel.evaluation.extraction_suite import run_extraction_suite
        from docintel.evaluation.ocr_suite import run_ocr_suite
        from docintel.evaluation.retrieval_suite import run_retrieval_suite
        from docintel.evaluation.tables_suite import run_tables_suite
        from docintel.evaluation.versions_suite import run_versions_suite
    except ImportError as exc:  # reportlab is not installed in the runtime image
        _fail(f"evaluation needs the synthetic dependency group (uv sync): {exc}")
        return EXIT_USAGE
    defaults = Settings.model_fields
    output = Path(args.output)
    suites = list(SUITES) if args.suite == "all" else [args.suite]
    try:
        for suite in suites:
            if suite == "ocr":
                report = await run_ocr_suite(output, quick=args.quick, languages=args.languages)
            elif suite == "tables":
                report = await run_tables_suite(output, quick=args.quick, languages=args.languages)
            elif suite == "extraction":
                report = await run_extraction_suite(
                    output, quick=args.quick, languages=args.languages
                )
            elif suite == "discrepancies":
                report = await run_discrepancy_suite(
                    output, quick=args.quick, languages=args.languages
                )
            elif suite == "retrieval":
                database_url = args.database_url or os.environ.get(
                    "TEST_DATABASE_URL", os.environ.get("DATABASE_URL")
                )
                if not database_url:
                    _fail(
                        "the retrieval suite needs a PostgreSQL server: pass --database-url "
                        "or set TEST_DATABASE_URL (a scratch database is created and dropped)"
                    )
                    return EXIT_USAGE
                report = await run_retrieval_suite(
                    output, database_url=database_url, quick=args.quick
                )
            elif suite == "versions":
                report = await run_versions_suite(
                    output, quick=args.quick, languages=args.languages
                )
            else:
                report = await run_classification_suite(
                    output,
                    threshold=defaults["classification_min_confidence"].default,
                    corpus_per_class=defaults["classification_corpus_per_class"].default,
                    quick=args.quick,
                    languages=args.languages,
                )
            _ok(f"{report.title}: {output / (suite + '.md')}")
    except OCRUnavailableError as exc:
        _fail(str(exc))
        return EXIT_FAILURE
    return EXIT_OK


# ---------------------------------------------------------------------------- entrypoint
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docintel", description="Document Intelligence admin CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="create demo departments and users (local/test only)")
    create = commands.add_parser("create-user", help="create a user account")
    create.add_argument("--email", required=True)
    create.add_argument("--full-name", required=True)
    create.add_argument("--role", required=True, choices=[role.value for role in Role])
    create.add_argument("--department", help="department name (created if missing)")
    create.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin"
    )
    commands.add_parser("check-ai", help="verify AI provider credentials and models")
    commands.add_parser(
        "match", help="Re-run matching, rules and review tasks for all processed documents"
    )
    reembed = commands.add_parser(
        "reembed", help="add vectors of the configured embedding model to indexed chunks"
    )
    reembed.add_argument(
        "--force", action="store_true", help="re-embed every chunk, not only stale ones"
    )
    usage = commands.add_parser("llm-usage", help="LLM calls, tokens and estimated cost")
    usage.add_argument("--days", type=int, default=7, help="look back this many days (default 7)")
    commands.add_parser("check-ocr", help="verify the OCR engine and configured languages")
    worker = commands.add_parser("worker", help="run the background job worker")
    worker.add_argument(
        "--until-idle", action="store_true", help="process runnable jobs, then exit"
    )
    commands.add_parser("worker-health", help="exit 0 if the worker heartbeat is fresh")
    generate = commands.add_parser("generate-documents", help="create a synthetic dataset")
    generate.add_argument("--output", default="synthetic_data/generated")
    generate.add_argument("--seed", type=int, default=42)
    generate.add_argument("--bundles-per-scenario", type=int, default=1)
    ingest = commands.add_parser("ingest", help="upload a directory via the REST API")
    ingest.add_argument("directory")
    ingest.add_argument("--api-url", default="http://localhost:8000")
    ingest.add_argument("--email", default="analyst@docintel.local")
    ingest.add_argument("--password-stdin", action="store_true")
    ingest.add_argument("--timeout", type=float, default=300.0)
    evaluate = commands.add_parser("evaluate", help="run evaluation suites and write reports")
    evaluate.add_argument(
        "--suite",
        choices=[*SUITES, "all"],
        default="all",
    )
    evaluate.add_argument("--output", default="../evaluation/reports")
    evaluate.add_argument("--quick", action="store_true", help="small datasets (smoke test)")
    evaluate.add_argument("--languages", default="eng", help="Tesseract languages")
    evaluate.add_argument(
        "--database-url",
        help="PostgreSQL server for the retrieval suite (default: TEST_DATABASE_URL)",
    )
    ingest.add_argument(
        "--require-completed",
        action="store_true",
        help="fail unless every document is COMPLETED (REVIEW_REQUIRED counts as a failure)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # Client-side commands work without the server's configuration (database, secrets).
    if args.command == "generate-documents":
        configure_logging(level="WARNING", log_format=LogFormat.CONSOLE)
        return _generate_documents(args)
    if args.command == "ingest":
        configure_logging(level="WARNING", log_format=LogFormat.CONSOLE)
        return asyncio.run(_ingest(args))
    if args.command == "evaluate":
        configure_logging(level="WARNING", log_format=LogFormat.CONSOLE)
        return asyncio.run(_evaluate(args))

    settings = get_settings()
    configure_logging(level="WARNING", log_format=settings.effective_log_format)
    match args.command:
        case "seed":
            return asyncio.run(_seed(settings))
        case "create-user":
            return asyncio.run(_create_user(settings, args))
        case "check-ai":
            return asyncio.run(_check_ai(settings))
        case "llm-usage":
            return asyncio.run(_llm_usage(settings, args.days))
        case "match":
            return asyncio.run(_match(settings))
        case "reembed":
            return asyncio.run(_reembed(settings, force=args.force))
        case "check-ocr":
            return asyncio.run(_check_ocr(settings))
        case "worker":
            configure_logging(level=settings.log_level, log_format=settings.effective_log_format)
            return asyncio.run(_worker(settings, until_idle=args.until_idle))
        case "worker-health":
            return _worker_health(settings)
        case _:  # argparse enforces the choices
            return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
