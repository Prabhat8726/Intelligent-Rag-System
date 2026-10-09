"""Retrieval and cited answers over the seed knowledge base (Module 13), through the API."""

from __future__ import annotations

import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from docintel.ai.base import LLMRequest, StructuredLLMResponse
from docintel.ai.local_embeddings import HASHING_MODEL, HashingEmbeddingProvider
from docintel.db.models import AuditLog, LLMCall, Sensitivity, User
from docintel.knowledge.embedding import ChunkEmbedder
from docintel.knowledge.rag import build_rag_engines
from docintel.processing.services import build_processing_services
from tests.conftest import auth_headers
from tests.factories.llm import ScriptedLLM
from tests.integration.conftest import Env

pytestmark = pytest.mark.integration

KNOWLEDGE_BASE = Path(__file__).resolve().parents[3] / "knowledge_base"
PLAYBOOK = "legal-negotiation-playbook"


def hashing_embedder() -> ChunkEmbedder:
    return ChunkEmbedder(HashingEmbeddingProvider(), max_sensitivity=Sensitivity.INTERNAL)


async def seed_knowledge_base(env: Env) -> dict[str, str]:
    """Upload the seed knowledge base under keys unique to this test; returns key -> test key.
    The Legal playbook is filed into the test's Legal department."""
    suffix = uuid.uuid4().hex[:8]
    keys: dict[str, str] = {}
    for path in sorted(KNOWLEDGE_BASE.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        key = next(
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("document_key:")
        )
        keys[key] = f"{key}-{suffix}"
        text = text.replace(f"document_key: {key}", f"document_key: {keys[key]}")
        data = {}
        if key == PLAYBOOK:
            text = text.replace("department: Legal\n", "")
            data["department_id"] = str(env.outsider.department_id)
        response = await env.client.post(
            "/api/v1/knowledge/documents",
            headers=auth_headers(env.admin),
            files={"file": (path.name, text.encode(), "text/markdown")},
            data=data,
        )
        assert response.status_code == 201, response.text
    services = build_processing_services(env.settings, embedder=hashing_embedder())
    await env.worker(services).run_until_idle()
    return keys


class CitingLLM(ScriptedLLM):
    """Answers with one claim citing whichever provided source contains `phrase`."""

    def __init__(self, phrase: str, claim: str) -> None:
        super().__init__()
        self.phrase = phrase
        self.claim = claim

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredLLMResponse[T]:
        labels = [
            match.group(1)
            for block in re.split(r"\n(?=\[S\d+\] )", request.prompt)
            if self.phrase in block and (match := re.match(r"\[(S\d+)\]", block))
        ]
        self.script = [{"claims": [{"text": self.claim, "citations": labels[:1] or ["S1"]}]}]
        return await super().generate_structured(request, schema)


def use_engines(env: Env, llm: ScriptedLLM | None = None) -> None:
    env.app.state.rag = build_rag_engines(
        env.settings, env.maker, llm=llm, embedder=hashing_embedder()
    )


async def search(
    env: Env, query: str, keys: dict[str, str], user: User | None = None, **extra: Any
) -> dict[str, Any]:
    response = await env.client.post(
        "/api/v1/knowledge/search",
        headers=auth_headers(user or env.viewer),
        json={"query": query, "document_keys": list(keys.values()), **extra},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def ask(
    env: Env, question: str, keys: dict[str, str], user: User | None = None, **extra: Any
) -> dict[str, Any]:
    response = await env.client.post(
        "/api/v1/knowledge/query",
        headers=auth_headers(user or env.viewer),
        json={"question": question, "document_keys": list(keys.values()), **extra},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def documents(passages: list[dict[str, Any]], keys: dict[str, str]) -> list[str]:
    original = {test_key: key for key, test_key in keys.items()}
    return [original[p["document_key"]] for p in passages]


# ------------------------------------------------------------------------------ retrieval
async def test_hybrid_search_finds_the_right_section(env: Env) -> None:
    keys = await seed_knowledge_base(env)
    use_engines(env)
    result = await search(env, "What is the price tolerance on invoices?", keys)
    assert result["retrieval"]["mode"] == "hybrid"
    assert result["retrieval"]["embedding_model"] == HASHING_MODEL
    assert result["evidence"]["sufficient"] is True
    top = result["passages"][0]
    assert documents([top], keys) == ["procurement-policy"]
    assert top["heading"] == "4.1 Tolerance"
    assert top["version_label"] == "2026.1"
    assert top["dense_similarity"] is not None
    assert top["text_score"] is not None


async def test_without_embeddings_search_is_full_text(env: Env) -> None:
    keys = await seed_knowledge_base(env)  # app engines: no embedding provider configured
    result = await search(env, "price tolerance", keys)
    assert result["retrieval"]["mode"] == "full_text"
    assert result["passages"][0]["heading"] == "4.1 Tolerance"


async def test_superseded_policy_is_only_cited_for_past_dates(env: Env) -> None:
    keys = await seed_knowledge_base(env)
    use_engines(env)
    question = "What is the maximum payment term that can be agreed with a vendor?"
    current = await search(env, question, keys)
    policy_key = keys["procurement-policy"]
    versions = {p["version_label"] for p in current["passages"] if p["document_key"] == policy_key}
    assert versions == {"2026.1"}
    assert all(p["status"] == "ACTIVE" for p in current["passages"])

    past = await search(env, question, keys, as_of="2025-06-30")
    policy = [p for p in past["passages"] if p["document_key"] == policy_key]
    assert {p["version_label"] for p in policy} == {"2025.1"}
    assert any("90 days" in p["content"] for p in policy)
    assert all(p["status"] == "SUPERSEDED" for p in policy)
    # Documents that only take effect later are not cited for that date.
    assert date.fromisoformat(past["retrieval"]["as_of"]) == date(2025, 6, 30)
    assert all(
        p["effective_from"] is None or p["effective_from"] <= "2025-06-30" for p in past["passages"]
    )


async def test_department_knowledge_never_leaves_the_database_for_others(env: Env) -> None:
    keys = await seed_knowledge_base(env)
    use_engines(env)
    question = "What termination notice may Legal accept as a fallback?"
    for user in (env.viewer, env.manager, env.analyst):  # Finance
        result = await search(env, question, keys, user=user)
        assert PLAYBOOK not in documents(result["passages"], keys)
    legal = await search(env, question, keys, user=env.outsider)
    assert documents(legal["passages"], keys)[0] == PLAYBOOK
    admin = await search(env, question, keys, user=env.admin)
    assert PLAYBOOK in documents(admin["passages"], keys)


async def test_category_filter(env: Env) -> None:
    keys = await seed_knowledge_base(env)
    use_engines(env)
    result = await search(env, "invoice", keys, categories=["FAQ"])
    assert result["passages"]
    assert {p["category"] for p in result["passages"]} == {"FAQ"}


# ------------------------------------------------------------------------------ answers
async def test_cited_answer(env: Env) -> None:
    keys = await seed_knowledge_base(env)
    llm = CitingLLM(
        "A difference of up to 0.01",
        "A difference of up to 0.01 per unit is accepted to allow for rounding.",
    )
    use_engines(env, llm)
    result = await ask(env, "How much may an invoiced unit price differ from the order?", keys)
    assert result["status"] == "ANSWERED"
    (claim,) = result["claims"]
    assert claim["grounded"] is True
    (label,) = claim["citations"]
    assert result["answer"].endswith(f"[{label}]")
    cited = next(source for source in result["sources"] if source["label"] == label)
    assert cited["cited"] is True
    assert cited["sent_to_model"] is True
    assert cited["section_path"].endswith("4.1 Tolerance")
    assert cited["version_label"] == "2026.1"
    assert result["provider"] == "scripted"

    (request,) = llm.requests
    assert request.purpose == "rag.answer"
    assert request.system_instruction is not None
    assert "untrusted data" in request.system_instruction
    assert f"[{label}] Procurement Policy, version 2026.1" in request.prompt
    assert "<<<SOURCES " in request.prompt
    async with env.maker() as session:
        audit = await session.scalar(
            select(AuditLog)
            .where(AuditLog.action == "knowledge.queried", AuditLog.actor_id == env.viewer.id)
            .order_by(AuditLog.id.desc())
        )
        calls = await session.scalar(
            select(LLMCall).where(LLMCall.purpose == "rag.answer").order_by(LLMCall.id.desc())
        )
    assert audit is not None
    assert audit.details["status"] == "ANSWERED"
    assert "question" not in audit.details  # only a fingerprint of the question
    assert len(audit.details["question_sha256"]) == 64
    assert [s["cited"] for s in audit.details["sources"] if s["label"] == label] == [True]
    assert calls is not None  # accounted like every model call


async def test_invalid_and_ungrounded_claims_are_flagged(env: Env) -> None:
    keys = await seed_knowledge_base(env)
    llm = ScriptedLLM(
        {
            "claims": [
                {"text": "Suppliers are paid by cheque.", "citations": ["S9"]},  # not provided
                {"text": "A price difference of 7531 percent is fine.", "citations": ["S1"]},
            ]
        }
    )
    use_engines(env, llm)
    result = await ask(env, "What is the price tolerance on invoices?", keys)
    assert result["status"] == "PARTIALLY_SUPPORTED"
    (flagged,) = result["claims"]  # the claim citing a source that was not provided is gone
    assert flagged["grounded"] is False  # 7531 occurs in no cited source
    assert any("without a valid citation" in notice for notice in result["notices"])
    assert any("could not be matched" in notice for notice in result["notices"])
    assert "cheque" not in result["answer"]


async def test_unanswerable_questions_are_refused_without_a_model_call(env: Env) -> None:
    keys = await seed_knowledge_base(env)
    llm = ScriptedLLM()
    use_engines(env, llm)
    result = await ask(env, "What is the dress code at the head office?", keys)
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["answer"] is None
    assert result["evidence"]["sufficient"] is False
    assert llm.requests == []


async def test_model_may_report_insufficient_evidence(env: Env) -> None:
    keys = await seed_knowledge_base(env)
    use_engines(env, ScriptedLLM({"claims": [], "insufficient_evidence": True}))
    result = await ask(env, "Which invoices need a purchase order?", keys)
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["answer"] is None


async def test_confidential_sources_are_not_sent_to_an_external_model(env: Env) -> None:
    keys = await seed_knowledge_base(env)
    llm = ScriptedLLM(
        {"claims": [{"text": "Termination needs notice.", "citations": ["S1"]}]}, local=False
    )
    use_engines(env, llm)  # AI_EXTERNAL_MAX_SENSITIVITY=INTERNAL (default)
    result = await ask(
        env, "What termination notice may Legal accept as a fallback?", keys, user=env.outsider
    )
    playbook = [s for s in result["sources"] if s["document_key"] == keys[PLAYBOOK]]
    assert playbook, "the Legal user still sees the playbook passages"
    assert all(source["sent_to_model"] is False for source in playbook)
    assert any("sensitivity limit" in notice for notice in result["notices"])
    for request in llm.requests:
        assert "up to 90 days for suppliers" not in request.prompt
        assert "Legal Negotiation Playbook" not in request.prompt


async def test_without_a_model_the_answer_is_retrieval_only(env: Env) -> None:
    keys = await seed_knowledge_base(env)
    use_engines(env, llm=None)
    result = await ask(env, "How long are supplier invoices retained?", keys)
    assert result["status"] == "RETRIEVAL_ONLY"
    assert result["answer"] is None
    assert result["model"] is None
    assert any("ten years" in source["content"].lower() for source in result["sources"])


async def test_query_requires_authentication_and_valid_input(env: Env) -> None:
    response = await env.client.post("/api/v1/knowledge/query", json={"question": "Who pays?"})
    assert response.status_code == 401
    response = await env.client.post(
        "/api/v1/knowledge/query",
        headers=auth_headers(env.viewer),
        json={"question": "x" * 1001},
    )
    assert response.status_code == 422
    response = await env.client.post(
        "/api/v1/knowledge/search",
        headers=auth_headers(env.viewer),
        json={"query": "tolerance", "unexpected": True},
    )
    assert response.status_code == 422
