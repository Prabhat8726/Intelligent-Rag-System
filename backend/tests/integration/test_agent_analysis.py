"""Investigations end to end (Modules 14-17): the API, the worker running the LangGraph graph
over controlled tools, deterministic and model-assisted analysis, guardrails and accounting."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from docintel.agent.runner import build_agent_deps
from docintel.ai.accounting import AccountedLLMProvider, DatabaseLLMCallLog
from docintel.ai.base import LLMRequest, LLMUsage, StructuredLLMResponse
from docintel.db.models import AgentToolCall, AuditLog, LLMCall, ReviewTask, User
from docintel.processing.services import build_processing_services
from docintel.synthetic.scenarios import Scenario
from docintel.workers.runner import Worker
from tests.conftest import auth_headers
from tests.factories.llm import ScriptedLLM
from tests.integration.conftest import Env
from tests.integration.test_knowledge_rag import hashing_embedder, seed_knowledge_base
from tests.integration.test_matching_api import bundle

pytestmark = pytest.mark.integration

GRAPH_NODES = [
    "understand_request",
    "identify_documents",
    "inspect_extraction",
    "run_rules",
    "retrieve_knowledge",
    "analyze",
    "determine_confidence",
    "recommend",
    "approval_gate",
]


async def processed(
    env: Env, tmp_path: Path, scenario: Scenario, seed: int, sensitivity: str = "INTERNAL"
) -> dict[str, str]:
    """The scenario's documents uploaded and processed; suffix -> document id."""
    docs = bundle(tmp_path, scenario, seed=seed)
    ids = {
        suffix: await env.upload(
            content, f"{scenario.value[:4]}-{suffix}.pdf", sensitivity=sensitivity
        )
        for suffix, (content, _) in docs.items()
    }
    await worker(env).run_until_idle()
    return ids


def worker(env: Env, llm: Any = None) -> Worker:
    services = build_processing_services(
        env.settings, sessionmaker=env.maker, embedder=hashing_embedder()
    )
    if llm is not None:
        log = DatabaseLLMCallLog(env.maker, daily_request_budget=0)
        llm = AccountedLLMProvider(llm, log)
    agent = build_agent_deps(env.settings, env.maker, llm=llm, embedder=hashing_embedder())
    return Worker(
        settings=env.settings,
        sessionmaker=env.maker,
        storage=env.storage,
        services=services,
        agent=agent,
    )


async def start(
    env: Env,
    user: User,
    query: str,
    document_ids: list[str] | None = None,
    **extra: Any,
) -> httpx.Response:
    return await env.client.post(
        "/api/v1/analysis",
        json={"query": query, "document_ids": document_ids or [], **extra},
        headers=auth_headers(user),
    )


async def investigate(
    env: Env, user: User, query: str, document_ids: list[str] | None = None, *, llm: Any = None,
    **extra: Any,
) -> dict[str, Any]:  # fmt: skip
    response = await start(env, user, query, document_ids, **extra)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "QUEUED"
    assert await worker(env, llm).run_until_idle() == 1
    result = await env.client.get(response.headers["Location"], headers=auth_headers(user))
    assert result.status_code == 200, result.text
    data: dict[str, Any] = result.json()
    assert data["status"] == "COMPLETED", data["error"]
    return data


def evidence_labels(data: dict[str, Any]) -> set[str]:
    return {item["label"] for item in data["result"]["evidence"]}


def rule_codes(data: dict[str, Any], category: str = "RULE_RESULT") -> list[str]:
    return [
        match.group(1)
        for finding in data["result"]["findings"]
        if finding["category"] == category
        and (match := re.search(r"\(([A-Z_]+), [A-Z]+\)", finding["statement"]))
    ]


# ------------------------------------------------------------------------------ deterministic
async def test_a_price_mismatch_is_investigated_and_sent_for_review(
    env: Env, tmp_path: Path
) -> None:
    await seed_knowledge_base(env)
    ids = await processed(env, tmp_path, Scenario.UNIT_PRICE_MISMATCH, 21)
    data = await investigate(env, env.reviewer, "Can we pay this invoice?", [ids["INV"]])

    assert data["plan"]["intent"] == "VERIFY_DOCUMENT"
    assert data["plan"]["source"] == "rules"
    assert [step["node"] for step in data["trace"]] == [
        *GRAPH_NODES,
        "execute_safe_action",
        "finalize",
    ]
    result = data["result"]
    assert result["summary_source"] == "rules"
    assert result["summary"].startswith("UNIT-INV.pdf (invoice): 1 rule(s) failed")
    roles = {doc["filename"]: doc["role"] for doc in result["documents"]}
    assert roles == {"UNIT-INV.pdf": "subject", "UNIT-PO.pdf": "related", "UNIT-DN.pdf": "related"}
    assert rule_codes(data) == ["INV_PO_UNIT_PRICE"]
    # Every finding cites evidence that exists; facts come from the rules, not a model.
    labels = evidence_labels(data)
    for finding in result["findings"]:
        assert set(finding["evidence"]) <= labels, finding
        assert finding["source"] == "rules"
    price = next(f for f in result["findings"] if "INV_PO_UNIT_PRICE" in f["statement"])
    assert any(label.startswith("D1.C") for label in price["evidence"])  # the comparison item
    sections = [source["section_path"] for source in result["sources"]]
    assert "4. Price variance › 4.1 Tolerance" in sections  # noqa: RUF001 - breadcrumb

    recommendation = result["recommendation"]
    assert recommendation["action"] == "HOLD_FOR_REVIEW"
    assert (recommendation["risk"], recommendation["requires_approval"]) == ("LOW", False)
    assert recommendation["target_document_id"] == ids["INV"]
    action = result["action"]
    assert action["status"] == "EXECUTED"
    assert result["confidence"]["level"] in ("HIGH", "MEDIUM")

    # The executed action is a review request on the document's task, linked to the run.
    async with env.maker() as session:
        task = await session.get(ReviewTask, uuid.UUID(action["review_task_id"]))
        assert task is not None
        requested = [reason for reason in task.reasons if reason["category"] == "REQUESTED"]
        assert len(requested) == 1
        assert requested[0]["message"].startswith(f"Investigation {data['id'][:8]}:")
        calls = list(
            await session.scalars(
                select(AgentToolCall)
                .where(AgentToolCall.run_id == uuid.UUID(data["id"]))
                .order_by(AgentToolCall.created_at)
            )
        )
        audits = {
            row.action: row
            for row in await session.scalars(
                select(AuditLog).where(AuditLog.entity_id == data["id"])
            )
        }
    assert [call.tool_name for call in calls][:2] == ["get_document", "get_extracted_fields"]
    assert calls[-1].tool_name == "create_review_task"
    assert all(call.status.value == "SUCCEEDED" for call in calls)
    assert len(data["tool_call_log"]) == data["usage"]["tool_calls"] == len(calls)
    assert data["usage"]["llm_calls"] == 0
    assert set(audits) == {"analysis.requested", "analysis.completed"}
    assert audits["analysis.completed"].actor_type.value == "AGENT"
    assert audits["analysis.completed"].actor_id == env.reviewer.id
    assert audits["analysis.completed"].details["recommendation"] == "HOLD_FOR_REVIEW"
    assert "Can we pay" not in str(audits["analysis.requested"].details)  # fingerprint only


async def test_clean_invoices_are_proposed_for_payment_and_duplicates_for_rejection(
    env: Env, tmp_path: Path
) -> None:
    clean = await processed(env, tmp_path, Scenario.CLEAN_MATCH, 22)
    duplicate = await processed(env, tmp_path, Scenario.DUPLICATE_INVOICE, 23)

    paid = await investigate(env, env.analyst, "Can we pay this invoice?", [clean["INV"]])
    result = paid["result"]
    assert result["confidence"]["level"] == "HIGH"
    recommendation = result["recommendation"]
    assert recommendation["action"] == "APPROVE_FOR_PAYMENT"
    assert (recommendation["requires_approval"], recommendation["required_role"]) == (
        True,
        "MANAGER",
    )
    assert result["action"]["status"] == "PROPOSED"  # proposed only: nothing executed
    assert paid["trace"][-2]["node"] == "propose_for_approval"
    assert (await env.detail(clean["INV"]))["status"] == "COMPLETED"  # untouched

    checked = await investigate(env, env.analyst, "Is this a duplicate?", [duplicate["INV2"]])
    assert checked["plan"]["intent"] == "CHECK_DUPLICATE"
    assert "INV_DUPLICATE" in rule_codes(checked)
    assert checked["result"]["recommendation"]["action"] == "REJECT_DUPLICATE"
    assert checked["result"]["action"]["status"] == "PROPOSED"
    related = {d["filename"] for d in checked["result"]["documents"] if d["role"] == "related"}
    assert "DUPL-INV.pdf" in related  # the original is shown with the copy


async def test_documents_are_found_from_the_question_or_not_guessed(
    env: Env, tmp_path: Path
) -> None:
    await processed(env, tmp_path, Scenario.UNIT_PRICE_MISMATCH, 21)
    found = await investigate(
        env,
        env.reviewer,
        "Why does the Bluepeak Office Solutions invoice not match its purchase order?",
        allow_safe_actions=False,
    )
    assert found["plan"]["intent"] == "INVESTIGATE_DISCREPANCY"
    assert found["plan"]["document_query"] == "invoice from Bluepeak Office Solutions"
    subjects = [d["filename"] for d in found["result"]["documents"] if d["role"] == "subject"]
    assert subjects == ["UNIT-INV.pdf"]
    assert found["result"]["recommendation"]["action"] == "HOLD_FOR_REVIEW"
    assert found["result"]["action"]["status"] == "SKIPPED"  # safe actions were not allowed
    assert any(f["detail"].startswith("Documents were identified by search") for f in
               found["result"]["confidence"]["factors"])  # fmt: skip

    vague = await investigate(env, env.reviewer, "What is wrong with this invoice?")
    assert vague["result"]["documents"] == []
    assert vague["result"]["recommendation"]["action"] == "NO_ACTION"
    assert vague["result"]["confidence"]["level"] == "LOW"
    assert vague["usage"]["tool_calls"] == 0
    assert any("does not identify a document" in notice for notice in vague["result"]["notices"])


async def test_policy_questions_are_answered_from_the_knowledge_base(env: Env) -> None:
    await seed_knowledge_base(env)
    data = await investigate(
        env, env.reviewer, "Who must approve payment terms longer than 60 days?"
    )
    assert data["plan"]["intent"] == "POLICY_QUESTION"
    result = data["result"]
    assert result["documents"] == []
    assert result["recommendation"]["action"] == "NO_ACTION"
    assert result["sources"]
    assert result["sources"][0]["section_path"] == "7. Payment terms"
    assert {f["category"] for f in result["findings"]} == {"RETRIEVED_KNOWLEDGE"}
    assert [step["node"] for step in data["trace"]][:3] == [
        "understand_request",
        "identify_documents",
        "retrieve_knowledge",
    ]


# ------------------------------------------------------------------------------ with a model
class AnalystLLM(ScriptedLLM):
    """A model that plans, writes one grounded and several invalid findings, asks one follow-up
    question and proposes paying the invoice (which the guardrails must refuse)."""

    def __init__(self) -> None:
        super().__init__()
        self.analyses = 0

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredLLMResponse[T]:
        self.requests.append(request)
        if request.purpose == "agent.plan":
            data: dict[str, Any] = {
                "intent": "VERIFY_DOCUMENT",
                "document_query": None,
                "knowledge_questions": ["invoice price variance tolerance"],
                "focus_fields": ["unit_price", "Robert'); DROP TABLE x;--"],
            }
        else:
            self.analyses += 1
            facts = request.prompt.split("<<<FACTS", 1)[1]
            rule = re.search(r"\[(D1\.R\d+)\] INV_PO_UNIT_PRICE", facts)
            passage = re.search(r"\[(K\d+)\] [^\n]*4\.1 Tolerance: ([^\n]+)", facts)
            assert rule is not None
            assert passage is not None
            quote = " ".join(passage.group(2).split()[:18])
            data = {
                "summary": "The invoice charges more than the order for one item.",
                "findings": [
                    {
                        "category": "RETRIEVED_KNOWLEDGE",
                        "statement": quote,
                        "evidence": [passage.group(1)],
                    },
                    {
                        "category": "AI_INFERENCE",
                        "statement": "The price difference exceeds what the policy tolerates.",
                        "evidence": [rule.group(1), passage.group(1)],
                    },
                    {
                        "category": "AI_INFERENCE",
                        "statement": "The difference is only 999.99.",
                        "evidence": [rule.group(1)],
                    },
                    {
                        "category": "AI_INFERENCE",
                        "statement": "The unit price check passes within tolerance.",
                        "evidence": [rule.group(1)],
                    },
                    {
                        "category": "AI_INFERENCE",
                        "statement": "Unsupported claim.",
                        "evidence": ["Z9"],
                    },
                ],
                "recommended_action": "APPROVE_FOR_PAYMENT",
                "rationale": "Looks acceptable.",
                "rationale_evidence": [rule.group(1)],
                "follow_up_questions": ["Who approves price variances?"]
                if self.analyses == 1
                else [],
            }
        return StructuredLLMResponse(
            data=schema.model_validate(data),
            raw_text="{}",
            usage=LLMUsage(provider="scripted", model="scripted-default", latency_ms=5),
        )


async def test_model_findings_are_validated_and_guardrails_overrule_the_model(
    env: Env, tmp_path: Path
) -> None:
    await seed_knowledge_base(env)
    ids = await processed(env, tmp_path, Scenario.UNIT_PRICE_MISMATCH, 21)
    model = AnalystLLM()
    data = await investigate(env, env.reviewer, "Can we pay this?", [ids["INV"]], llm=model)

    assert data["plan"]["source"] == "model"
    assert data["plan"]["focus_fields"] == ["unit_price"]  # the malformed name was dropped
    nodes = [step["node"] for step in data["trace"]]
    assert nodes.count("analyze") == 2  # one follow-up retrieval round
    assert nodes.count("retrieve_knowledge") == 2
    result = data["result"]
    assert result["summary_source"] == "model"
    assert result["model"] == {"provider": "scripted", "model": "scripted-default"}
    by_model = [f for f in result["findings"] if f["source"] == "model"]
    assert [(f["category"], f["grounded"]) for f in by_model] == [
        ("RETRIEVED_KNOWLEDGE", True),
        ("AI_INFERENCE", True),
    ]
    statements = " ".join(f["statement"] for f in result["findings"])
    assert "999.99" not in statements  # a number not in the evidence
    assert "passes within tolerance" not in statements  # contradicts the failed rule
    assert "Unsupported claim" not in statements  # cites nothing that exists
    assert any("contradicted a rule outcome" in notice for notice in result["notices"])
    analysis = next(f for f in result["confidence"]["factors"] if f["factor"] == "analysis")
    assert analysis["effect"] == -0.1

    recommendation = result["recommendation"]
    assert recommendation["action"] == "HOLD_FOR_REVIEW"
    assert recommendation["source"] == "rules"
    assert recommendation["guardrail_notes"] == [
        "The model proposed APPROVE_FOR_PAYMENT, not allowed: a rule failed, could not "
        "confirm a value, or a comparison found differences."
    ]
    # Prompts: facts inside per-request markers, untrusted-data instruction, no tools offered.
    plan_request, *analysis_requests = model.requests
    assert plan_request.purpose == "agent.plan"
    assert len(analysis_requests) == 2
    for request in analysis_requests:
        nonce = re.search(r"<<<FACTS ([0-9a-f]{16})", request.prompt)
        assert nonce is not None
        assert request.prompt.count(f"END FACTS {nonce.group(1)}>>>") == 1
        assert request.system_instruction is not None
        assert "untrusted data" in request.system_instruction
    async with env.maker() as session:
        calls = list(
            await session.scalars(
                select(LLMCall).where(LLMCall.agent_run_id == uuid.UUID(data["id"]))
            )
        )
    assert sorted(call.purpose for call in calls) == [
        "agent.analysis",
        "agent.analysis",
        "agent.plan",
    ]
    assert data["usage"]["llm_calls"] == 3


async def test_content_above_the_sensitivity_limit_is_not_sent_to_the_model(
    env: Env, tmp_path: Path
) -> None:
    ids = await processed(env, tmp_path, Scenario.UNIT_PRICE_MISMATCH, 21, "CONFIDENTIAL")
    model = AnalystLLM()
    data = await investigate(env, env.reviewer, "Can we pay this?", [ids["INV"]], llm=model)
    assert [request.purpose for request in model.requests] == ["agent.plan"]
    result = data["result"]
    assert result["summary_source"] == "rules"
    assert any("sensitivity limit" in notice for notice in result["notices"])
    assert result["recommendation"]["action"] == "HOLD_FOR_REVIEW"
