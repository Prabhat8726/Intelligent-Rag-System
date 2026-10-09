"""Business document search (Module 28) over processed synthetic documents, against their
ground truth."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from docintel.ai.local_embeddings import HashingEmbeddingProvider
from docintel.db.models import Sensitivity, User
from docintel.knowledge.embedding import ChunkEmbedder
from docintel.knowledge.rag import build_rag_engines
from docintel.processing.services import build_processing_services
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.scenarios import Scenario
from tests.conftest import auth_headers
from tests.integration.conftest import Env

pytestmark = pytest.mark.integration

# Seed 19 gives bundles from vendors with 30, 45 and 60-day payment terms.
SEED = 19
SCENARIOS = [
    Scenario.CLEAN_MATCH,
    Scenario.CLEAN_MATCH,
    Scenario.VENDOR_NAME_VARIANT,
    Scenario.UNIT_PRICE_MISMATCH,
]


def embedder() -> ChunkEmbedder:
    return ChunkEmbedder(HashingEmbeddingProvider(), max_sensitivity=Sensitivity.INTERNAL)


async def ingest(env: Env, root: Path) -> dict[str, dict[str, Any]]:
    """Upload and process the dataset; returns doc_id -> {id, truth}."""
    manifest = generate_dataset(root, seed=SEED, scenarios=SCENARIOS)
    documents: dict[str, dict[str, Any]] = {}
    for entry in manifest["documents"]:
        document_id = await env.upload(
            (root / entry["file"]).read_bytes(), Path(entry["file"]).name
        )
        truth = json.loads((root / entry["ground_truth"]).read_text(encoding="utf-8"))
        documents[entry["doc_id"]] = {"id": document_id, "truth": truth}
    services = build_processing_services(env.settings, embedder=embedder())
    await env.worker(services).run_until_idle()
    env.app.state.rag = build_rag_engines(env.settings, env.maker, embedder=embedder())
    return documents


async def search(env: Env, query: str, user: User | None = None, **extra: Any) -> dict[str, Any]:
    response = await env.client.post(
        "/api/v1/search",
        headers=auth_headers(user or env.analyst),
        json={"query": query, **extra},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def found(result: dict[str, Any]) -> set[str]:
    return {hit["document"]["id"] for hit in result["results"]}


def expected(documents: dict[str, dict[str, Any]], **conditions: Any) -> set[str]:
    def keep(truth: dict[str, Any]) -> bool:
        fields = truth["fields"]
        for name, test in conditions.items():
            value = truth["document_type"] if name == "document_type" else fields.get(name)
            if not (test(value) if callable(test) else value == test):
                return False
        return True

    return {item["id"] for item in documents.values() if keep(item["truth"])}


async def test_natural_language_document_search(env: Env, tmp_path: Path) -> None:
    documents = await ingest(env, tmp_path / "dataset")
    vendors = {item["truth"]["fields"]["vendor_code"] for item in documents.values()}
    assert {"KIS", "BOS", "HPP"} <= vendors

    # "Find all invoices from Vendor X": structured filters only.
    result = await search(env, "Find all invoices from Kestrel Industrial Supply")
    assert result["mode"] == "structured"
    assert result["interpretation"]["document_types"] == ["INVOICE"]
    assert result["interpretation"]["vendor"] == "Kestrel Industrial Supply"
    assert result["interpretation"]["vendors_matched"] == ["Kestrel Industrial Supply Inc."]
    assert found(result) == expected(documents, document_type="INVOICE", vendor_code="KIS")
    assert all(hit["vendor_name"] == "Kestrel Industrial Supply Inc." for hit in result["results"])

    # "Payment terms longer than N days" on the extracted, normalized terms.
    result = await search(env, "documents with payment terms longer than 40 days")
    assert result["interpretation"]["payment_terms_days"] == {"op": "gt", "value": "40"}
    long_terms = expected(documents, payment_terms_days=lambda days: days is not None and days > 40)
    assert long_terms
    assert found(result) == long_terms
    assert all(hit["payment_terms_days"] > 40 for hit in result["results"])
    assert all(
        any("payment terms" in reason for reason in hit["reasons"]) for hit in result["results"]
    )

    # Amounts and types.
    result = await search(env, "purchase orders over 1,000")
    assert result["interpretation"]["total"] == {"op": "gt", "value": "1000"}
    assert found(result) == expected(
        documents,
        document_type="PURCHASE_ORDER",
        total=lambda total: total is not None and Decimal(str(total)) > 1000,
    )

    # Dates: the month of one invoice.
    invoice = next(
        item for item in documents.values() if item["truth"]["document_type"] == "INVOICE"
    )
    issued = invoice["truth"]["fields"]["issue_date"]
    month = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ][int(issued[5:7]) - 1]
    result = await search(env, f"invoices in {month} {issued[:4]}")
    assert invoice["id"] in found(result)
    assert all(hit["document_date"][:7] == issued[:7] for hit in result["results"])

    # Free text: a line item of one bundle is found in that bundle's documents, with a snippet.
    item = invoice["truth"]["line_items"][0]["description"]
    bundle = {
        value["id"]
        for value in documents.values()
        if value["truth"]["bundle_id"] == invoice["truth"]["bundle_id"]
    }
    result = await search(env, item)
    assert result["mode"] == "text"
    assert bundle <= found(result)
    top = result["results"][0]
    assert top["snippet"] is not None
    assert top["score"] is not None

    # Types with text: nothing to find, and the interpretation says why.
    result = await search(env, "contracts containing termination clauses")
    assert result["mode"] == "structured+text"
    assert result["interpretation"]["document_types"] == ["CONTRACT"]
    assert result["interpretation"]["text"] == "termination clauses"
    assert result["results"] == []

    # Another department sees none of these documents, whatever it asks.
    for query in ("invoices", item, "documents with payment terms longer than 40 days"):
        assert (await search(env, query, user=env.outsider))["results"] == []
    assert len((await search(env, "invoices", user=env.viewer))["results"]) == len(
        expected(documents, document_type="INVOICE")
    )


async def test_payment_terms_written_in_text_are_found(env: Env) -> None:
    """A document whose terms were not extracted is matched from its text, and says so."""
    from tests.factories.files import text_pdf_bytes

    pdf = text_pdf_bytes(
        [
            "Service agreement notes",
            "The customer shall pay all amounts within 75 days of receipt.",
            "Prepared for the facilities team.",
        ]
    )
    document_id = await env.upload(pdf, "service-notes.pdf")
    await env.worker(build_processing_services(env.settings, embedder=embedder())).run_until_idle()
    result = await search(env, "documents with payment terms longer than 60 days")
    hit = next(hit for hit in result["results"] if hit["document"]["id"] == document_id)
    assert hit["payment_terms_days"] == 75
    assert any("read from the text" in reason for reason in hit["reasons"])
    assert (
        hit["snippet"]["text"].startswith("The customer shall pay")
        or "75 days" in hit["snippet"]["text"]
    )
    shorter = await search(env, "documents with payment terms longer than 80 days")
    assert document_id not in found(shorter)


async def test_search_requires_authentication_and_validates_input(env: Env) -> None:
    response = await env.client.post("/api/v1/search", json={"query": "invoices"})
    assert response.status_code == 401
    for body in ({"query": ""}, {"query": "x" * 501}, {"query": "a", "limit": 0}, {"q": "a"}):
        response = await env.client.post(
            "/api/v1/search", headers=auth_headers(env.analyst), json=body
        )
        assert response.status_code == 422
