"""Phase 3 end to end: upload -> worker (OCR, layout, tables, classification) -> API.

Uses committed data (the worker runs in its own transactions), a real Tesseract engine and the
real local classifier; the LLM and, where needed, the OCR engine are replaced by fakes.
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pytest
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import select

from docintel.ai.base import LLMRequest, LLMResponse, LLMUsage, ModelTier, StructuredLLMResponse
from docintel.db.models import (
    AuditLog,
    DocumentClassification,
    DocumentType,
)
from docintel.processing.ocr import OCRResult, OCRWord, Orientation
from docintel.processing.services import build_processing_services, load_corrections
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.scenarios import Scenario
from docintel.workers.runner import Worker
from tests.conftest import auth_headers
from tests.factories.files import (
    INVOICE_LINES,
    invoice_pdf_bytes,
    pdf_bytes,
    scanned_pdf_bytes,
    text_image,
)
from tests.integration.conftest import Env

pytestmark = pytest.mark.integration


class FakeLLM:
    """Answers classification requests; extraction requests get an empty (all-null) result."""

    def __init__(self, label: DocumentType, quote: str) -> None:
        self.label = label
        self.quote = quote
        self.purposes: list[str] = []

    @property
    def calls(self) -> int:
        return self.purposes.count("classification")

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
        return "fake-fast"

    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredLLMResponse[T]:
        self.purposes.append(request.purpose)
        if request.purpose != "classification":
            return StructuredLLMResponse(
                data=schema.model_validate({}),
                raw_text="{}",
                usage=LLMUsage(provider="fake", model="fake", latency_ms=5),
            )
        data = schema.model_validate({"document_type": self.label, "evidence_quote": self.quote})
        return StructuredLLMResponse(
            data=data,
            raw_text="{}",
            usage=LLMUsage(provider="fake", model="fake-fast", latency_ms=5),
        )

    async def aclose(self) -> None:
        return None


class LowConfidenceOCR:
    @property
    def name(self) -> str:
        return "low-confidence"

    async def recognize(self, image: Image.Image, *, dpi: int) -> OCRResult:
        words = [
            OCRWord(text, 100 + i * 200, 100, 150, 40, 31.0, (1, 1, 1))
            for i, text in enumerate(["Invoice", "INV-77", "Total", "Due", "71.55"])
        ]
        return OCRResult(words, image.width, image.height, 1.0)

    async def detect_orientation(self, image: Image.Image, *, dpi: int) -> Orientation | None:
        return None


# ------------------------------------------------------------------------------ happy paths
async def test_scanned_invoice_is_read_classified_and_previewed(env: Env) -> None:
    scan = scanned_pdf_bytes([text_image(INVOICE_LINES, dpi=200)], dpi=200)
    document_id = await env.upload(scan, "scan.pdf")
    assert await env.worker().run_until_idle() == 1

    detail = await env.detail(document_id)
    assert detail["status"] == "COMPLETED", detail["review_reasons"]
    assert detail["document_type"] == "INVOICE"
    classification = detail["classification"]
    assert classification["method"] == "LOCAL_MODEL"
    assert classification["is_current"] is True
    assert float(classification["confidence"]) >= 0.7
    assert classification["signals"]["local"][0]["label"] == "INVOICE"
    assert classification["model_version"].startswith("tfidf-lr-v1:")
    (page,) = detail["pages"]
    assert page["extraction_method"] == "OCR"
    assert float(page["ocr_confidence"]) > 80
    assert page["has_preview"] is True
    assert "preview_storage_key" not in page
    assert detail["sensitivity_assessment"] == {
        "findings": [],
        "type_minimum": None,
        "detected": None,
    }

    response = await env.client.get(
        f"/api/v1/documents/{document_id}/pages/1", headers=auth_headers(env.viewer)
    )
    assert response.status_code == 200
    page_detail = response.json()
    assert "INV-1001" in page_detail["text"]
    word = next(w for w in page_detail["words"] if w[0] == "INV-1001")
    assert 0 < word[1] < word[3] <= page_detail["width"]  # x0 < x1 within the page (pt)
    assert word[5] is not None  # OCR confidence
    assert page_detail["layout"]["blocks"]

    image = await env.client.get(
        f"/api/v1/documents/{document_id}/pages/1/image", headers=auth_headers(env.viewer)
    )
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert image.headers["cache-control"].startswith("private")
    with Image.open(io.BytesIO(image.content)) as preview:
        assert preview.width == 300


async def test_multi_page_line_item_table_is_stitched(env: Env, tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    manifest = generate_dataset(dataset, seed=5, scenarios=[Scenario.LONG_MULTIPAGE])
    entry = next(d for d in manifest["documents"] if d["document_type"] == "INVOICE")
    truth = json.loads((dataset / entry["ground_truth"]).read_text())
    document_id = await env.upload((dataset / entry["file"]).read_bytes(), "long-invoice.pdf")
    await env.worker().run_until_idle()

    response = await env.client.get(
        f"/api/v1/documents/{document_id}/tables", headers=auth_headers(env.analyst)
    )
    assert response.status_code == 200
    (table,) = response.json()
    assert (table["page_start"], table["page_end"]) == (1, 2)
    assert table["extraction_method"] == "NATIVE"
    assert table["header"][:3] == ["#", "Item", "Description"]
    assert table["row_count"] == len(truth["line_items"]) == len(table["rows"])
    assert [row["cells"][1] for row in table["rows"]] == [i["sku"] for i in truth["line_items"]]
    assert {row["page_number"] for row in table["rows"]} == {1, 2}
    detail = await env.detail(document_id)
    assert detail["document_type"] == "INVOICE"
    assert [page["extraction_method"] for page in detail["pages"]] == ["NATIVE", "NATIVE"]


# ------------------------------------------------------------------------------ human review
async def test_uncertain_document_is_corrected_and_correction_survives_reprocessing(
    env: Env,
) -> None:
    document_id = await env.upload(pdf_bytes(), "slip.pdf")  # invoice or receipt? (p=0.62)
    await env.worker().run_until_idle()
    detail = await env.detail(document_id)
    assert detail["status"] == "REVIEW_REQUIRED"
    # Uncertain type, and the one-line "invoice" lacks most invoice fields.
    assert detail["review_reasons"] == ["CLASSIFICATION_UNCERTAIN", "MISSING_REQUIRED_FIELDS"]
    machine = detail["classification"]
    assert machine["signals"]["llm"]["used"] is False
    extraction = (
        await env.client.get(
            f"/api/v1/documents/{document_id}/extraction", headers=auth_headers(env.analyst)
        )
    ).json()
    assert extraction["schema_name"] == "invoice"

    url = f"/api/v1/documents/{document_id}/classification"
    body = {"document_type": "RECEIPT", "note": "till receipt, not a bill"}
    assert (
        await env.client.patch(url, json=body, headers=auth_headers(env.viewer))
    ).status_code == 403
    assert (
        await env.client.patch(url, json=body, headers=auth_headers(env.outsider))
    ).status_code == 404
    response = await env.client.patch(url, json=body, headers=auth_headers(env.reviewer))
    assert response.status_code == 200, response.text
    corrected = response.json()
    assert corrected["method"] == "HUMAN"
    assert corrected["created_by"]["id"] == str(env.reviewer.id)
    assert corrected["signals"]["previous"]["method"] == "LOCAL_MODEL"

    # A different type means a different schema: the version is queued for re-extraction.
    detail = await env.detail(document_id)
    assert detail["status"] == "PENDING"
    assert detail["review_reasons"] == []
    assert detail["document_type"] == "RECEIPT"
    assert detail["latest_job"]["status"] == "QUEUED"
    await env.worker().run_until_idle()

    detail = await env.detail(document_id)
    assert detail["document_type"] == "RECEIPT"
    assert detail["classification"]["method"] == "HUMAN"
    assert "CLASSIFICATION_UNCERTAIN" not in detail["review_reasons"]
    history = detail["classification_history"]
    assert [c["method"] for c in history] == ["LOCAL_MODEL", "HUMAN", "LOCAL_MODEL"]
    assert [c["is_current"] for c in history] == [False, True, False]
    # The machine opinion is still recorded, but a labelled document is not worth an LLM call.
    if "llm" in history[0]["signals"]:
        assert history[0]["signals"]["llm"]["reason"] == "document has a human label"
    assert len(detail["pages"]) == 1  # results replaced, not duplicated
    extraction = (
        await env.client.get(
            f"/api/v1/documents/{document_id}/extraction", headers=auth_headers(env.analyst)
        )
    ).json()
    assert extraction["schema_name"] == "receipt"

    async with env.maker() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.entity_id == document_id,
                AuditLog.action == "document.classification.corrected",
            )
        )
        assert audit is not None
        assert audit.details["to"] == "RECEIPT"
        assert audit.details["reextraction_job_id"] is not None
        corrections = await load_corrections(session)
    assert any(
        sample.label == DocumentType.RECEIPT and "INV-1001" in sample.text for sample in corrections
    )


async def test_invalid_correction_requests(env: Env) -> None:
    document_id = await env.upload(invoice_pdf_bytes(), "inv.pdf")
    url = f"/api/v1/documents/{document_id}/classification"
    headers = auth_headers(env.reviewer)
    assert (
        await env.client.patch(url, json={"document_type": "MEME"}, headers=headers)
    ).status_code == 422
    too_long = {"document_type": "OTHER", "note": "x" * 501}
    assert (await env.client.patch(url, json=too_long, headers=headers)).status_code == 422
    extra = {"document_type": "OTHER", "confidence": 1}
    assert (await env.client.patch(url, json=extra, headers=headers)).status_code == 422


# ------------------------------------------------------------------------------ AI fallback
async def test_llm_fallback_confirms_an_uncertain_label(env: Env) -> None:
    llm = FakeLLM(DocumentType.INVOICE, quote="Invoice No. INV-1001")
    strict = env.settings.model_copy(update={"classification_min_confidence": 0.99})
    worker = Worker(
        settings=env.settings,
        sessionmaker=env.maker,
        storage=env.storage,
        services=build_processing_services(strict, sessionmaker=env.maker, llm=llm),
    )
    document_id = await env.upload(invoice_pdf_bytes(), "inv.pdf")
    await worker.run_until_idle()
    detail = await env.detail(document_id)
    assert llm.calls == 1
    classification = detail["classification"]
    assert classification["method"] == "ENSEMBLE"
    assert classification["signals"]["llm"]["used"] is True
    assert classification["signals"]["llm"]["quote_found"] is True
    assert classification["signals"]["external_ai"]["allowed"] is True
    assert detail["document_type"] == "INVOICE"


async def test_confidential_documents_never_reach_the_llm(env: Env) -> None:
    llm = FakeLLM(DocumentType.INVOICE, quote="Invoice")
    strict = env.settings.model_copy(update={"classification_min_confidence": 0.99})
    worker = Worker(
        settings=env.settings,
        sessionmaker=env.maker,
        storage=env.storage,
        services=build_processing_services(strict, sessionmaker=env.maker, llm=llm),
    )
    document_id = await env.upload(invoice_pdf_bytes(), "inv.pdf", sensitivity="CONFIDENTIAL")
    await worker.run_until_idle()
    detail = await env.detail(document_id)
    assert llm.calls == 0
    assert detail["status"] == "REVIEW_REQUIRED"
    assert detail["review_reasons"] == ["CLASSIFICATION_UNCERTAIN"]
    gate = detail["classification"]["signals"]["external_ai"]
    assert gate == {
        "allowed": False,
        "effective_sensitivity": "CONFIDENTIAL",
        "reason": "CONFIDENTIAL content may not be sent to external AI "
        "(AI_EXTERNAL_MAX_SENSITIVITY=INTERNAL)",
    }


async def test_low_ocr_confidence_sends_the_document_to_review(env: Env) -> None:
    worker = Worker(
        settings=env.settings,
        sessionmaker=env.maker,
        storage=env.storage,
        services=build_processing_services(
            env.settings, sessionmaker=env.maker, ocr=LowConfidenceOCR()
        ),
    )
    blank_scan = scanned_pdf_bytes([Image.new("L", (600, 800), 255)], dpi=100)
    document_id = await env.upload(blank_scan, "faint.pdf")
    await worker.run_until_idle()
    detail = await env.detail(document_id)
    assert detail["status"] == "REVIEW_REQUIRED"
    assert "LOW_OCR_CONFIDENCE" in detail["review_reasons"]
    assert float(detail["pages"][0]["ocr_confidence"]) == pytest.approx(31.0)


# ------------------------------------------------------------------------------ access control
async def test_content_endpoints_respect_document_scope(env: Env) -> None:
    document_id = await env.upload(invoice_pdf_bytes(), "inv.pdf")
    await env.worker().run_until_idle()
    base = f"/api/v1/documents/{document_id}"
    for path in ("/pages/1", "/pages/1/image", "/tables"):
        assert (await env.client.get(base + path)).status_code == 401
        outsider = await env.client.get(base + path, headers=auth_headers(env.outsider))
        assert outsider.status_code == 404, path
        assert (
            await env.client.get(base + path, headers=auth_headers(env.viewer))
        ).status_code == 200
    viewer = auth_headers(env.viewer)
    assert (await env.client.get(base + "/pages/2", headers=viewer)).status_code == 404
    assert (await env.client.get(base + "/pages/2/image", headers=viewer)).status_code == 404
    assert (await env.client.get(base + "/pages/0", headers=viewer)).status_code == 422
    unknown = f"/api/v1/documents/{uuid.uuid4()}/pages/1/image"
    assert (await env.client.get(unknown, headers=viewer)).status_code == 404
    async with env.maker() as session:
        records = await session.scalars(
            select(DocumentClassification).where(
                DocumentClassification.document_id == uuid.UUID(document_id)
            )
        )
        assert [record.is_current for record in records] == [True]
