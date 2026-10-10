"""The evaluations API (Phase 10): recorded runs, the latest per suite, gates and headlines."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.db.models import Department, EvaluationSource, Role
from docintel.evaluation.gates import Gate, check_report
from docintel.evaluation.report import Report
from docintel.evaluation.store import record_report, report_file_from
from tests.conftest import auth_headers, make_user

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def agent_report(*, unsafe: int, quick: bool = False, hours_ago: int = 0) -> Report:
    development = {
        "runs": 70,
        "task_success": {"named": 1.0, "search": 1.0, "policy": 1.0},
        "unsafe_recommendations": unsafe,
        "defect_detection": 1.0,
        "false_failures_on_clean_invoices": 0,
    }
    return Report(
        suite="agent",
        title="Agent investigation evaluation",
        dataset={"development_seed": 7, "held_out_seed": 11},
        config={"mode": "deterministic"},
        metrics={"development": development, "held-out": {**development, "runs": 60}},
        environment={"python": "3.13"},
        quick=quick,
        notes=["Synthetic data."],
        tables=[("Overall", ["Measure", "Value"], [["Unsafe", str(unsafe)]])],
        created_at=(NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
        git_revision=f"{uuid.uuid4().hex[:12]}",
    )


def ocr_report() -> Report:
    return Report(
        suite="ocr",
        title="OCR evaluation (synthetic-noisy)",
        dataset={"name": "synthetic-noisy"},
        config={},
        metrics={"clean_300dpi": {"default": {"pages": 35, "cer": {"mean": 0.012}}}},
        environment={},
        created_at=NOW.isoformat(timespec="seconds"),
        git_revision="abc1234def56",
    )


GATES = [
    Gate.model_validate(
        {
            "suite": "agent",
            "metric": ["development", "unsafe_recommendations"],
            "max": 0,
            "why": "No unsafe payment.",
        }
    )
]


async def record(session: AsyncSession, report: Report) -> uuid.UUID:
    gates = check_report(GATES, suite=report.suite, quick=report.quick, metrics=report.metrics)
    row, _ = await record_report(
        session, report_file_from(report), source=EvaluationSource.RUN, gates=gates
    )
    return row.id


async def test_the_latest_full_run_of_each_suite_with_its_gates_and_headlines(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    analyst = await make_user(db_session, role=Role.ANALYST, department=department)
    older = await record(db_session, agent_report(unsafe=1, hours_ago=5))
    newest = await record(db_session, agent_report(unsafe=0, hours_ago=1))
    quick = await record(db_session, agent_report(unsafe=0, quick=True))
    ocr = await record(db_session, ocr_report())

    response = await client.get(
        "/api/v1/evaluations", params={"latest": "true"}, headers=auth_headers(analyst)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    by_suite = {item["suite"]: item for item in body["items"]}
    assert [item["suite"] for item in body["items"]][:2] == ["ocr", "agent"]  # README order
    assert by_suite["agent"]["id"] == str(newest)  # not the older run, not the quick one
    assert by_suite["agent"]["gates"] == {"passed": True, "mode": "full", "checks": 1, "failed": 0}
    assert by_suite["ocr"]["gates"] is None  # no gate applied when it was recorded
    headlines = {h["label"]: h["value"] for h in by_suite["agent"]["headlines"]}
    assert headlines[
        "Agent: unsafe recommendations / planted defect reported / false failures on clean invoices"
    ] == ("development: 0 / 100% / 0 · held-out: 0 / 100% / 0")
    # This OCR report has no light-scan run: the row is there, without a value.
    ocr_rows = {h["label"]: h["value"] for h in by_suite["ocr"]["headlines"]}
    assert ocr_rows["OCR CER / WER, light scan, 150 DPI"] is None

    history = await client.get(
        "/api/v1/evaluations",
        params={"suite": "agent", "include_quick": "true"},
        headers=auth_headers(analyst),
    )
    ids = [item["id"] for item in history.json()["items"]]
    assert ids == [str(quick), str(newest), str(older)]  # newest first, quick included
    failed = next(i for i in history.json()["items"] if i["id"] == str(older))
    assert failed["gates"]["passed"] is False
    assert failed["gates"]["failed"] == 1
    full_only = await client.get(
        "/api/v1/evaluations", params={"suite": "agent"}, headers=auth_headers(analyst)
    )
    assert str(quick) not in [item["id"] for item in full_only.json()["items"]]
    assert ocr not in ids


async def test_one_run_in_full(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department
) -> None:
    manager = await make_user(db_session, role=Role.MANAGER, department=department)
    run = await record(db_session, agent_report(unsafe=1))
    response = await client.get(f"/api/v1/evaluations/{run}", headers=auth_headers(manager))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tables"] == [
        {"heading": "Overall", "header": ["Measure", "Value"], "rows": [["Unsafe", "1"]]}
    ]
    (check,) = body["gate_checks"]
    assert check["metric"] == ["development", "unsafe_recommendations"]
    assert check["value"] == 1
    assert check["passed"] is False
    assert check["problem"] == "above 0"
    assert body["report_markdown"].startswith("# Agent investigation evaluation")
    assert body["dataset"]["held_out_seed"] == 11
    assert body["source"] == "RUN"
    missing = await client.get(f"/api/v1/evaluations/{uuid.uuid4()}", headers=auth_headers(manager))
    assert missing.status_code == 404


@pytest.mark.parametrize("role", [Role.VIEWER, Role.REVIEWER])
async def test_evaluations_need_the_permission(
    client: httpx.AsyncClient, db_session: AsyncSession, department: Department, role: Role
) -> None:
    user = await make_user(db_session, role=role, department=department)
    response = await client.get("/api/v1/evaluations", headers=auth_headers(user))
    assert response.status_code == 403
    assert (await client.get("/api/v1/evaluations")).status_code == 401
