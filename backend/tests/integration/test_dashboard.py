"""Dashboard (Module 19) and the inbox's extraction column: figures from the documents the
caller can see, nothing from another department."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from docintel.synthetic.scenarios import Scenario
from tests.conftest import auth_headers
from tests.integration.conftest import Env
from tests.integration.test_agent_analysis import processed, worker

pytestmark = pytest.mark.integration


async def summary(env: Env, user: Any, days: int = 30) -> dict[str, Any]:
    response = await env.client.get(
        "/api/v1/dashboard/summary", params={"days": days}, headers=auth_headers(user)
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_the_dashboard_summarizes_the_departments_documents(env: Env, tmp_path: Path) -> None:
    clean = await processed(env, tmp_path, Scenario.CLEAN_MATCH, 22)
    mismatch = await processed(env, tmp_path, Scenario.UNIT_PRICE_MISMATCH, 24)
    started = await env.client.post(
        "/api/v1/workflows",
        json={"workflow_type": "INVOICE_PROCESSING", "document_id": clean["INV"]},
        headers=auth_headers(env.analyst),
    )
    assert started.status_code == 202, started.text
    await worker(env).run_until_idle()
    uploaded = len(clean) + len(mismatch)

    figures = await summary(env, env.analyst)
    documents = figures["documents"]
    assert documents["total"] == uploaded == documents["uploaded_in_period"]
    assert documents["by_type"]["INVOICE"] == 2
    assert documents["by_status"].get("REVIEW_REQUIRED", 0) >= 1
    processing = figures["processing"]
    assert processing["processed_in_period"] == uploaded
    assert processing["average_seconds"] is not None
    assert processing["p95_seconds"] >= processing["average_seconds"]
    assert processing["failed_in_period"] == 0
    assert figures["review_queue"]["open"] >= 1
    assert figures["discrepancies"]["documents_failing"] >= 1
    assert "INV_PO_UNIT_PRICE" in {r["rule_code"] for r in figures["discrepancies"]["by_rule"]}
    assert figures["workflows"]["awaiting_approval"] == 1
    assert figures["workflows"]["by_status"]["AWAITING_APPROVAL"] == 1
    # The workflow's investigation was requested by the analyst: theirs to count.
    assert figures["investigations"]["scope"] == "mine"
    assert figures["investigations"]["by_recommendation"]["APPROVE_FOR_PAYMENT"] == 1
    trend = figures["confidence"]
    assert len(trend) == 30
    assert trend[-1]["day"] == figures["until"][:10]
    assert sum(point["processed"] for point in trend) == uploaded
    today = trend[-1]
    assert today["extraction_confidence"] is not None
    assert 0 < today["extraction_confidence"] <= 1
    assert today["auto_accepted"] <= today["processed"]
    actions = [item["action"] for item in figures["activity"]]
    assert "workflow.started" in actions
    assert "document.processing.completed" in actions
    named = [item for item in figures["activity"] if item["document_id"]]
    assert named
    assert all(item["document_name"] for item in named)
    assert "ip_address" not in str(figures)

    # Viewers of the department see the same documents; investigations are personal.
    viewer = await summary(env, env.viewer)
    assert viewer["documents"] == documents
    assert viewer["investigations"]["in_period"] == 0
    # Another department sees none of it.
    outsider = await summary(env, env.outsider)
    assert outsider["documents"]["total"] == 0
    assert outsider["review_queue"]["open"] == 0
    assert outsider["discrepancies"]["documents_failing"] == 0
    assert outsider["workflows"]["awaiting_approval"] == 0
    assert outsider["activity"] == []
    assert sum(point["processed"] for point in outsider["confidence"]) == 0
    # Administrators see every department (including this one).
    admin = await summary(env, env.admin, days=7)
    assert admin["investigations"]["scope"] == "all"
    assert admin["documents"]["total"] >= uploaded
    assert len(admin["confidence"]) == 7

    # The inbox shows each processed document's extraction confidence and routing.
    inbox = await env.client.get(
        "/api/v1/documents", params={"limit": 50}, headers=auth_headers(env.analyst)
    )
    items = inbox.json()["items"]
    assert len(items) == uploaded
    for item in items:
        assert item["extraction"] is not None, item["display_filename"]
        assert 0 <= float(item["extraction"]["overall_confidence"]) <= 1
        assert item["extraction"]["review_level"] in ("AUTO", "ANALYST_REVIEW", "MANDATORY_REVIEW")


async def test_the_period_is_bounded(env: Env) -> None:
    for days in (0, 91):
        response = await env.client.get(
            "/api/v1/dashboard/summary", params={"days": days}, headers=auth_headers(env.viewer)
        )
        assert response.status_code == 422
    anonymous = await env.client.get("/api/v1/dashboard/summary")
    assert anonymous.status_code == 401
