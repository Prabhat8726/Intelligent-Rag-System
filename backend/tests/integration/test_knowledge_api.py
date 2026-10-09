"""Knowledge base ingestion through the API and the worker (Module 12)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from docintel.ai.base import EMBEDDING_DIMENSIONS, EmbeddingResult, EmbeddingTask
from docintel.ai.errors import ProviderUnavailableError
from docintel.ai.local_embeddings import HASHING_MODEL, HashingEmbeddingProvider
from docintel.db.models import (
    AuditLog,
    Department,
    DocumentChunk,
    KnowledgeChunk,
    KnowledgeDocument,
    Sensitivity,
    User,
)
from docintel.knowledge.chunking import PATH_SEPARATOR
from docintel.knowledge.embedding import ChunkEmbedder
from docintel.knowledge.reembed import reembed
from docintel.processing.services import build_processing_services
from tests.conftest import auth_headers
from tests.factories.files import invoice_pdf_bytes, text_pdf_bytes
from tests.integration.conftest import Env

pytestmark = pytest.mark.integration

KNOWLEDGE_BASE = Path(__file__).resolve().parents[3] / "knowledge_base"
CARD = "4111 1111 1111 1111"  # Luhn-valid test card number


def policy(
    *,
    key: str,
    version: str = "1",
    effective_from: str | None = None,
    effective_to: str | None = None,
    body: str = "Invoices are paid within 30 days of receipt.",
    extra: str = "",
    category: str | None = "POLICY",
) -> str:
    lines = ["---", f"title: Payment Policy {version}", f"document_key: {key}"]
    lines.append(f"version: {version}")
    if category:
        lines.append(f"category: {category}")
    if effective_from:
        lines.append(f"effective_from: {effective_from}")
    if effective_to:
        lines.append(f"effective_to: {effective_to}")
    if extra:
        lines.append(extra)
    lines += ["---", "", "# Payment Policy", "", "## 1. Payment terms", "", body, ""]
    return "\n".join(lines)


def unique_key() -> str:
    return f"policy-{uuid.uuid4().hex[:10]}"


class ExternalEmbeddings:
    """An external (non-local) provider: the sensitivity gate applies to it."""

    name = "fake-external"
    local = False
    model = "fake-external-768"
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.error = error
        self._inner = HashingEmbeddingProvider()

    async def embed(self, texts: Sequence[str], task: EmbeddingTask) -> EmbeddingResult:
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        result = await self._inner.embed(texts, task)
        return EmbeddingResult(result.vectors, self.model, self.dimensions, 0.0)

    async def aclose(self) -> None:
        return None


def hashing_worker(env: Env, provider: Any = None, max_sensitivity: str = "INTERNAL") -> Any:
    embedder = ChunkEmbedder(
        provider or HashingEmbeddingProvider(), max_sensitivity=Sensitivity(max_sensitivity)
    )
    return env.worker(build_processing_services(env.settings, embedder=embedder))


async def upload(
    env: Env,
    content: bytes | str,
    *,
    user: User | None = None,
    filename: str = "policy.md",
    content_type: str = "text/markdown",
    data: dict[str, str] | None = None,
    expect: int = 201,
) -> dict[str, Any]:
    raw = content.encode() if isinstance(content, str) else content
    response = await env.client.post(
        "/api/v1/knowledge/documents",
        headers=auth_headers(user or env.manager),
        files={"file": (filename, raw, content_type)},
        data=data or {},
    )
    assert response.status_code == expect, response.text
    body: dict[str, Any] = response.json()
    return body


async def detail(env: Env, document_id: str, user: User | None = None) -> dict[str, Any]:
    response = await env.client.get(
        f"/api/v1/knowledge/documents/{document_id}", headers=auth_headers(user or env.manager)
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def chunks(env: Env, document_id: str, user: User | None = None) -> list[dict[str, Any]]:
    response = await env.client.get(
        f"/api/v1/knowledge/documents/{document_id}/chunks",
        headers=auth_headers(user or env.manager),
    )
    assert response.status_code == 200, response.text
    body: list[dict[str, Any]] = response.json()
    return body


# ------------------------------------------------------------------------------ ingestion
async def test_markdown_policy_is_chunked_embedded_and_audited(env: Env) -> None:
    text = (KNOWLEDGE_BASE / "procurement-policy-2026.md").read_text(encoding="utf-8")
    text = text.replace("document_key: procurement-policy", f"document_key: {unique_key()}")
    created = await upload(env, text, filename="procurement-policy-2026.md")
    assert created["status"] == "PROCESSING"
    assert created["category"] == "POLICY"
    assert created["version_label"] == "2026.1"
    assert created["effective_from"] == "2026-01-01"
    assert created["department"] is None  # organization-wide

    assert await hashing_worker(env).run_until_idle() == 1
    document = await detail(env, created["id"], env.viewer)  # knowledge:read is enough
    assert document["status"] == "ACTIVE"
    assert document["embedding_model"] == HASHING_MODEL
    assert document["embedding_note"] is None
    assert document["effective_sensitivity"] == "INTERNAL"
    assert document["latest_job"]["status"] == "COMPLETED"
    assert set(document["latest_job"]["stage_timings"]) == {"integrity", "parse", "chunk", "embed"}

    passages = await chunks(env, created["id"])
    assert len(passages) == document["chunk_count"] > 10
    assert all(passage["has_embedding"] for passage in passages)
    tolerance = next(p for p in passages if p["heading"] == "4.1 Tolerance")
    assert tolerance["section_path"] == f"4. Price variance{PATH_SEPARATOR}4.1 Tolerance"
    assert "0.01" in tolerance["content"]
    assert tolerance["effective_from"] == "2026-01-01"
    assert tolerance["effective_to"] is None

    async with env.maker() as session:
        actions = (
            await session.scalars(
                select(AuditLog.action).where(AuditLog.entity_id == created["id"])
            )
        ).all()
        stored = await session.scalar(
            select(KnowledgeChunk.search).where(
                KnowledgeChunk.knowledge_document_id == uuid.UUID(created["id"]),
                KnowledgeChunk.heading == "4.1 Tolerance",
            )
        )
    assert set(actions) == {"knowledge.uploaded", "knowledge.processing.completed"}
    assert stored is not None
    assert "toler" in stored  # weighted, stemmed full-text vector


async def test_without_an_embedding_provider_chunks_are_full_text_only(env: Env) -> None:
    created = await upload(env, policy(key=unique_key()))
    await env.worker().run_until_idle()  # test settings: Gemini without a key
    document = await detail(env, created["id"])
    assert document["status"] == "ACTIVE"
    assert document["embedding_model"] is None
    assert "no embedding provider" in document["embedding_note"]
    assert not any(passage["has_embedding"] for passage in await chunks(env, created["id"]))


async def test_pdf_knowledge_document_uses_the_extraction_pipeline(env: Env) -> None:
    pdf = text_pdf_bytes(
        [
            "1. Scope",
            "This policy covers all company records.",
            "2. Retention periods",
            "Supplier invoices are kept for ten years.",
        ]
    )
    created = await upload(
        env,
        pdf,
        filename="records-retention.pdf",
        content_type="application/pdf",
        data={"category": "COMPLIANCE", "document_key": unique_key()},
    )
    assert created["source_format"] == "PDF"
    assert created["page_count"] == 1
    await hashing_worker(env).run_until_idle()
    document = await detail(env, created["id"])
    assert document["status"] == "ACTIVE", document["processing_error"]
    passages = await chunks(env, created["id"])
    retention = next(p for p in passages if p["heading"] == "2. Retention periods")
    assert "ten years" in retention["content"]
    assert (retention["page_start"], retention["page_end"]) == (1, 1)


# ------------------------------------------------------------------------------ versions
async def _windows(env: Env, document_id: str) -> set[tuple[str | None, str | None]]:
    return {(p["effective_from"], p["effective_to"]) for p in await chunks(env, document_id)}


@pytest.mark.parametrize("newest_first", [False, True])
async def test_a_new_version_supersedes_the_old_one_in_either_upload_order(
    env: Env, newest_first: bool
) -> None:
    key = unique_key()
    old_text = policy(
        key=key, version="2025", effective_from="2025-01-01", effective_to="2025-12-31"
    )
    new_text = policy(key=key, version="2026", effective_from="2026-01-01", body="Paid in 45 days.")
    worker = hashing_worker(env)
    order = [new_text, old_text] if newest_first else [old_text, new_text]
    ids = []
    for text in order:
        ids.append((await upload(env, text, filename="payment.md"))["id"])
        await worker.run_until_idle()
    new_id, old_id = (ids[0], ids[1]) if newest_first else (ids[1], ids[0])

    old, new = await detail(env, old_id), await detail(env, new_id)
    assert (old["status"], new["status"]) == ("SUPERSEDED", "ACTIVE")
    assert await _windows(env, old_id) == {("2025-01-01", "2025-12-31")}
    assert await _windows(env, new_id) == {("2026-01-01", None)}
    if not newest_first:
        assert new["supersedes_id"] == old_id

    listed = await env.client.get(
        "/api/v1/knowledge/documents",
        params={"document_key": key},
        headers=auth_headers(env.viewer),
    )
    assert [item["version_label"] for item in listed.json()["items"]] == ["2026", "2025"]


async def test_archiving_the_active_version_restores_the_previous_one(env: Env) -> None:
    key = unique_key()
    worker = hashing_worker(env)
    first = await upload(env, policy(key=key, version="1", effective_from="2026-01-01"))
    await worker.run_until_idle()
    second = await upload(
        env, policy(key=key, version="2", effective_from="2026-06-01", body="Paid in 45 days.")
    )
    await worker.run_until_idle()
    assert await _windows(env, first["id"]) == {("2026-01-01", "2026-05-31")}

    response = await env.client.delete(
        f"/api/v1/knowledge/documents/{second['id']}", headers=auth_headers(env.manager)
    )
    assert response.status_code == 204
    gone = await env.client.get(
        f"/api/v1/knowledge/documents/{second['id']}", headers=auth_headers(env.manager)
    )
    assert gone.status_code == 404
    restored = await detail(env, first["id"])
    assert restored["status"] == "ACTIVE"
    assert await _windows(env, first["id"]) == {("2026-01-01", None)}
    async with env.maker() as session:
        archived = await session.get(KnowledgeDocument, uuid.UUID(second["id"]))
        left = await session.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.knowledge_document_id == uuid.UUID(second["id"]))
        )
    assert archived is not None
    assert archived.status == "ARCHIVED"
    assert left == 0


async def test_version_conflicts(env: Env) -> None:
    key = unique_key()
    text = policy(key=key)
    first = await upload(env, text)
    # A second version may be uploaded while the first is processing: the later upload wins.
    second = await upload(env, policy(key=key, version="2", body="Paid within 45 days."))
    await upload(env, text, expect=409)  # identical file, even while processing
    await hashing_worker(env).run_until_idle()
    assert (await detail(env, first["id"]))["status"] == "SUPERSEDED"
    assert (await detail(env, second["id"]))["status"] == "ACTIVE"
    # Same key restricted to a department: a version may not change its audience.
    await upload(
        env,
        policy(key=key, version="3"),
        data={"department_id": str(env.manager.department_id)},
        expect=409,
    )


# ------------------------------------------------------------------------------ access
async def test_department_knowledge_is_invisible_outside_the_department(env: Env) -> None:
    async with env.maker() as session:
        legal = await session.scalar(
            select(Department.name).where(Department.id == env.outsider.department_id)
        )
    assert legal is not None
    playbook = policy(
        key=unique_key(),
        category="CONTRACT_GUIDELINE",
        extra=f"department: {legal.upper()}\nsensitivity: CONFIDENTIAL",
    )
    # A Finance manager may not publish for Legal; an administrator may.
    await upload(env, playbook, user=env.manager, expect=403)
    created = await upload(env, playbook, user=env.admin)
    assert created["department"]["name"] == legal
    await hashing_worker(env).run_until_idle()

    for finance_user in (env.manager, env.viewer, env.analyst):
        response = await env.client.get(
            f"/api/v1/knowledge/documents/{created['id']}", headers=auth_headers(finance_user)
        )
        assert response.status_code == 404
        listing = await env.client.get(
            "/api/v1/knowledge/documents", headers=auth_headers(finance_user)
        )
        assert created["id"] not in {item["id"] for item in listing.json()["items"]}
    assert (await detail(env, created["id"], env.outsider))["status"] == "ACTIVE"


async def test_only_knowledge_managers_upload_or_archive(env: Env) -> None:
    for user in (env.analyst, env.reviewer, env.viewer):
        await upload(env, policy(key=unique_key()), user=user, expect=403)
    created = await upload(env, policy(key=unique_key()))
    response = await env.client.delete(
        f"/api/v1/knowledge/documents/{created['id']}", headers=auth_headers(env.analyst)
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("content", "filename", "content_type", "status_code", "message"),
    [
        (b"# Policy\x00\x01binary", "policy.md", "text/markdown", 422, "binary"),
        (b"\xff\xfe\x00bad", "policy.md", "text/markdown", 422, "UTF-8"),
        ("# Policy\n\nNo category here.", "policy.md", "text/markdown", 422, "category"),
        ("---\ntitle: x\n", "policy.md", "text/markdown", 422, "front matter"),
        (
            "---\ncategory: POLICY\neffective_from: 2026-13-01\n---\n# P\n\nText.",
            "p.md",
            "text/markdown",
            422,
            "effective_from",
        ),
        ("# Policy", "policy.docx", "application/octet-stream", 415, "extension"),
        (invoice_pdf_bytes(), "policy.md", "text/markdown", 415, "does not match"),
        ("# Policy\n\nText.", "policy.md", "application/pdf", 415, "content type"),
    ],
)
async def test_invalid_knowledge_files_are_rejected(
    env: Env,
    content: bytes | str,
    filename: str,
    content_type: str,
    status_code: int,
    message: str,
) -> None:
    body = await upload(
        env, content, filename=filename, content_type=content_type, expect=status_code
    )
    assert message.lower() in body["detail"].lower()


# ------------------------------------------------------------------------------ AI gate
async def test_external_embeddings_respect_the_sensitivity_gate(env: Env) -> None:
    provider = ExternalEmbeddings()
    worker = hashing_worker(env, provider, max_sensitivity="INTERNAL")
    internal = await upload(env, policy(key=unique_key()))
    confidential = await upload(
        env, policy(key=unique_key(), version="c"), data={"sensitivity": "CONFIDENTIAL"}
    )
    # Labelled INTERNAL, but a card number in the text makes it RESTRICTED.
    card = await upload(env, policy(key=unique_key(), version="r", body=f"Card {CARD} on file."))
    await worker.run_until_idle()

    sent = [text for call in provider.calls for text in call]
    assert sent
    assert all("Payment Policy 1" in text for text in sent)  # only the INTERNAL document
    assert (await detail(env, internal["id"]))["embedding_model"] == "fake-external-768"
    for document_id, level in ((confidential["id"], "CONFIDENTIAL"), (card["id"], "RESTRICTED")):
        document = await detail(env, document_id)
        assert document["status"] == "ACTIVE"
        assert document["effective_sensitivity"] == level
        assert document["embedding_model"] is None
        assert "may not be sent to external AI" in document["embedding_note"]
        assert not any(p["has_embedding"] for p in await chunks(env, document_id))


async def test_embedding_failure_on_the_last_attempt_keeps_the_document(env: Env) -> None:
    provider = ExternalEmbeddings(ProviderUnavailableError("down", provider="fake"))
    created = await upload(env, policy(key=unique_key()))
    await hashing_worker(env, provider, max_sensitivity="RESTRICTED").run_until_idle()
    document = await detail(env, created["id"])
    assert len(provider.calls) == document["latest_job"]["max_attempts"]  # retried, then kept
    assert document["status"] == "ACTIVE"
    assert document["chunk_count"] > 0
    assert document["embedding_model"] is None
    assert "reembed" in document["embedding_note"]


async def test_embedding_failure_is_retried_while_attempts_remain(env: Env) -> None:
    provider = ExternalEmbeddings(ProviderUnavailableError("down", provider="fake"))
    created = await upload(env, policy(key=unique_key()))
    worker = hashing_worker(env, provider, max_sensitivity="RESTRICTED")
    await worker.run_once()
    document = await detail(env, created["id"])
    assert document["status"] == "PROCESSING"
    assert document["latest_job"]["status"] == "QUEUED"  # retry scheduled
    provider.error = None
    await worker.run_until_idle()
    assert (await detail(env, created["id"]))["embedding_model"] == "fake-external-768"


# ------------------------------------------------------------------------------ documents
async def test_business_documents_are_indexed_and_removed_on_delete(env: Env) -> None:
    document_id = await env.upload(invoice_pdf_bytes())
    await hashing_worker(env).run_until_idle()
    async with env.maker() as session:
        rows = (
            await session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id == uuid.UUID(document_id))
            )
        ).all()
    assert rows
    assert rows[0].context_prefix.startswith("Invoice: ")
    assert any("Kestrel Industrial Supply" in row.content for row in rows)
    assert all(row.embedding_model == HASHING_MODEL for row in rows)

    response = await env.client.delete(
        f"/api/v1/documents/{document_id}", headers=auth_headers(env.manager)
    )
    assert response.status_code == 204
    async with env.maker() as session:
        left = await session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == uuid.UUID(document_id))
        )
    assert left == 0


async def test_reembed_adds_vectors_of_the_configured_model(env: Env) -> None:
    public = await upload(env, policy(key=unique_key()))
    confidential = await upload(
        env, policy(key=unique_key(), version="c"), data={"sensitivity": "CONFIDENTIAL"}
    )
    invoice = await env.upload(invoice_pdf_bytes())
    await env.worker().run_until_idle()  # no embedding provider: full-text only
    assert (await detail(env, public["id"]))["embedding_model"] is None

    provider = ExternalEmbeddings()
    embedder = ChunkEmbedder(provider, max_sensitivity=Sensitivity.INTERNAL)
    summary = await reembed(env.maker, embedder)
    assert summary.chunks_embedded > 0
    assert summary.chunks_blocked > 0
    assert (await detail(env, public["id"]))["embedding_model"] == "fake-external-768"
    assert all(p["has_embedding"] for p in await chunks(env, public["id"]))
    blocked = await detail(env, confidential["id"])
    assert blocked["embedding_model"] is None
    assert "CONFIDENTIAL" in blocked["embedding_note"]
    async with env.maker() as session:
        models = set(
            await session.scalars(
                select(DocumentChunk.embedding_model).where(
                    DocumentChunk.document_id == uuid.UUID(invoice)
                )
            )
        )
    assert models == {"fake-external-768"}

    sent = len(provider.calls)
    again = await reembed(env.maker, embedder)  # nothing stale: no provider calls
    assert again.chunks_embedded == 0
    assert len(provider.calls) == sent
