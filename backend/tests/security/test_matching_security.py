"""Matching never crosses department boundaries, and nothing leaks through its results."""

from __future__ import annotations

from pathlib import Path

import pytest

from docintel.synthetic.scenarios import Scenario
from tests.conftest import auth_headers
from tests.integration.conftest import Env
from tests.integration.test_matching_api import bundle, findings

pytestmark = pytest.mark.integration


async def _upload_as_admin(env: Env, content: bytes, name: str, department_id: str) -> str:
    response = await env.client.post(
        "/api/v1/documents",
        headers=auth_headers(env.admin),
        files={"file": (name, content, "application/pdf")},
        data={"sensitivity": "INTERNAL", "department_id": department_id},
    )
    assert response.status_code == 201, response.text
    document_id: str = response.json()["id"]
    return document_id


async def test_documents_are_never_matched_across_departments(env: Env, tmp_path: Path) -> None:
    docs = bundle(tmp_path, Scenario.DUPLICATE_INVOICE, seed=51)
    legal = str(env.outsider.department_id)
    # The order and a copy of the invoice belong to Legal; Finance has the invoice.
    legal_order = await _upload_as_admin(env, docs["PO"][0], "PO.pdf", legal)
    legal_copy = await _upload_as_admin(env, docs["INV"][0], "INV-legal.pdf", legal)
    finance_invoice = await env.upload(docs["INV2"][0], "INV.pdf")
    await env.worker().run_until_idle()

    data = await findings(env, finance_invoice)
    assert data["comparisons"] == []  # Legal's order is not "on file" for Finance
    outcomes = {result["rule_code"]: result["outcome"] for result in data["rule_results"]}
    assert outcomes["INV_MISSING_PO"] == "WARN"
    assert outcomes["INV_DUPLICATE"] == "PASS"  # Legal's earlier copy is invisible here
    assert data["duplicates"] == []
    detail = await env.detail(finance_invoice)
    assert detail["duplicate_of_id"] is None

    # Inside Legal the copy and the order do match.
    legal_findings = await findings(env, legal_copy, env.outsider)
    assert [c["comparison_type"] for c in legal_findings["comparisons"]] == ["INVOICE_PO"]
    assert {d["document_id"] for c in legal_findings["comparisons"] for d in c["documents"]} == {
        legal_copy,
        legal_order,
    }


async def test_phase5_reads_are_scoped(env: Env, tmp_path: Path) -> None:
    docs = bundle(tmp_path, Scenario.UNIT_PRICE_MISMATCH, seed=52)
    ids = {suffix: await env.upload(docs[suffix][0], f"{suffix}.pdf") for suffix in ("PO", "INV")}
    await env.worker().run_until_idle()
    data = await findings(env, ids["INV"])
    comparison_id = data["comparisons"][0]["id"]
    task_id = data["open_task"]["id"]

    outsider = auth_headers(env.outsider)
    get = env.client.get
    assert (
        await get(f"/api/v1/documents/{ids['INV']}/findings", headers=outsider)
    ).status_code == 404
    assert (
        await get(f"/api/v1/documents/{ids['INV']}/versions", headers=outsider)
    ).status_code == 404
    assert (await get(f"/api/v1/comparisons/{comparison_id}", headers=outsider)).status_code == 404
    listed = await get("/api/v1/comparisons", headers=outsider)
    assert comparison_id not in {item["id"] for item in listed.json()["items"]}
    assert (await get(f"/api/v1/review-tasks/{task_id}", headers=outsider)).status_code == 404
    claim = await env.client.post(f"/api/v1/review-tasks/{task_id}/claim", headers=outsider)
    assert claim.status_code == 404
    queue = await get("/api/v1/review-tasks", params={"state": "all"}, headers=outsider)
    assert task_id not in {item["id"] for item in queue.json()["items"]}
    manual = await env.client.post(
        "/api/v1/comparisons",
        json={"documents": [{"document_id": ids["INV"], "role": "INVOICE"},
                            {"document_id": ids["PO"], "role": "PURCHASE_ORDER"}]},
        headers=outsider,
    )  # fmt: skip
    assert manual.status_code == 404
    # Re-evaluation needs documents:process and visibility.
    evaluate = await env.client.post(
        "/api/v1/rules/evaluate", json={"document_ids": [ids["INV"]]}, headers=outsider
    )
    assert evaluate.status_code == 403
    admin_elsewhere = await env.client.post(
        "/api/v1/rules/evaluate", json={"document_ids": [ids["INV"]]},
        headers=auth_headers(env.admin),
    )  # fmt: skip
    assert admin_elsewhere.status_code == 200  # admins see every department
