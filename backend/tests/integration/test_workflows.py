"""Workflows end to end (Modules 17, 18): the API, the worker running the steps, the
investigation, proposals, human approval with maker-checker, executors and reports."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from docintel.agent.runner import build_agent_deps
from docintel.ai.base import LLMRequest, LLMUsage, StructuredLLMResponse
from docintel.db.models import (
    AuditLog,
    ProcessingJob,
    ReviewRequest,
    ReviewTask,
    User,
    WorkflowAction,
    WorkflowActionTransition,
)
from docintel.processing.services import build_processing_services
from docintel.synthetic.contracts import generate_contract_versions
from docintel.synthetic.scenarios import Scenario
from docintel.workers.runner import Worker
from tests.conftest import auth_headers
from tests.factories.llm import ScriptedLLM
from tests.integration.conftest import Env
from tests.integration.test_agent_analysis import processed, worker
from tests.integration.test_knowledge_rag import hashing_embedder
from tests.integration.test_matching_api import bundle

pytestmark = pytest.mark.integration


async def start(env: Env, user: User, document_id: str, workflow_type: str) -> dict[str, Any]:
    response = await env.client.post(
        "/api/v1/workflows",
        json={"workflow_type": workflow_type, "document_id": document_id},
        headers=auth_headers(user),
    )
    assert response.status_code == 202, response.text
    assert response.headers["location"] == f"/api/v1/workflows/{response.json()['id']}"
    body: dict[str, Any] = response.json()
    return body


async def get(env: Env, workflow_id: str, user: User | None = None) -> dict[str, Any]:
    response = await env.client.get(
        f"/api/v1/workflows/{workflow_id}", headers=auth_headers(user or env.analyst)
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def run(env: Env, user: User, document_id: str, workflow_type: str) -> dict[str, Any]:
    """Start the workflow and let the worker run it until it needs a person or ends."""
    started = await start(env, user, document_id, workflow_type)
    assert started["status"] == "QUEUED"
    await worker(env).run_until_idle()
    return await get(env, started["id"], user)


async def decide(
    env: Env, user: User, workflow_id: str, verb: str, reason: str | None = None
) -> Any:
    return await env.client.post(
        f"/api/v1/workflows/{workflow_id}/{verb}",
        json={"reason": reason} if reason is not None else {},
        headers=auth_headers(user),
    )


def statuses(action: dict[str, Any]) -> list[str]:
    return [item["to_status"] for item in action["transitions"]]


def steps(workflow: dict[str, Any]) -> dict[str, str]:
    return {step["step_name"]: step["status"] for step in workflow["steps"]}


async def test_a_clean_invoice_is_approved_for_payment_by_someone_else(
    env: Env, tmp_path: Path
) -> None:
    clean = await processed(env, tmp_path, Scenario.CLEAN_MATCH, 22)
    workflow = await run(env, env.analyst, clean["INV"], "INVOICE_PROCESSING")
    assert workflow["status"] == "AWAITING_APPROVAL", workflow
    assert steps(workflow) == {
        "check_document": "COMPLETED",
        "investigate": "COMPLETED",
        "propose_action": "COMPLETED",
        "approval": "RUNNING",
        "execute_action": "PENDING",
        "report": "PENDING",
    }
    (action,) = workflow["actions"]
    assert (action["action_type"], action["status"], action["required_role"]) == (
        "APPROVE_FOR_PAYMENT",
        "AWAITING_APPROVAL",
        "MANAGER",
    )
    assert statuses(action) == ["PROPOSED", "AWAITING_APPROVAL"]
    assert action["payload"]["document"]["document_number"]
    # The starter sees why they cannot decide; the investigation is shown with the proposal.
    assert action["can_decide"] is False
    assert any("maker-checker" in reason for reason in action["blockers"])
    assert workflow["analysis"]["recommendation"]["action"] == "APPROVE_FOR_PAYMENT"
    assert workflow["pending_action"]["action_type"] == "APPROVE_FOR_PAYMENT"

    # Who may decide: analysts lack the permission, reviewers the role.
    assert (await decide(env, env.analyst, workflow["id"], "approve")).status_code == 403
    refused = await decide(env, env.reviewer, workflow["id"], "approve")
    assert refused.status_code == 403
    assert "must be decided by a MANAGER" in refused.json()["detail"]
    summary = await env.client.get("/api/v1/workflows/summary", headers=auth_headers(env.manager))
    assert summary.json() == {"awaiting_my_decision": 1}
    mine = await env.client.get(
        "/api/v1/workflows", params={"awaiting_me": "true"}, headers=auth_headers(env.reviewer)
    )
    assert mine.json()["total"] == 0

    approved = await decide(env, env.manager, workflow["id"], "approve", "Checked the PO.")
    assert approved.status_code == 200, approved.text
    done = approved.json()
    assert (done["status"], done["outcome"]) == ("COMPLETED", "APPROVED_FOR_PAYMENT")
    (action,) = done["actions"]
    assert statuses(action) == ["PROPOSED", "AWAITING_APPROVAL", "APPROVED", "EXECUTED"]
    assert action["decided_by"]["email"] == env.manager.email
    assert action["decision_reason"] == "Checked the PO."
    assert action["execution_result"]["decision"] == "APPROVED_FOR_PAYMENT"
    assert action["execution_result"]["payment_reference"].startswith("PAY-")
    assert steps(done)["report"] == "COMPLETED"
    assert len(done["report_ids"]) == 1
    # Decided once: a second decision conflicts.
    assert (await decide(env, env.admin, workflow["id"], "approve")).status_code == 409

    # Visible with the document; not to another department.
    assert (await get(env, workflow["id"], env.viewer))["status"] == "COMPLETED"
    hidden = await env.client.get(
        f"/api/v1/workflows/{workflow['id']}", headers=auth_headers(env.outsider)
    )
    assert hidden.status_code == 404

    async with env.maker() as session:
        events = list(
            await session.scalars(
                select(AuditLog.action)
                .where(AuditLog.details["workflow_id"].astext == workflow["id"])
                .order_by(AuditLog.id)
            )
        )
        assert [event for event in events if event.startswith("workflow.")] == [
            "workflow.action.proposed",
            "workflow.action.awaiting_approval",
            "workflow.approval_denied",
            "workflow.action.approved",
            "workflow.action.executed",
        ]
        # The investigation and the report are attributed to the workflow as well.
        assert {"analysis.completed", "report.generated"} <= set(events)
        lifecycle = list(
            await session.scalars(
                select(AuditLog.action)
                .where(AuditLog.entity_type == "workflow", AuditLog.entity_id == workflow["id"])
                .order_by(AuditLog.id)
            )
        )
        assert lifecycle == ["workflow.started", "workflow.completed"]


async def test_maker_checker_holds_for_managers_and_in_the_database(
    env: Env, tmp_path: Path
) -> None:
    clean = await processed(env, tmp_path, Scenario.CLEAN_MATCH, 22)
    workflow = await run(env, env.manager, clean["INV"], "INVOICE_PROCESSING")
    assert workflow["status"] == "AWAITING_APPROVAL"
    # The manager started it: they may not approve it. The analyst uploaded it.
    refused = await decide(env, env.manager, workflow["id"], "approve")
    assert refused.status_code == 403
    assert "You started this workflow" in refused.json()["detail"]
    (action,) = workflow["actions"]
    async with env.maker() as session:
        stored = await session.get(WorkflowAction, action["id"])
        assert stored is not None
        assert set(map(str, stored.maker_ids)) == {str(env.manager.id), str(env.analyst.id)}
    async with env.maker() as session:
        # Even a direct write cannot record a maker's decision.
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE workflow_actions SET decided_by_id = :maker, "
                        "decided_at = now(), status = 'APPROVED' WHERE id = :id"
                    ),
                    {"maker": env.manager.id, "id": action["id"]},
                )
    async with env.maker() as session:
        # History is append-only.
        with pytest.raises(DBAPIError, match="append-only"):
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE workflow_action_transitions SET reason = 'x' WHERE action_id = :id"
                    ),
                    {"id": action["id"]},
                )
    approved = await decide(env, env.admin, workflow["id"], "approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["outcome"] == "APPROVED_FOR_PAYMENT"


async def test_a_duplicate_is_proposed_for_rejection_and_the_approver_can_say_no(
    env: Env, tmp_path: Path
) -> None:
    duplicate = await processed(env, tmp_path, Scenario.DUPLICATE_INVOICE, 23)
    workflow = await run(env, env.analyst, duplicate["INV2"], "INVOICE_PROCESSING")
    (action,) = workflow["actions"]
    assert (action["action_type"], action["status"]) == ("REJECT_DUPLICATE", "AWAITING_APPROVAL")
    missing = await env.client.post(
        f"/api/v1/workflows/{workflow['id']}/reject",
        json={},
        headers=auth_headers(env.manager),
    )
    assert missing.status_code == 422  # a rejection needs a reason
    rejected = await decide(env, env.manager, workflow["id"], "reject", "Different delivery.")
    assert rejected.status_code == 200, rejected.text
    done = rejected.json()
    assert (done["status"], done["outcome"]) == ("REJECTED", "PROPOSAL_REJECTED")
    (action,) = done["actions"]
    assert statuses(action) == ["PROPOSED", "AWAITING_APPROVAL", "REJECTED"]
    assert steps(done)["execute_action"] == "SKIPPED"
    assert len(done["report_ids"]) == 1
    async with env.maker() as session:
        request = await session.scalar(
            select(ReviewRequest).where(ReviewRequest.document_id == duplicate["INV2"])
        )
        assert request is not None
        assert "rejected by" in request.reason
        assert "Different delivery." in request.reason


async def test_a_discrepancy_goes_to_review_without_waiting_for_approval(
    env: Env, tmp_path: Path
) -> None:
    mismatch = await processed(env, tmp_path, Scenario.UNIT_PRICE_MISMATCH, 21)
    workflow = await run(env, env.analyst, mismatch["INV"], "INVOICE_PROCESSING")
    assert (workflow["status"], workflow["outcome"]) == ("COMPLETED", "SENT_TO_REVIEW"), workflow
    (action,) = workflow["actions"]
    assert action["action_type"] == "HOLD_FOR_REVIEW"
    assert statuses(action) == ["PROPOSED", "APPROVED", "EXECUTED"]
    assert action["decided_by"] is None  # low risk: the risk table, not a person
    assert steps(workflow)["approval"] == "SKIPPED"
    async with env.maker() as session:
        task = await session.scalar(
            select(ReviewTask).where(
                ReviewTask.document_id == mismatch["INV"], ReviewTask.status == "OPEN"
            )
        )
        assert task is not None
        assert any(item["category"] == "REQUESTED" for item in task.reasons)
        transitions = list(
            await session.scalars(
                select(WorkflowActionTransition).where(
                    WorkflowActionTransition.action_id == action["id"]
                )
            )
        )
        assert [t.actor_type.value for t in transitions] == ["SYSTEM", "SYSTEM", "SYSTEM"]


async def test_starting_needs_a_processed_document_of_the_right_type(
    env: Env, tmp_path: Path
) -> None:
    clean = await processed(env, tmp_path, Scenario.CLEAN_MATCH, 22)
    wrong = await env.client.post(
        "/api/v1/workflows",
        json={"workflow_type": "CONTRACT_REVIEW", "document_id": clean["INV"]},
        headers=auth_headers(env.analyst),
    )
    assert wrong.status_code == 422
    assert "needs a contract" in wrong.json()["detail"]
    for user, expected in ((env.viewer, 403), (env.reviewer, 403), (env.outsider, 403)):
        response = await env.client.post(
            "/api/v1/workflows",
            json={"workflow_type": "INVOICE_PROCESSING", "document_id": clean["INV"]},
            headers=auth_headers(user),
        )
        assert response.status_code == expected
    first = await start(env, env.analyst, clean["INV"], "INVOICE_PROCESSING")
    again = await env.client.post(
        "/api/v1/workflows",
        json={"workflow_type": "INVOICE_PROCESSING", "document_id": clean["INV"]},
        headers=auth_headers(env.manager),
    )
    assert again.status_code == 409
    # Cancelled before the worker picks it up: the job is cancelled too.
    cancelled = await env.client.post(
        f"/api/v1/workflows/{first['id']}/cancel", json={}, headers=auth_headers(env.analyst)
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    async with env.maker() as session:
        job = await session.scalar(
            select(ProcessingJob).where(ProcessingJob.workflow_id == first["id"])
        )
        assert job is not None
        assert job.status.value == "CANCELLED"
    assert await worker(env).run_until_idle() == 0


# ------------------------------------------------------------------------------ contracts
async def contract(env: Env, tmp_path: Path, seed: int) -> tuple[str, list[bytes]]:
    """Version 1 of a synthetic contract family, uploaded and processed; all versions' files."""
    folder = tmp_path / f"contracts-{seed}"
    manifest = generate_contract_versions(folder, seed=seed, families=1)
    (family,) = manifest["contracts"]
    files = [(folder / name).read_bytes() for name in family["files"]]
    document_id = await env.upload(files[0], "supply-agreement.pdf")
    await worker(env).run_until_idle()
    return document_id, files


async def new_version(env: Env, document_id: str, content: bytes, number: int) -> None:
    response = await env.client.post(
        f"/api/v1/documents/{document_id}/versions",
        files={"file": (f"supply-agreement-v{number}.pdf", content, "application/pdf")},
        headers=auth_headers(env.analyst),
    )
    assert response.status_code == 201, response.text
    await worker(env).run_until_idle()


async def resolve_open_task(env: Env, document_id: str) -> None:
    """A reviewer accepts whatever the document's open review task lists."""
    findings = await env.client.get(
        f"/api/v1/documents/{document_id}/findings", headers=auth_headers(env.reviewer)
    )
    task = findings.json()["open_task"]
    if task is None:
        return
    resolved = await env.client.post(
        f"/api/v1/review-tasks/{task['id']}/resolve",
        json={"resolution": "APPROVED", "note": "Values checked."},
        headers=auth_headers(env.reviewer),
    )
    assert resolved.status_code == 200, resolved.text


async def test_contract_review_against_the_guidelines_and_previous_versions(
    env: Env, tmp_path: Path
) -> None:
    # Seed 37: versions 1 and 2 follow the guidelines (Ohio law, required clauses, notice
    # within 90 days); version 3 drops a required clause.
    document_id, files = await contract(env, tmp_path, 37)
    findings = await env.client.get(
        f"/api/v1/documents/{document_id}/findings", headers=auth_headers(env.analyst)
    )
    outcomes = {r["rule_code"]: r["outcome"] for r in findings.json()["rule_results"]}
    assert outcomes["CONTRACT_REQUIRED_CLAUSES"] == "PASS"
    assert outcomes["CONTRACT_GOVERNING_LAW"] == "PASS"
    assert outcomes["DOC_MANDATORY_FIELDS"] == "PASS"  # parties read from the preamble
    # Some extracted values are uncertain, so a person reviews them first: no approval yet.
    assert findings.json()["open_task"]["task_type"] == "EXTRACTION_REVIEW"
    held = await run(env, env.analyst, document_id, "CONTRACT_REVIEW")
    assert (held["status"], held["outcome"]) == ("COMPLETED", "SENT_TO_REVIEW"), held
    assert "open review task" in held["actions"][0]["rationale"]

    await resolve_open_task(env, document_id)
    first = await run(env, env.analyst, document_id, "CONTRACT_REVIEW")
    assert steps(first)["compare_versions"] == "SKIPPED"
    (action,) = first["actions"]
    assert (action["action_type"], action["required_role"]) == ("APPROVE_CONTRACT", "MANAGER")
    approved = await decide(env, env.manager, first["id"], "approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["outcome"] == "CONTRACT_APPROVED"

    await new_version(env, document_id, files[1], 2)
    await resolve_open_task(env, document_id)
    second = await run(env, env.analyst, document_id, "CONTRACT_REVIEW")
    compared = next(s for s in second["steps"] if s["step_name"] == "compare_versions")
    assert compared["status"] == "COMPLETED"
    assert (compared["output"]["from_version"], compared["output"]["to_version"]) == (1, 2)
    assert compared["output"]["changed"] >= 1
    (action,) = second["actions"]
    assert action["action_type"] == "APPROVE_CONTRACT"
    assert "changed" in action["rationale"]
    assert action["payload"]["version_changes"]["to_version"] == 2

    # Version 3 arrives while version 2 waits: the proposal is stale.
    await new_version(env, document_id, files[2], 3)
    stale = await decide(env, env.manager, second["id"], "approve")
    assert stale.status_code == 409
    assert "start a new workflow" in stale.json()["detail"]
    detail = await get(env, second["id"])
    assert detail["document"]["is_current_version"] is False
    rejected = await decide(env, env.manager, second["id"], "reject", "Superseded by version 3.")
    assert rejected.status_code == 200, rejected.text

    third = await run(env, env.analyst, document_id, "CONTRACT_REVIEW")
    assert (third["status"], third["outcome"]) == ("COMPLETED", "SENT_TO_LEGAL_REVIEW"), third
    (action,) = third["actions"]
    assert action["action_type"] == "REQUEST_LEGAL_REVIEW"
    assert "Required clause(s) missing" in action["rationale"]
    report = await env.client.get(
        f"/api/v1/reports/{third['report_ids'][0]}", headers=auth_headers(env.viewer)
    )
    content = report.json()["content"]
    assert "## Contract clauses" in content
    assert "### Changes since version 2" in content
    assert "CONTRACT_REQUIRED_CLAUSES" in content


# ------------------------------------------------------------------------------ executors
async def test_an_approval_fails_when_the_invoice_no_longer_qualifies(
    env: Env, tmp_path: Path
) -> None:
    clean = await processed(env, tmp_path, Scenario.CLEAN_MATCH, 22)
    workflow = await run(env, env.analyst, clean["INV"], "INVOICE_PROCESSING")
    assert workflow["status"] == "AWAITING_APPROVAL"
    # Meanwhile someone asks for a review: the invoice is no longer clear to pay.
    requested = await env.client.post(
        "/api/v1/review-tasks/requests",
        json={"document_id": clean["INV"], "reason": "Bank details changed.", "priority": "HIGH"},
        headers=auth_headers(env.reviewer),
    )
    assert requested.status_code == 201, requested.text
    response = await decide(env, env.manager, workflow["id"], "approve")
    assert response.status_code == 200, response.text
    done = response.json()
    assert (done["status"], done["outcome"]) == ("FAILED", "ACTION_FAILED")
    (action,) = done["actions"]
    assert statuses(action) == ["PROPOSED", "AWAITING_APPROVAL", "APPROVED", "FAILED"]
    assert "open review task" in action["error"]
    assert steps(done)["execute_action"] == "FAILED"
    assert steps(done)["report"] == "COMPLETED"  # the failure is reported too


class ClarifyingLLM(ScriptedLLM):
    """A model that proposes asking the vendor about a price difference."""

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredLLMResponse[T]:
        self.requests.append(request)
        if request.purpose == "agent.plan":
            data: dict[str, Any] = {
                "intent": "VERIFY_DOCUMENT",
                "document_query": None,
                "knowledge_questions": [],
                "focus_fields": ["unit_price"],
            }
        else:
            rule = re.search(r"\[(D1\.R\d+)\] INV_PO_UNIT_PRICE", request.prompt)
            assert rule is not None
            data = {
                "summary": "The invoiced unit price differs from the purchase order.",
                "findings": [],
                "recommended_action": "REQUEST_VENDOR_CLARIFICATION",
                "rationale": "The vendor should confirm which unit price applies.",
                "rationale_evidence": [rule.group(1)],
            }
        return StructuredLLMResponse(
            data=schema.model_validate(data),
            raw_text="{}",
            usage=LLMUsage(provider="scripted", model="scripted-default", latency_ms=5),
        )


async def test_a_model_proposal_to_ask_the_vendor_waits_for_a_reviewer(
    env: Env, tmp_path: Path
) -> None:
    mismatch = await processed(env, tmp_path, Scenario.UNIT_PRICE_MISMATCH, 21)
    started = await start(env, env.analyst, mismatch["INV"], "INVOICE_PROCESSING")
    await worker(env, llm=ClarifyingLLM()).run_until_idle()
    workflow = await get(env, started["id"])
    (action,) = workflow["actions"]
    assert (action["action_type"], action["proposed_by_type"], action["required_role"]) == (
        "REQUEST_VENDOR_CLARIFICATION",
        "AGENT",
        "REVIEWER",
    )
    transitions = action["transitions"]
    assert transitions[0]["actor_type"] == "AGENT"  # proposed by the model, for the analyst
    approved = await decide(env, env.reviewer, workflow["id"], "approve")
    assert approved.status_code == 200, approved.text
    done = approved.json()
    assert done["outcome"] == "AWAITING_VENDOR_CLARIFICATION"
    result = done["actions"][0]["execution_result"]
    assert result["sent"] is False
    assert result["message"].startswith("Dear ")
    assert "INV_PO_UNIT_PRICE" not in result["message"]  # the vendor sees the message only
    assert result["review_task_id"] is not None


# ------------------------------------------------------------------------------ auto-start
async def test_workflows_start_when_a_document_is_processed(env: Env, tmp_path: Path) -> None:
    settings = env.settings.model_copy(update={"workflow_auto_start": ["INVOICE_PROCESSING"]})
    docs = bundle(tmp_path, Scenario.UNIT_PRICE_MISMATCH, seed=24)
    for suffix, (content, _) in docs.items():
        await env.upload(content, f"AUTO-{suffix}.pdf")
    services = build_processing_services(
        settings, sessionmaker=env.maker, embedder=hashing_embedder()
    )
    agent = build_agent_deps(settings, env.maker, embedder=hashing_embedder())
    auto_worker = Worker(
        settings=settings,
        sessionmaker=env.maker,
        storage=env.storage,
        services=services,
        agent=agent,
    )
    await auto_worker.run_until_idle()
    listing = await env.client.get(
        "/api/v1/workflows",
        params={"workflow_type": "INVOICE_PROCESSING"},
        headers=auth_headers(env.viewer),
    )
    (workflow,) = [
        w for w in listing.json()["items"] if w["document"]["filename"] == "AUTO-INV.pdf"
    ]
    assert (workflow["trigger"], workflow["initiated_by"]["id"]) == ("AUTO", str(env.analyst.id))
    assert workflow["status"] == "COMPLETED"
    # Reprocessing the same version does not start another one.
    reprocess = await env.client.post(
        f"/api/v1/documents/{workflow['document']['id']}/process",
        headers=auth_headers(env.analyst),
    )
    assert reprocess.status_code == 202, reprocess.text
    await auto_worker.run_until_idle()
    again = await env.client.get(
        "/api/v1/workflows",
        params={"document_id": workflow["document"]["id"]},
        headers=auth_headers(env.viewer),
    )
    assert again.json()["total"] == 1
