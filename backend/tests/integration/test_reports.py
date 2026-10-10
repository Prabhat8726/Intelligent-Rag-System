"""Reports (Module 30): generation from current data, reproducibility, downloads, access, and
the generate_report / get_workflow_status tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from docintel.db.models import AuditLog, Report, User
from docintel.reports.render import render, sha256
from docintel.synthetic.scenarios import Scenario
from tests.conftest import auth_headers
from tests.integration.conftest import Env
from tests.integration.test_agent_analysis import processed
from tests.integration.test_agent_tools import as_user, ok, registry
from tests.integration.test_workflows import decide, run

pytestmark = pytest.mark.integration


async def generate(env: Env, user: User, report_type: str, subject_id: str) -> Any:
    return await env.client.post(
        "/api/v1/reports",
        json={"report_type": report_type, "subject_id": subject_id},
        headers=auth_headers(user),
    )


async def test_reports_are_reproducible_scoped_and_downloadable(env: Env, tmp_path: Path) -> None:
    clean = await processed(env, tmp_path, Scenario.CLEAN_MATCH, 22)
    workflow = await run(env, env.analyst, clean["INV"], "INVOICE_PROCESSING")
    approved = (await decide(env, env.manager, workflow["id"], "approve", "Fine.")).json()
    (workflow_report,) = approved["report_ids"]

    first = await generate(env, env.reviewer, "INVOICE_VERIFICATION", clean["INV"])
    assert first.status_code == 201, first.text
    assert first.headers["location"] == f"/api/v1/reports/{first.json()['id']}"
    report = first.json()
    content: str = report["content"]
    for section in (
        "# Invoice verification: CLEA-INV.pdf",
        "## Source documents",
        "## Extracted values",
        "## Comparison",
        "## Rule results",
        "## AI analysis",
        "## Workflows and decisions",
        "## Human reviews",
    ):
        assert section in content
    assert "APPROVE_FOR_PAYMENT - EXECUTED" in content
    assert f"Decided by {env.manager.email}" in content
    assert "CLEA-PO.pdf" in content  # the related order is a source document
    assert set(report["document_ids"]) >= {clean["INV"], clean["PO"]}
    assert report["content_sha256"] == sha256(content)

    # Same data, same report: the content does not depend on when it is generated.
    second = (await generate(env, env.reviewer, "INVOICE_VERIFICATION", clean["INV"])).json()
    assert second["content_sha256"] == report["content_sha256"]
    assert second["id"] != report["id"]
    verified = await env.client.post(
        f"/api/v1/reports/{report['id']}/verify", headers=auth_headers(env.viewer)
    )
    assert verified.json() == {
        "report_id": report["id"],
        "matches": True,
        "content_sha256": report["content_sha256"],
    }
    # A stored report that was altered no longer verifies.
    async with env.maker() as session, session.begin():
        await session.execute(
            update(Report).where(Report.id == second["id"]).values(content="tampered")
        )
    tampered = await env.client.post(
        f"/api/v1/reports/{second['id']}/verify", headers=auth_headers(env.viewer)
    )
    assert tampered.json()["matches"] is False

    markdown = await env.client.get(
        f"/api/v1/reports/{report['id']}/download", headers=auth_headers(env.viewer)
    )
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.headers["x-content-sha256"] == report["content_sha256"]
    assert 'filename="invoice-verification-clea-inv.pdf-' in markdown.headers["content-disposition"]
    assert markdown.text == content
    snapshot = await env.client.get(
        f"/api/v1/reports/{report['id']}/download",
        params={"format": "json"},
        headers=auth_headers(env.viewer),
    )
    assert render(json.loads(snapshot.text)) == content  # the snapshot renders to the report

    listing = await env.client.get(
        "/api/v1/reports", params={"document_id": clean["INV"]}, headers=auth_headers(env.viewer)
    )
    assert {item["id"] for item in listing.json()["items"]} == {
        workflow_report,
        report["id"],
        second["id"],
    }
    # Another department sees none of it.
    hidden = await env.client.get(
        f"/api/v1/reports/{report['id']}", headers=auth_headers(env.outsider)
    )
    assert hidden.status_code == 404
    elsewhere = await env.client.get("/api/v1/reports", headers=auth_headers(env.outsider))
    assert elsewhere.json()["total"] == 0
    assert (
        await generate(env, env.outsider, "INVOICE_VERIFICATION", clean["INV"])
    ).status_code == 404
    assert (
        await generate(env, env.viewer, "INVOICE_VERIFICATION", clean["INV"])
    ).status_code == 403
    wrong = await generate(env, env.analyst, "CONTRACT_REVIEW", clean["INV"])
    assert wrong.status_code == 422

    async with env.maker() as session:
        downloads = list(
            await session.scalars(
                select(AuditLog.details)
                .where(AuditLog.action == "report.downloaded", AuditLog.entity_id == report["id"])
                .order_by(AuditLog.id)
            )
        )
        assert downloads == [
            {"format": "md", "sha256": report["content_sha256"]},
            {"format": "json", "sha256": report["content_sha256"]},
        ]


async def test_analysis_and_comparison_reports(env: Env, tmp_path: Path) -> None:
    mismatch = await processed(env, tmp_path, Scenario.UNIT_PRICE_MISMATCH, 21)
    workflow = await run(env, env.analyst, mismatch["INV"], "INVOICE_PROCESSING")
    # The approver-side: a manager may report on the workflow's investigation (visible with
    # the document) though investigations are otherwise personal.
    analysis = await generate(env, env.manager, "AI_ANALYSIS", workflow["agent_run_id"])
    assert analysis.status_code == 201, analysis.text
    content = analysis.json()["content"]
    assert "# AI analysis: Can we pay this invoice?" in content
    assert "Recommendation: HOLD_FOR_REVIEW (rules)" in content
    assert "### Invoice processing workflow" in content
    assert mismatch["INV"] in analysis.json()["document_ids"]

    comparisons = await env.client.get(
        "/api/v1/comparisons",
        params={"document_id": mismatch["INV"]},
        headers=auth_headers(env.analyst),
    )
    comparison_id = comparisons.json()["items"][0]["id"]
    compared = await generate(env, env.analyst, "DOCUMENT_COMPARISON", comparison_id)
    assert compared.status_code == 201, compared.text
    assert "MISMATCH" in compared.json()["content"]
    assert compared.json()["subject_type"] == "COMPARISON"

    compliance = await generate(env, env.analyst, "COMPLIANCE_REVIEW", mismatch["PO"])
    assert compliance.status_code == 201, compliance.text
    assert "## Extracted values" not in compliance.json()["content"]
    assert "## Rule results" in compliance.json()["content"]


async def test_report_and_workflow_tools(env: Env, tmp_path: Path) -> None:
    clean = await processed(env, tmp_path, Scenario.CLEAN_MATCH, 22)
    workflow = await run(env, env.analyst, clean["INV"], "INVOICE_PROCESSING")
    tools = registry(env)

    status = ok(
        await tools.call("get_workflow_status", {"document_id": clean["INV"]}, as_user(env.viewer))
    )
    (item,) = status["workflows"]
    assert (item["status"], item["actions"][0]["status"]) == (
        "AWAITING_APPROVAL",
        "AWAITING_APPROVAL",
    )
    assert item["steps"]["approval"] == "RUNNING"
    invalid = await tools.call(
        "get_workflow_status",
        {"document_id": clean["INV"], "workflow_id": workflow["id"]},
        as_user(env.viewer),
    )
    assert invalid.status.value == "INVALID"
    hidden = await tools.call(
        "get_workflow_status", {"workflow_id": workflow["id"]}, as_user(env.outsider)
    )
    assert (hidden.status.value, hidden.error) == ("FAILED", "Not found or not permitted.")

    denied = await tools.call(
        "generate_report",
        {"report_type": "INVOICE_VERIFICATION", "subject_id": clean["INV"]},
        as_user(env.viewer),
    )
    assert denied.status.value == "DENIED"
    made = ok(
        await tools.call(
            "generate_report",
            {"report_type": "INVOICE_VERIFICATION", "subject_id": clean["INV"]},
            as_user(env.analyst),
        )
    )
    assert made["download_path"] == f"/api/v1/reports/{made['report_id']}/download"
    # No tool can decide: approval stays with people.
    assert "approve" not in " ".join(tools.names())
