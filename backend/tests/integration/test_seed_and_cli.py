from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel import cli
from docintel.auth.passwords import PasswordPolicyError
from docintel.auth.seed import SEED_USERS, seed_demo_identities
from docintel.core.errors import ConflictError
from docintel.db.models import AuditLog, Department, User, Vendor
from docintel.tools import ingest as ingest_tool
from docintel.tools.ingest import IngestItem
from docintel.vendors.seed import seed_demo_vendors
from tests.conftest import PRODUCTION_SECRET, make_settings

pytestmark = pytest.mark.integration

SEED_PASSWORD = "demo passphrase for local use"


async def test_seed_creates_departments_and_users_idempotently(db_session: AsyncSession) -> None:
    first = await seed_demo_identities(db_session, password=SEED_PASSWORD)
    assert sorted(first.created_users) == sorted(u.email for u in SEED_USERS)
    assert first.existing_users == []

    second = await seed_demo_identities(db_session, password=SEED_PASSWORD)
    assert second.created_users == []
    assert sorted(second.existing_users) == sorted(u.email for u in SEED_USERS)

    admin = await db_session.scalar(select(User).where(User.email == "admin@docintel.local"))
    assert admin is not None
    assert admin.department_id is None
    analyst = await db_session.scalar(select(User).where(User.email == "analyst@docintel.local"))
    assert analyst is not None
    assert analyst.department is not None
    assert analyst.department.name == "Finance"
    department_count = await db_session.scalar(select(func.count()).select_from(Department))
    assert (department_count or 0) >= 4
    seeded_ids = [
        str(user_id)
        for user_id in await db_session.scalars(
            select(User.id).where(User.email.in_([u.email for u in SEED_USERS]))
        )
    ]
    created_events = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "user.created", AuditLog.entity_id.in_(seeded_ids))
    )
    assert created_events == len(SEED_USERS)


async def test_seed_rejects_weak_password(db_session: AsyncSession) -> None:
    with pytest.raises(PasswordPolicyError):
        await seed_demo_identities(db_session, password="short")


async def test_create_user_rejects_duplicate_email(db_session: AsyncSession) -> None:
    from docintel.audit.service import SYSTEM_REQUEST
    from docintel.auth.service import UserService
    from docintel.db.models import Role

    service = UserService(db_session)
    await service.create_user(
        email="dup@example.test",
        full_name="First",
        password=SEED_PASSWORD,
        role=Role.VIEWER,
        department=None,
        meta=SYSTEM_REQUEST,
    )
    with pytest.raises(ConflictError):
        await service.create_user(
            email="DUP@example.test",
            full_name="Second",
            password=SEED_PASSWORD,
            role=Role.VIEWER,
            department=None,
            meta=SYSTEM_REQUEST,
        )


async def test_cli_seed_refuses_in_production(capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(
        app_env="production",
        jwt_secret_key=PRODUCTION_SECRET,
        metrics_token=PRODUCTION_SECRET,
        seed_user_password=SEED_PASSWORD,
    )
    assert await cli._seed(settings) == cli.EXIT_USAGE
    assert "Refusing" in capsys.readouterr().out


async def test_cli_seed_requires_password(capsys: pytest.CaptureFixture[str]) -> None:
    assert await cli._seed(make_settings(seed_user_password=None)) == cli.EXIT_USAGE
    assert "SEED_USER_PASSWORD" in capsys.readouterr().out


async def test_cli_check_ai_without_key_fails_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    assert await cli._check_ai(make_settings(gemini_api_key=None)) == cli.EXIT_FAILURE
    output = capsys.readouterr().out
    assert "GEMINI_API_KEY is not set" in output
    assert "synthetic" in output


async def test_cli_check_ai_reports_an_unreachable_ollama(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(
        llm_provider="ollama",
        ollama_model="vlm:7b",
        ollama_base_url="http://127.0.0.1:9",  # nothing listens on the discard port
        llm_max_retries=0,
    )
    assert await cli._check_ai(settings) == cli.EXIT_FAILURE
    output = capsys.readouterr().out
    assert "is the server running" in output
    assert "[SKIP] embeddings" in output
    assert "free tier" not in output  # the Gemini data-use warning is Gemini-specific


async def test_cli_llm_usage_summarizes_recorded_calls(
    database_url: str, db_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(database_url=database_url, llm_daily_request_budget=50)
    assert await cli._llm_usage(settings, days=1) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "No LLM calls" in out or "est. cost USD" in out


async def test_seed_creates_the_demo_vendor_master(db_session: AsyncSession) -> None:
    created, existing = await seed_demo_vendors(db_session)
    assert created == []  # the session fixture already seeded them: seeding is idempotent
    assert "Kestrel Industrial Supply Inc." in existing
    vendor = await db_session.scalar(
        select(Vendor).where(Vendor.canonical_name == "Altamira Components GmbH")
    )
    assert vendor is not None
    assert (vendor.default_currency, vendor.tax_id_key, vendor.aliases) == (
        "EUR",
        "DE298374615",
        [],
    )


def test_generate_documents_needs_no_server_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for variable in ("DATABASE_URL", "JWT_SECRET_KEY"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env file to fall back on
    output = tmp_path / "dataset"
    assert cli.main(["generate-documents", "--output", str(output), "--seed", "5"]) == cli.EXIT_OK
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["seed"] == 5
    assert manifest["document_count"] > 0
    assert "generated" in capsys.readouterr().out


def test_generate_documents_for_one_scenario(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "demo"
    argv = ["generate-documents", "--output", str(output), "--seed", "9"]
    assert cli.main([*argv, "--scenario", "UNIT_PRICE_MISMATCH"]) == cli.EXIT_OK
    manifest = json.loads((output / "manifest.json").read_text())
    assert {entry["scenario"] for entry in manifest["documents"]} == {"UNIT_PRICE_MISMATCH"}
    assert sorted(entry["document_type"] for entry in manifest["documents"]) == [
        "DELIVERY_NOTE",
        "INVOICE",
        "PURCHASE_ORDER",
    ]
    capsys.readouterr()
    assert cli.main([*argv, "--scenario", "NOT_A_SCENARIO"]) == cli.EXIT_USAGE
    assert "UNIT_PRICE_MISMATCH" in capsys.readouterr().out


def test_ingest_without_password_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SEED_USER_PASSWORD", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main(["ingest", str(tmp_path)]) == cli.EXIT_USAGE
    assert "--password-stdin" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("items", "flags", "expected", "message"),
    [
        ([IngestItem("a.pdf", "A", "id-a", status="COMPLETED")], [], cli.EXIT_OK, None),
        (
            [
                IngestItem("a.pdf", "A", "id-a", status="COMPLETED"),
                IngestItem("r.pdf", "R", "id-r", status="REVIEW_REQUIRED"),
            ],
            [],
            cli.EXIT_OK,
            None,
        ),
        (
            [IngestItem("r.pdf", "R", "id-r", status="REVIEW_REQUIRED")],
            ["--require-completed"],
            cli.EXIT_FAILURE,
            "1 document(s) not COMPLETED: r.pdf",
        ),
        (
            [
                IngestItem("a.pdf", "A", "id-a", status="COMPLETED"),
                IngestItem("b.pdf", "B", "id-b", status="FAILED"),
                IngestItem("c.pdf", "C", http_status=415, error="unsupported"),
            ],
            [],
            cli.EXIT_FAILURE,
            "2 document(s) not COMPLETED/REVIEW_REQUIRED: b.pdf, c.pdf",
        ),
    ],
)
def test_ingest_exit_code_reflects_document_outcomes(
    items: list[IngestItem],
    flags: list[str],
    expected: int,
    message: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_ingest_directory(*_args: object, **_kwargs: object) -> list[IngestItem]:
        return items

    monkeypatch.setenv("SEED_USER_PASSWORD", "unused-in-this-test")
    monkeypatch.setattr(ingest_tool, "ingest_directory", fake_ingest_directory)
    assert cli.main(["ingest", str(tmp_path), *flags]) == expected
    if message is not None:
        assert message in capsys.readouterr().out
