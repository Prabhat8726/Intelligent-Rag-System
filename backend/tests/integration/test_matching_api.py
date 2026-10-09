"""Phase 5 through the API and the worker: matching in any arrival order, the review queue,
duplicates, rule administration, manual comparisons and document versions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from docintel.db.models import AuditLog
from docintel.synthetic.contracts import generate_contract_versions
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.scenarios import Scenario
from tests.conftest import auth_headers
from tests.integration.conftest import Env

pytestmark = pytest.mark.integration


def bundle(
    tmp_path: Path, scenario: Scenario, seed: int = 21
) -> dict[str, tuple[bytes, dict[str, Any]]]:
    """The scenario's documents by suffix ("PO", "DN", "INV", "INV2"): (file bytes, truth)."""
    dataset = tmp_path / f"bundle-{scenario.value}-{seed}"
    manifest = generate_dataset(dataset, seed=seed, scenarios=[scenario])
    found: dict[str, tuple[bytes, dict[str, Any]]] = {}
    for entry in manifest["documents"]:
        truth = json.loads((dataset / entry["ground_truth"]).read_text())
        found[entry["doc_id"].split("-", 1)[1]] = ((dataset / entry["file"]).read_bytes(), truth)
    return found


async def findings(env: Env, document_id: str, user: Any = None) -> dict[str, Any]:
    response = await env.client.get(
        f"/api/v1/documents/{document_id}/findings", headers=auth_headers(user or env.analyst)
    )
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


def outcomes(data: dict[str, Any]) -> dict[str, str]:
    return {result["rule_code"]: result["outcome"] for result in data["rule_results"]}


def needing_review(data: dict[str, Any]) -> set[str]:
    return {
        code for code, outcome in outcomes(data).items() if outcome in {"FAIL", "WARN", "ERROR"}
    }


# ------------------------------------------------------------------------------ arrival order
async def test_an_invoice_is_matched_when_its_order_arrives_later(env: Env, tmp_path: Path) -> None:
    docs = bundle(tmp_path, Scenario.UNIT_PRICE_MISMATCH)
    invoice_id = await env.upload(docs["INV"][0], "invoice.pdf")
    assert await env.worker().run_until_idle() == 1
    first = await findings(env, invoice_id)
    assert outcomes(first)["INV_MISSING_PO"] == "WARN"  # its order is not on file yet
    assert first["comparisons"] == []
    task_id = first["open_task"]["id"]
    assert (await env.detail(invoice_id))["review"]["id"] == task_id

    order_id = await env.upload(docs["PO"][0], "order.pdf")
    note_id = await env.upload(docs["DN"][0], "delivery.pdf")
    assert await env.worker().run_until_idle() == 2

    later = await findings(env, invoice_id)
    assert needing_review(later) == {"INV_PO_UNIT_PRICE"}
    assert later["open_task"]["id"] == task_id  # the same task, now about the price
    assert [reason["code"] for reason in later["open_task"]["reasons"]] == ["INV_PO_UNIT_PRICE"]
    (summary,) = later["comparisons"]
    assert summary["comparison_type"] == "INVOICE_PO_DELIVERY"
    assert {d["document_id"]: d["role"] for d in summary["documents"]} == {
        invoice_id: "INVOICE",
        order_id: "PURCHASE_ORDER",
        note_id: "DELIVERY_NOTE",
    }
    assert summary["summary"]["MISMATCH"] == 1

    response = await env.client.get(
        f"/api/v1/comparisons/{summary['id']}", headers=auth_headers(env.viewer)
    )
    assert response.status_code == 200
    comparison = response.json()
    (mismatch,) = [item for item in comparison["items"] if item["status"] == "MISMATCH"]
    defect = docs["INV"][1]["defects"][0]
    assert mismatch["check_name"] == "unit_price"
    assert mismatch["line_key"] == defect["sku"]
    assert (mismatch["left_value"], mismatch["right_value"]) == (
        str(float(defect["actual"])).rstrip("0").rstrip("."),
        str(float(defect["expected"])).rstrip("0").rstrip("."),
    )
    (left,), (right,) = mismatch["left"], mismatch["right"]
    assert (left["document_id"], right["document_id"]) == (invoice_id, order_id)
    assert left["field_id"]
    assert right["field_id"]
    assert left["page"] == 1
    assert left["source_text"]
    assert comparison["settings"]["price_abs"] == "0.01"

    # The order and the delivery note are clean, and see the comparison they take part in.
    order = await findings(env, order_id)
    assert needing_review(order) == set()
    # It takes part in the invoice's comparison and in the delivery note's.
    assert {(c["id"] == summary["id"], c["comparison_type"]) for c in order["comparisons"]} == {
        (True, "INVOICE_PO_DELIVERY"),
        (False, "PO_DELIVERY"),
    }
    assert (await env.detail(order_id))["status"] == "COMPLETED"
    assert (await env.detail(note_id))["status"] == "COMPLETED"


# ------------------------------------------------------------------------------ review queue
async def test_review_lifecycle(env: Env, tmp_path: Path) -> None:
    docs = bundle(tmp_path, Scenario.QUANTITY_MISMATCH)
    for suffix in ("PO", "DN", "INV"):
        await env.upload(docs[suffix][0], f"{suffix}.pdf")
    await env.worker().run_until_idle()
    invoices = await env.client.get(
        "/api/v1/review-tasks", params={"task_type": "DISCREPANCY_REVIEW"},
        headers=auth_headers(env.reviewer),
    )  # fmt: skip
    assert invoices.status_code == 200
    (task,) = [
        t for t in invoices.json()["items"] if t["document"]["display_filename"] == "INV.pdf"
    ]
    assert task["priority"] == "HIGH"
    assert task["due_at"] is not None
    assert task["overdue"] is False
    assert {reason["code"] for reason in task["reasons"]} == {
        "INV_PO_QUANTITY",
        "INV_DELIVERED_QUANTITY",
    }
    url = f"/api/v1/review-tasks/{task['id']}"

    viewer = await env.client.get("/api/v1/review-tasks", headers=auth_headers(env.viewer))
    assert viewer.status_code == 403
    outsider = await env.client.get(url, headers=auth_headers(env.outsider))
    assert outsider.status_code == 404
    others = await env.client.get("/api/v1/review-tasks", headers=auth_headers(env.outsider))
    assert task["id"] not in {item["id"] for item in others.json()["items"]}

    claimed = await env.client.post(f"{url}/claim", headers=auth_headers(env.reviewer))
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "IN_PROGRESS"
    assert claimed.json()["assigned_to"]["id"] == str(env.reviewer.id)
    mine = await env.client.get(
        "/api/v1/review-tasks", params={"assigned": "me"}, headers=auth_headers(env.reviewer)
    )
    assert [item["id"] for item in mine.json()["items"]] == [task["id"]]
    taken = await env.client.post(f"{url}/claim", headers=auth_headers(env.analyst))
    assert taken.status_code == 409  # claimed by someone else
    reject = await env.client.post(
        f"{url}/resolve", json={"resolution": "REJECTED"}, headers=auth_headers(env.reviewer)
    )
    assert reject.status_code == 422  # a rejection needs a note
    cleared = await env.client.post(
        f"{url}/resolve", json={"resolution": "CLEARED"}, headers=auth_headers(env.reviewer)
    )
    assert cleared.status_code == 422

    resolved = await env.client.post(
        f"{url}/resolve",
        json={"resolution": "APPROVED", "note": "vendor delivered the rest; PO amended"},
        headers=auth_headers(env.reviewer),
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert (body["status"], body["resolution"]) == ("RESOLVED", "APPROVED")
    assert body["resolved_by"]["id"] == str(env.reviewer.id)
    document_id = body["document_id"]
    assert (await env.detail(document_id))["status"] == "COMPLETED"
    again = await env.client.post(
        f"{url}/resolve", json={"resolution": "APPROVED"}, headers=auth_headers(env.reviewer)
    )
    assert again.status_code == 409

    # Re-running the rules finds the same discrepancies: they were resolved, no new task.
    evaluated = await env.client.post(
        "/api/v1/rules/evaluate", json={"document_ids": [document_id]},
        headers=auth_headers(env.analyst),
    )  # fmt: skip
    assert evaluated.status_code == 200, evaluated.text
    assert (await env.detail(document_id))["status"] == "COMPLETED"
    data = await findings(env, document_id)
    assert data["open_task"] is None
    assert [t["resolution"] for t in data["review_history"]] == ["APPROVED"]

    async with env.maker() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.entity_id == task["id"], AuditLog.action == "review_task.resolved"
            )
        )
    assert audit is not None
    assert audit.details["resolution"] == "APPROVED"
    assert "amended" not in json.dumps(audit.details)  # notes stay out of the audit log


# ------------------------------------------------------------------------------ duplicates
async def test_a_manager_takes_over_a_claimed_task_and_a_corrected_version_supersedes_it(
    env: Env, tmp_path: Path
) -> None:
    docs = bundle(tmp_path, Scenario.UNIT_PRICE_MISMATCH)
    ids = {
        suffix: await env.upload(docs[suffix][0], f"{suffix}.pdf") for suffix in ("PO", "DN", "INV")
    }
    await env.worker().run_until_idle()
    first = (await env.detail(ids["INV"]))["review"]
    url = f"/api/v1/review-tasks/{first['id']}"
    claim = await env.client.post(f"{url}/claim", headers=auth_headers(env.reviewer))
    assert claim.status_code == 200
    release = await env.client.post(f"{url}/release", headers=auth_headers(env.analyst))
    assert release.status_code == 409  # someone else's claim
    taken = await env.client.post(f"{url}/claim", headers=auth_headers(env.manager))
    assert taken.status_code == 200
    assert taken.json()["assigned_to"]["id"] == str(env.manager.id)

    # The supplier sends a corrected invoice as version 2 (same seed: same order, right prices).
    corrected = bundle(tmp_path, Scenario.CLEAN_MATCH)["INV"][0]
    uploaded = await env.client.post(
        f"/api/v1/documents/{ids['INV']}/versions",
        files={"file": ("INV-v2.pdf", corrected, "application/pdf")},
        headers=auth_headers(env.analyst),
    )
    assert uploaded.status_code == 201, uploaded.text
    await env.worker().run_until_idle()
    data = await findings(env, ids["INV"])
    (old,) = data["review_history"]
    assert (old["id"], old["status"], old["resolution"]) == (first["id"], "CANCELLED", None)
    assert old["resolution_note"] == "Superseded by a new version of the document."
    assert data["open_task"] is None
    assert outcomes(data)["INV_PO_UNIT_PRICE"] == "PASS"
    assert (await env.detail(ids["INV"]))["status"] == "COMPLETED"


async def test_a_resent_invoice_is_held_as_a_duplicate_until_the_original_goes(
    env: Env, tmp_path: Path
) -> None:
    docs = bundle(tmp_path, Scenario.DUPLICATE_INVOICE)
    ids = {
        suffix: await env.upload(docs[suffix][0], f"{suffix}.pdf") for suffix in ("PO", "DN", "INV")
    }
    await env.worker().run_until_idle()
    ids["INV2"] = await env.upload(docs["INV2"][0], "INV2.pdf")
    await env.worker().run_until_idle()

    copy = await env.detail(ids["INV2"])
    assert copy["status"] == "REVIEW_REQUIRED"
    assert copy["review_reasons"] == ["DUPLICATE_SUSPECTED"]
    assert copy["duplicate_of_id"] == ids["INV"]
    assert copy["duplicate_reason"] == "SAME_VENDOR_AND_NUMBER"
    assert copy["review"]["task_type"] == "DUPLICATE_REVIEW"
    data = await findings(env, ids["INV2"])
    assert [(d["document_id"], d["direction"]) for d in data["duplicates"]] == [
        (ids["INV"], "original")
    ]
    original = await findings(env, ids["INV"])
    assert [(d["document_id"], d["direction"]) for d in original["duplicates"]] == [
        (ids["INV2"], "copy")
    ]
    assert (await env.detail(ids["INV"]))["status"] == "COMPLETED"

    deleted = await env.client.delete(
        f"/api/v1/documents/{ids['INV']}", headers=auth_headers(env.manager)
    )
    assert deleted.status_code == 204
    copy = await env.detail(ids["INV2"])
    assert (copy["status"], copy["review_reasons"], copy["duplicate_of_id"]) == (
        "COMPLETED",
        [],
        None,
    )
    history = (await findings(env, ids["INV2"]))["review_history"]
    assert [t["resolution"] for t in history] == ["CLEARED"]
    assert history[0]["resolved_by"]["id"] == str(env.manager.id)


# ------------------------------------------------------------------------------ rules
async def test_rules_are_read_by_all_changed_by_admins_and_re_evaluated(
    env: Env, tmp_path: Path
) -> None:
    listing = await env.client.get("/api/v1/rules", headers=auth_headers(env.viewer))
    assert listing.status_code == 200
    rules = {rule["code"]: rule for rule in listing.json()}
    assert len(rules) == 19
    price = rules["INV_PO_UNIT_PRICE"]
    assert price["params"] == {"tolerance_pct": "0", "tolerance_abs": "0.01"}
    assert "tolerance_pct" in price["params_schema"]["properties"]

    url = "/api/v1/rules/INV_PO_UNIT_PRICE"
    body = {"params": {"tolerance_pct": "0.15", "tolerance_abs": "0.01"}, "note": "pilot"}
    assert (
        await env.client.patch(url, json=body, headers=auth_headers(env.manager))
    ).status_code == 403
    invalid = await env.client.patch(
        url, json={"params": {"tolerance_pct": "2"}}, headers=auth_headers(env.admin)
    )
    assert invalid.status_code == 422
    assert "tolerance_pct" in invalid.json()["detail"]

    docs = bundle(tmp_path, Scenario.UNIT_PRICE_MISMATCH, seed=33)
    for suffix in ("PO", "DN"):
        await env.upload(docs[suffix][0], f"{suffix}.pdf")
    invoice_id = await env.upload(docs["INV"][0], "INV.pdf")
    await env.worker().run_until_idle()
    assert needing_review(await findings(env, invoice_id)) == {"INV_PO_UNIT_PRICE"}

    try:
        changed = await env.client.patch(url, json=body, headers=auth_headers(env.admin))
        assert changed.status_code == 200, changed.text
        assert changed.json()["version"] == price["version"] + 1
        assert changed.json()["updated_by"]["id"] == str(env.admin.id)
        evaluate = await env.client.post(
            "/api/v1/rules/evaluate", json={"document_ids": [invoice_id]},
            headers=auth_headers(env.analyst),
        )  # fmt: skip
        assert evaluate.status_code == 200
        (result,) = evaluate.json()
        assert result["evaluated"] is True
        assert {r["rule_code"]: r["outcome"] for r in result["results"]}[
            "INV_PO_UNIT_PRICE"
        ] == "PASS"
        assert (await env.detail(invoice_id))["status"] == "COMPLETED"  # within the new tolerance
    finally:
        restored = await env.client.patch(
            url, json={"params": price["params"]}, headers=auth_headers(env.admin)
        )
        assert restored.status_code == 200
    await env.client.post(
        "/api/v1/rules/evaluate", json={"document_ids": [invoice_id]},
        headers=auth_headers(env.analyst),
    )  # fmt: skip
    data = await findings(env, invoice_id)
    assert needing_review(data) == {"INV_PO_UNIT_PRICE"}
    assert data["open_task"] is not None  # the earlier task was cleared by the rule, not a person
    async with env.maker() as session:
        audit = await session.scalar(
            select(AuditLog)
            .where(AuditLog.action == "rule.updated")
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    assert audit is not None
    assert audit.details["before"]["params"]["tolerance_pct"] == "0.15"
    assert audit.details["after"]["params"]["tolerance_pct"] == "0"


# ------------------------------------------------------------------------------ manual comparisons
async def test_a_user_compares_documents_of_their_choice(env: Env, tmp_path: Path) -> None:
    clean = bundle(tmp_path, Scenario.CLEAN_MATCH, seed=41)
    other = bundle(tmp_path, Scenario.CLEAN_MATCH, seed=42)
    invoice_id = await env.upload(clean["INV"][0], "INV.pdf")
    order_id = await env.upload(other["PO"][0], "unrelated-PO.pdf")
    await env.worker().run_until_idle()

    body = {
        "documents": [
            {"document_id": invoice_id, "role": "INVOICE"},
            {"document_id": order_id, "role": "PURCHASE_ORDER"},
        ]
    }
    denied = await env.client.post(
        "/api/v1/comparisons", json=body, headers=auth_headers(env.viewer)
    )
    assert denied.status_code == 403
    wrong = await env.client.post(
        "/api/v1/comparisons",
        json={"documents": [{"document_id": invoice_id, "role": "PURCHASE_ORDER"},
                            {"document_id": order_id, "role": "INVOICE"}]},
        headers=auth_headers(env.analyst),
    )  # fmt: skip
    assert wrong.status_code == 422
    created = await env.client.post(
        "/api/v1/comparisons", json=body, headers=auth_headers(env.analyst)
    )
    assert created.status_code == 201, created.text
    comparison = created.json()
    assert (comparison["origin"], comparison["comparison_type"]) == ("MANUAL", "INVOICE_PO")
    assert comparison["requested_by"]["id"] == str(env.analyst.id)
    statuses = {item["check_name"]: item["status"] for item in comparison["items"]}
    assert statuses["po_reference"] == "MISMATCH"  # the invoice cites a different order
    listed = await env.client.get(
        "/api/v1/comparisons", params={"document_id": invoice_id, "origin": "MANUAL"},
        headers=auth_headers(env.viewer),
    )  # fmt: skip
    assert [item["id"] for item in listed.json()["items"]] == [comparison["id"]]
    hidden = await env.client.get(
        f"/api/v1/comparisons/{comparison['id']}", headers=auth_headers(env.outsider)
    )
    assert hidden.status_code == 404
    # A manual comparison does not change the documents' review state.
    assert (await env.detail(order_id))["status"] == "COMPLETED"


# ------------------------------------------------------------------------------ versions
async def test_contract_versions_are_compared_clause_by_clause(env: Env, tmp_path: Path) -> None:
    manifest = generate_contract_versions(tmp_path / "contracts", seed=5, families=1)
    (family,) = manifest["contracts"]
    files = [(tmp_path / "contracts" / name).read_bytes() for name in family["files"]]
    document_id = await env.upload(files[0], "agreement.pdf")
    await env.worker().run_until_idle()

    url = f"/api/v1/documents/{document_id}/versions"

    def files_for(content: bytes) -> dict[str, tuple[str, bytes, str]]:
        return {"file": ("agreement-v2.pdf", content, "application/pdf")}

    assert (
        await env.client.post(url, files=files_for(files[1]), headers=auth_headers(env.viewer))
    ).status_code == 403
    same = await env.client.post(url, files=files_for(files[0]), headers=auth_headers(env.analyst))
    assert same.status_code == 409
    added = await env.client.post(url, files=files_for(files[1]), headers=auth_headers(env.analyst))
    assert added.status_code == 201, added.text
    assert added.json()["current_version"]["version_number"] == 2
    assert added.json()["status"] == "PENDING"
    busy = await env.client.post(url, files=files_for(files[2]), headers=auth_headers(env.analyst))
    assert busy.status_code == 409  # wait until version 2 is processed
    unprocessed = await env.client.get(
        f"{url}/compare", params={"from": 1, "to": 2}, headers=auth_headers(env.viewer)
    )
    assert unprocessed.status_code == 409
    await env.worker().run_until_idle()

    versions = (await env.client.get(url, headers=auth_headers(env.viewer))).json()
    assert [(v["version_number"], v["is_current"], v["processed"]) for v in versions] == [
        (2, True, True),
        (1, False, True),
    ]
    compared = await env.client.get(
        f"{url}/compare", params={"from": 1, "to": 2}, headers=auth_headers(env.viewer)
    )
    assert compared.status_code == 200, compared.text
    clauses = [
        c for c in compared.json()["clauses"] if c["title"] not in ("Preamble", "Signatures")
    ]
    got = {
        change: sorted(c["title"] for c in clauses if c["change"] == change.upper())
        for change in ("added", "removed", "modified")
    }
    assert got == {key: sorted(value) for key, value in family["changes"][0].items()}
    modified = [c for c in compared.json()["clauses"] if c["change"] == "MODIFIED"]
    assert all(any(op["op"] != "equal" for op in c["operations"]) for c in modified)

    same_version = await env.client.get(
        f"{url}/compare", params={"from": 2, "to": 2}, headers=auth_headers(env.viewer)
    )
    assert same_version.status_code == 422
    outsider = await env.client.get(
        f"{url}/compare", params={"from": 1, "to": 2}, headers=auth_headers(env.outsider)
    )
    assert outsider.status_code == 404
