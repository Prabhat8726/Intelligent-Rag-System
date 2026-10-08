"""Management CLI.

docintel seed                      demo departments + one user per role (not in staging/prod)
docintel create-user --email ...   create a user (password prompted or read from stdin)
docintel check-ai                  verify the configured AI provider end-to-end
docintel worker [--until-idle]     run the background job worker
docintel worker-health             exit 0 if the worker heartbeat is fresh (container probe)
docintel generate-documents        synthetic POs/invoices/delivery notes with ground truth
docintel ingest DIR                upload a directory through the REST API and wait for results
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
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from docintel.ai.base import EmbeddingTask, LLMRequest, ModelTier
from docintel.ai.errors import ProviderError
from docintel.ai.gemini import GeminiLLMProvider
from docintel.ai.registry import build_embedding_provider, build_llm_provider
from docintel.audit.service import SYSTEM_REQUEST
from docintel.auth.passwords import PasswordPolicyError
from docintel.auth.seed import seed_demo_identities
from docintel.auth.service import UserService
from docintel.core.config import LogFormat, Settings, get_settings
from docintel.core.errors import ConflictError
from docintel.core.logging import configure_logging
from docintel.db.models import Role
from docintel.db.session import create_engine, create_sessionmaker
from docintel.storage import build_storage
from docintel.workers.runner import Worker, heartbeat_age_seconds

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
    except PasswordPolicyError as exc:
        _fail(f"SEED_USER_PASSWORD rejected: {exc}")
        return EXIT_USAGE
    finally:
        await engine.dispose()
    for email in report.created_users:
        _ok(f"created {email}")
    for email in report.existing_users:
        print(f"[SKIP] {email} already exists")
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
    print(
        "NOTE: on the Gemini free tier, Google may use prompts for product improvement and\n"
        "human review. Only send synthetic or non-sensitive data\n"
        "(see docs/architecture/01-requirements.md, finding C1).\n"
    )
    llm_ok = await _check_llm(settings)
    embeddings_ok = await _check_embeddings(settings)
    return EXIT_OK if llm_ok and embeddings_ok else EXIT_FAILURE


# ---------------------------------------------------------------------------- worker
async def _worker(settings: Settings, *, until_idle: bool) -> int:
    engine = create_engine(settings)
    try:
        worker = Worker(
            settings=settings,
            sessionmaker=create_sessionmaker(engine),
            storage=build_storage(settings),
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
        detail = item.inspection_kind or item.error or ""
        print(f"  {item.status or item.http_status!s:<16} {item.file:<40} {detail}")
    summary = summarize(items)
    _ok(f"summary: {summary} (report: {Path(args.directory) / 'ingest-report.json'})")
    unfinished = sum(
        1 for item in items if item.document_id and item.status in {"PENDING", "PROCESSING"}
    )
    if unfinished:
        _fail(f"{unfinished} document(s) unfinished after {args.timeout}s; is a worker running?")
    not_completed = [item.file for item in items if item.status != "COMPLETED"]
    if not_completed:
        _fail(f"{len(not_completed)} document(s) not completed: {', '.join(not_completed)}")
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

    settings = get_settings()
    configure_logging(level="WARNING", log_format=settings.effective_log_format)
    match args.command:
        case "seed":
            return asyncio.run(_seed(settings))
        case "create-user":
            return asyncio.run(_create_user(settings, args))
        case "check-ai":
            return asyncio.run(_check_ai(settings))
        case "worker":
            configure_logging(level=settings.log_level, log_format=settings.effective_log_format)
            return asyncio.run(_worker(settings, until_idle=args.until_idle))
        case "worker-health":
            return _worker_health(settings)
        case _:  # argparse enforces the choices
            return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
