"""Phase 4 end to end: upload -> worker (structured extraction) -> extraction, evidence,
correction and vendor APIs; LLM call accounting with the daily budget."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from docintel.ai.base import LLMRequest, LLMResponse, LLMUsage, ModelTier, StructuredLLMResponse
from docintel.db.models import AuditLog, LLMCall, Role, User
from docintel.processing.services import build_processing_services
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.scenarios import Scenario
from tests.conftest import auth_headers
from tests.integration.conftest import Env

pytestmark = pytest.mark.integration


def _synthetic(
    tmp_path: Path, scenario: Scenario, doc_suffix: str, seed: int = 21
) -> tuple[bytes, dict[str, Any]]:
    dataset = tmp_path / f"dataset-{scenario.value}"
    manifest = generate_dataset(dataset, seed=seed, scenarios=[scenario])
    entry = next(d for d in manifest["documents"] if d["doc_id"].endswith(doc_suffix))
    truth = json.loads((dataset / entry["ground_truth"]).read_text())
    return (dataset / entry["file"]).read_bytes(), truth


async def _extraction(env: Env, document_id: str, user: Any = None) -> dict[str, Any]:
    response = await env.client.get(
        f"/api/v1/documents/{document_id}/extraction", headers=auth_headers(user or env.analyst)
    )
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


def _field(extraction: dict[str, Any], path: str) -> dict[str, Any]:
    return next(item for item in extraction["fields"] if item["field_path"] == path)


# ------------------------------------------------------------------------------ happy path
async def test_invoice_fields_are_extracted_with_evidence_and_vendor(
    env: Env, tmp_path: Path
) -> None:
    order, _ = _synthetic(tmp_path, Scenario.CLEAN_MATCH, "-PO")
    content, truth = _synthetic(tmp_path, Scenario.CLEAN_MATCH, "-INV")
    await env.upload(order, "order.pdf")  # the invoice is matched with its purchase order
    document_id = await env.upload(content, "invoice.pdf")
    assert await env.worker().run_until_idle() == 2

    detail = await env.detail(document_id)
    assert detail["status"] == "COMPLETED", detail["review_reasons"]
    assert detail["vendor"]["canonical_name"] == truth["fields"]["vendor_name_canonical"]

    extraction = await _extraction(env, document_id, env.viewer)
    assert (extraction["schema_name"], extraction["schema_version"]) == ("invoice", 1)
    assert extraction["status"] == "SUCCEEDED"
    assert extraction["method"] == "LOCAL"
    assert extraction["review_level"] == "AUTO"
    assert extraction["vendor"]["canonical_name"] == truth["fields"]["vendor_name_canonical"]
    assert extraction["signals"]["llm"]["reason"] == "no LLM provider configured"
    assert all(check["status"] == "PASS" for check in extraction["checks"])

    number = _field(extraction, "invoice_number")
    assert number["original_value"] == truth["fields"]["number"]
    assert number["evidence_status"] == "VERIFIED"
    assert number["page_number"] == 1
    assert number["source_text"].endswith(truth["fields"]["number"])
    assert len(number["bbox"]) == 4
    total = _field(extraction, "total")
    assert Decimal(total["normalized_value"]["value"]) == Decimal(truth["fields"]["total"])
    assert total["confidence_signals"]["consistency"] is True
    items = [
        f
        for f in extraction["fields"]
        if f["group_name"] == "line_items" and f["field_name"] == "sku"
    ]
    assert [f["original_value"] for f in items] == [i["sku"] for i in truth["line_items"]]

    evidence = await env.client.get(
        f"/api/v1/documents/{document_id}/evidence",
        params={"field_path": "total"},
        headers=auth_headers(env.viewer),
    )
    assert evidence.status_code == 200
    (only,) = evidence.json()
    assert only["field_path"] == "total"
    assert only["evidence_status"] == "VERIFIED"
    assert only["corrected"] is False

    by_vendor = await env.client.get(
        "/api/v1/documents",
        params={"vendor_id": detail["vendor"]["id"]},
        headers=auth_headers(env.analyst),
    )
    assert document_id in [item["id"] for item in by_vendor.json()["items"]]


async def test_extraction_endpoints_respect_scope_and_state(env: Env, tmp_path: Path) -> None:
    content, _ = _synthetic(tmp_path, Scenario.CLEAN_MATCH, "-PO")
    document_id = await env.upload(content, "po.pdf")
    url = f"/api/v1/documents/{document_id}/extraction"
    # Not processed yet: no extraction.
    assert (await env.client.get(url, headers=auth_headers(env.analyst))).status_code == 404
    await env.worker().run_until_idle()
    assert (await env.client.get(url, headers=auth_headers(env.outsider))).status_code == 404
    assert (
        await env.client.get(
            f"/api/v1/documents/{document_id}/evidence", headers=auth_headers(env.outsider)
        )
    ).status_code == 404
    unknown = f"/api/v1/documents/{uuid.uuid4()}/extraction"
    assert (await env.client.get(unknown, headers=auth_headers(env.analyst))).status_code == 404


# ------------------------------------------------------------------------------ corrections
async def test_reviewer_corrects_a_wrong_total_and_the_document_leaves_review(
    env: Env, tmp_path: Path
) -> None:
    order, _ = _synthetic(tmp_path, Scenario.TOTAL_ARITHMETIC_ERROR, "-PO")
    content, _ = _synthetic(tmp_path, Scenario.TOTAL_ARITHMETIC_ERROR, "-INV")
    await env.upload(order, "order.pdf")
    document_id = await env.upload(content, "invoice.pdf")
    await env.worker().run_until_idle()
    detail = await env.detail(document_id)
    assert detail["status"] == "REVIEW_REQUIRED"
    # The extraction flags the inconsistency, and so does the arithmetic rule.
    assert detail["review_reasons"] == ["EXTRACTION_INCONSISTENT", "RULE_VIOLATION"]

    extraction = await _extraction(env, document_id)
    assert extraction["review_level"] == "MANDATORY_REVIEW"
    failed = [c for c in extraction["checks"] if c["status"] == "FAIL"]
    assert [c["code"] for c in failed] == ["TOTAL_ARITHMETIC"]
    total = _field(extraction, "total")
    url = f"/api/v1/documents/{document_id}/extraction/fields/{total['id']}"
    correct = failed[0]["expected"]  # subtotal + tax, as the reviewer verifies

    body = {"value": correct, "note": "vendor confirmed the total by phone"}
    assert (
        await env.client.patch(url, json=body, headers=auth_headers(env.viewer))
    ).status_code == 403
    assert (
        await env.client.patch(url, json=body, headers=auth_headers(env.outsider))
    ).status_code == 404
    invalid = await env.client.patch(
        url, json={"value": "about a thousand"}, headers=auth_headers(env.reviewer)
    )
    assert invalid.status_code == 422
    assert "not a valid money value" in invalid.json()["detail"]
    missing = f"/api/v1/documents/{document_id}/extraction/fields/{uuid.uuid4()}"
    assert (
        await env.client.patch(missing, json=body, headers=auth_headers(env.reviewer))
    ).status_code == 404

    response = await env.client.patch(url, json=body, headers=auth_headers(env.reviewer))
    assert response.status_code == 200, response.text
    corrected = response.json()
    assert corrected["corrected_value"] == correct
    assert corrected["original_value"] == _field(extraction, "total")["original_value"]
    assert corrected["corrected_by"]["id"] == str(env.reviewer.id)
    assert corrected["confidence"] == "1.0000"

    detail = await env.detail(document_id)
    assert detail["status"] == "COMPLETED"
    assert detail["review_reasons"] == []
    extraction = await _extraction(env, document_id)
    assert extraction["review_level"] == "AUTO"
    assert all(check["status"] == "PASS" for check in extraction["checks"])

    async with env.maker() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.entity_id == document_id,
                AuditLog.action == "document.extraction.field_corrected",
            )
        )
    assert audit is not None
    assert audit.details["field_path"] == "total"
    assert audit.details["review_level"] == {"from": "MANDATORY_REVIEW", "to": "AUTO"}
    assert correct not in json.dumps(audit.details)  # values stay out of the audit log

    # Reprocessing the same version keeps the reviewer's correction.
    reprocess = await env.client.post(
        f"/api/v1/documents/{document_id}/process", headers=auth_headers(env.analyst)
    )
    assert reprocess.status_code == 202
    await env.worker().run_until_idle()
    again = await _extraction(env, document_id)
    assert again["id"] != extraction["id"]
    total = _field(again, "total")
    assert total["corrected_value"] == correct
    assert total["corrected_by"]["id"] == str(env.reviewer.id)
    assert (await env.detail(document_id))["status"] == "COMPLETED"


# ------------------------------------------------------------------------------ vendors API
async def test_vendor_master_api(env: Env) -> None:
    listing = await env.client.get(
        "/api/v1/vendors", params={"q": "kestrel ind supply"}, headers=auth_headers(env.viewer)
    )
    assert listing.status_code == 200
    names = [item["canonical_name"] for item in listing.json()["items"]]
    assert "Kestrel Industrial Supply Inc." in names
    by_tax_id = await env.client.get(
        "/api/v1/vendors", params={"q": "US-47-2917735"}, headers=auth_headers(env.viewer)
    )
    assert [v["canonical_name"] for v in by_tax_id.json()["items"]] == [
        "Kestrel Industrial Supply Inc."
    ]

    name = f"Northwind Traders {uuid.uuid4().hex[:6]}"
    body = {"canonical_name": name, "aliases": ["NWT"], "tax_id": "NW-1", "default_currency": "EUR"}
    # Managing vendors is a manager/admin task.
    assert (
        await env.client.post("/api/v1/vendors", json=body, headers=auth_headers(env.analyst))
    ).status_code == 403
    async with env.maker() as session, session.begin():
        boss = User(
            id=uuid.uuid4(),
            email=f"manager-{uuid.uuid4().hex[:8]}@example.test",
            full_name="Test Manager",
            password_hash="not-used",
            role=Role.MANAGER,
            department_id=env.analyst.department_id,
        )
        session.add(boss)
    created = await env.client.post("/api/v1/vendors", json=body, headers=auth_headers(boss))
    assert created.status_code == 201, created.text
    vendor = created.json()
    assert vendor["aliases"] == ["NWT"]
    duplicate = await env.client.post("/api/v1/vendors", json=body, headers=auth_headers(boss))
    assert duplicate.status_code == 409
    bad = await env.client.post(
        "/api/v1/vendors",
        json={**body, "canonical_name": "x" * 10, "default_currency": "euro"},
        headers=auth_headers(boss),
    )
    assert bad.status_code == 422
    updated = await env.client.patch(
        f"/api/v1/vendors/{vendor['id']}",
        json={"aliases": ["NWT", "Northwind"], "is_active": False},
        headers=auth_headers(boss),
    )
    assert updated.status_code == 200
    assert updated.json()["aliases"] == ["NWT", "Northwind"]
    assert updated.json()["is_active"] is False
    assert updated.json()["tax_id"] == "NW-1"  # untouched fields keep their value


# ------------------------------------------------------------------------------ LLM accounting
class ExtractingLLM:
    """Classifies nothing (never asked: the local model is confident) and returns the printed
    invoice number for extraction requests."""

    def __init__(self, invoice_number: str) -> None:
        self.invoice_number = invoice_number
        self.requests: list[LLMRequest] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def supports_images(self) -> bool:
        return False

    @property
    def local(self) -> bool:
        return False

    def model_for(self, tier: ModelTier) -> str:
        return "fake-model"

    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredLLMResponse[T]:
        self.requests.append(request)
        data = schema.model_validate(
            {
                "invoice_number": {
                    "value": self.invoice_number,
                    "page": 1,
                    "source_text": f"Invoice No. {self.invoice_number}",
                }
            }
        )
        usage = LLMUsage("fake", "fake-model", 7.0, input_tokens=900, output_tokens=60)
        return StructuredLLMResponse(data=data, raw_text="{}", usage=usage)

    async def aclose(self) -> None:
        return None


async def test_llm_calls_are_accounted_and_the_daily_budget_holds(env: Env, tmp_path: Path) -> None:
    content, truth = _synthetic(tmp_path, Scenario.CLEAN_MATCH, "-INV", seed=33)
    second, _ = _synthetic(tmp_path / "b", Scenario.CLEAN_MATCH, "-PO", seed=34)
    async with env.maker() as session:
        before = len(list(await session.scalars(select(LLMCall.id))))
    settings = env.settings.model_copy(
        update={
            "extraction_llm_mode": "always",
            "llm_daily_request_budget": before + 1,
            "llm_pricing": {},
        }
    )
    llm = ExtractingLLM(truth["fields"]["number"])
    services = build_processing_services(settings, sessionmaker=env.maker, llm=llm)

    first_id = await env.upload(content, "invoice.pdf")
    await env.worker(services).run_until_idle()
    extraction = await _extraction(env, first_id)
    assert extraction["method"] == "COMBINED"
    assert extraction["prompt_version"] == "extract-v1"
    number = _field(extraction, "invoice_number")
    assert (number["origin"], number["confidence_signals"]["agreement"]) == ("BOTH", True)

    async with env.maker() as session:
        call = await session.scalar(
            select(LLMCall).where(LLMCall.document_id == uuid.UUID(first_id))
        )
    assert call is not None
    assert (call.provider, call.model, call.purpose) == ("fake", "fake-model", "extraction.invoice")
    assert (call.input_tokens, call.output_tokens, call.status.value) == (900, 60, "SUCCEEDED")
    assert call.estimated_cost_usd is None  # no price configured

    # The budget (one call today) is spent: the next document is extracted locally only.
    second_id = await env.upload(second, "po.pdf")
    await env.worker(services).run_until_idle()
    assert len(llm.requests) == 1
    extraction = await _extraction(env, second_id)
    assert extraction["method"] == "LOCAL"
    assert "ProviderBudgetExceededError" in extraction["signals"]["llm"]["errors"][0]
