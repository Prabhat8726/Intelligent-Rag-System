"""The controlled tools (Module 15) against processed synthetic documents: schemas,
permissions, access scoping, side effects and the call log."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update

from docintel.agent.tools import (
    NOT_FOUND,
    Caller,
    ToolEnvironment,
    ToolRegistry,
    ToolResult,
    build_registry,
)
from docintel.db.models import (
    AgentToolCall,
    AuditLog,
    Comparison,
    ComparisonOrigin,
    RuleResultRecord,
    ToolCallStatus,
    ToolChannel,
    User,
)
from docintel.synthetic.scenarios import Scenario
from tests.conftest import auth_headers
from tests.integration.conftest import Env
from tests.integration.test_knowledge_rag import seed_knowledge_base
from tests.integration.test_matching_api import bundle, findings

pytestmark = pytest.mark.integration


def registry(env: Env) -> ToolRegistry:
    return build_registry(ToolEnvironment(env.settings, env.maker, env.app.state.rag))


def ok(result: ToolResult) -> dict[str, Any]:
    """The output of a successful call."""
    assert result.status == ToolCallStatus.SUCCEEDED, result.error
    assert result.output is not None
    return result.output


def as_user(user: User, via: ToolChannel = ToolChannel.MCP, **kwargs: Any) -> Caller:
    return Caller(user_id=user.id, via=via, **kwargs)


async def processed_invoice(env: Env, tmp_path: Path) -> dict[str, str]:
    docs = bundle(tmp_path, Scenario.UNIT_PRICE_MISMATCH)
    ids = {suffix: await env.upload(docs[suffix][0], f"{suffix}.pdf") for suffix in ("PO", "DN")}
    ids["INV"] = await env.upload(docs["INV"][0], "INV.pdf")
    assert await env.worker().run_until_idle() == 3
    return ids


async def counts(env: Env) -> tuple[int, int]:
    async with env.maker() as session:
        comparisons = await session.scalar(select(func.count()).select_from(Comparison))
        results = await session.scalar(select(func.count()).select_from(RuleResultRecord))
    return int(comparisons or 0), int(results or 0)


async def test_read_tools_return_typed_scoped_results(env: Env, tmp_path: Path) -> None:
    ids = await processed_invoice(env, tmp_path)
    tools = registry(env)
    assert tools.names() == [
        "search_documents",
        "get_document",
        "get_extracted_fields",
        "get_document_evidence",
        "search_knowledge_base",
        "compare_documents",
        "run_business_rules",
        "create_review_task",
    ]
    viewer = as_user(env.viewer)

    found = await tools.call("search_documents", {"query": "invoices", "limit": 5}, viewer)
    found_out = ok(found)
    assert [hit["document_id"] for hit in found_out["results"]] == [ids["INV"]]
    assert found_out["understood_as"] == ["type INVOICE"]

    document = await tools.call("get_document", {"document_id": ids["INV"]}, viewer)
    document_out = ok(document)
    assert document_out["document_type"] == "INVOICE"
    assert document_out["status"] == "REVIEW_REQUIRED"
    assert document_out["open_review_task"]["task_type"] == "DISCREPANCY_REVIEW"
    assert document_out["extraction"]["schema_name"] == "invoice"
    assert document_out["effective_sensitivity"] in ("INTERNAL", "CONFIDENTIAL")

    fields = await tools.call(
        "get_extracted_fields",
        {"document_id": ids["INV"], "field_paths": ["total", "invoice_number", "no_such_field"]},
        viewer,
    )
    fields_out = ok(fields)
    assert {item["field_path"] for item in fields_out["fields"]} == {"total", "invoice_number"}
    assert fields_out["missing_paths"] == ["no_such_field"]
    total = next(item for item in fields_out["fields"] if item["field_path"] == "total")
    assert total["value"]
    assert total["page"] == 1

    evidence = await tools.call(
        "get_document_evidence", {"document_id": ids["INV"], "field_path": "total"}, viewer
    )
    evidence_out = ok(evidence)
    assert evidence_out["page_number"] == 1
    assert evidence_out["source_text"]
    assert len(evidence_out["bbox"]) == 4

    # The same calls by someone outside the department: "not found", never details.
    outsider = as_user(env.outsider)
    for name, arguments in (
        ("get_document", {"document_id": ids["INV"]}),
        ("get_extracted_fields", {"document_id": ids["INV"]}),
        ("get_document_evidence", {"document_id": ids["INV"], "field_path": "total"}),
        ("run_business_rules", {"document_ids": [ids["INV"]]}),
    ):
        denied = await tools.call(name, arguments, outsider)
        assert (denied.status, denied.error, denied.output) == (
            ToolCallStatus.FAILED,
            NOT_FOUND,
            None,
        ), name
    hidden = await tools.call("search_documents", {"query": "invoices"}, outsider)
    assert ok(hidden)["results"] == []


async def test_run_business_rules_evaluates_without_storing(env: Env, tmp_path: Path) -> None:
    ids = await processed_invoice(env, tmp_path)
    before = await counts(env)
    result = await registry(env).call(
        "run_business_rules", {"document_ids": [ids["INV"], ids["PO"]]}, as_user(env.viewer)
    )
    result_out = ok(result)
    invoice, order = result_out["documents"]
    outcomes = {item["rule_code"]: item["outcome"] for item in invoice["results"]}
    assert outcomes["INV_PO_UNIT_PRICE"] == "FAIL"
    assert all(item["stored_outcome"] == item["outcome"] for item in invoice["results"])
    comparison = invoice["comparison"]
    assert comparison["comparison_type"] == "INVOICE_PO_DELIVERY"
    assert set(comparison["counterpart_ids"]) == {ids["PO"], ids["DN"]}
    (issue,) = comparison["issues"]
    assert (issue["check"], issue["status"]) == ("unit_price", "MISMATCH")
    assert not {item["outcome"] for item in order["results"]} & {"FAIL", "ERROR"}
    assert await counts(env) == before  # a dry run: nothing replaced or added


async def test_compare_documents_records_a_manual_comparison(env: Env, tmp_path: Path) -> None:
    ids = await processed_invoice(env, tmp_path)
    tools = registry(env)
    arguments = {
        "documents": [
            {"document_id": ids["INV"], "role": "INVOICE"},
            {"document_id": ids["PO"], "role": "PURCHASE_ORDER"},
        ]
    }
    denied = await tools.call("compare_documents", arguments, as_user(env.viewer))
    assert denied.status == ToolCallStatus.DENIED
    assert denied.error == (
        "Not permitted: compare_documents needs the comparisons:create permission."
    )

    wrong_role = await tools.call(
        "compare_documents",
        {"documents": [{**arguments["documents"][0], "role": "PURCHASE_ORDER"},
                       arguments["documents"][1]]},
        as_user(env.reviewer),
    )  # fmt: skip
    assert wrong_role.status == ToolCallStatus.FAILED
    assert wrong_role.error == "INV.pdf is not a purchase order."

    compared = await tools.call("compare_documents", arguments, as_user(env.reviewer))
    compared_out = ok(compared)
    assert compared_out["comparison_type"] == "INVOICE_PO"
    assert compared_out["summary"]["MISMATCH"] == 1
    assert compared_out["items"][0]["status"] == "MISMATCH"  # problems first
    async with env.maker() as session:
        stored = await session.get(Comparison, uuid.UUID(compared_out["comparison_id"]))
        assert stored is not None
        assert stored.origin == ComparisonOrigin.MANUAL
        assert stored.requested_by_id == env.reviewer.id


async def test_create_review_task_survives_reevaluation_until_resolved(
    env: Env, tmp_path: Path
) -> None:
    ids = await processed_invoice(env, tmp_path)
    assert (await env.detail(ids["PO"]))["status"] == "COMPLETED"
    tools = registry(env)
    arguments = {
        "document_id": ids["PO"],
        "reason": "Confirm the unit price of the mismatched line with the buyer.",
        "priority": "HIGH",
    }
    assert (await tools.call("create_review_task", arguments, as_user(env.viewer))).status == (
        ToolCallStatus.DENIED
    )
    urgent = await tools.call(
        "create_review_task", {**arguments, "priority": "URGENT"}, as_user(env.reviewer)
    )
    assert urgent.status == ToolCallStatus.FAILED
    assert urgent.error == "Request a review with priority HIGH, NORMAL or LOW."

    first = await tools.call("create_review_task", arguments, as_user(env.reviewer))
    first_out = ok(first)
    assert first_out["created"] is True
    assert (first_out["task_status"], first_out["task_priority"]) == ("OPEN", "HIGH")
    assert first_out["document_status"] == "REVIEW_REQUIRED"
    again = ok(await tools.call("create_review_task", arguments, as_user(env.reviewer)))
    assert (again["created"], again["task_id"]) == (False, first_out["task_id"])

    data = await findings(env, ids["PO"])
    (reason,) = data["open_task"]["reasons"]
    assert reason["category"] == "REQUESTED"
    assert reason["message"] == arguments["reason"]
    assert data["open_task"]["task_type"] == "REQUESTED_REVIEW"

    # Re-evaluating the rules keeps the request on the task...
    evaluate = await env.client.post(
        "/api/v1/rules/evaluate",
        json={"document_ids": [ids["PO"]]},
        headers=auth_headers(env.analyst),
    )
    assert evaluate.status_code == 200, evaluate.text
    data = await findings(env, ids["PO"])
    assert data["open_task"]["id"] == first_out["task_id"]
    # ...until a person resolves it; then it stays resolved.
    resolved = await env.client.post(
        f"/api/v1/review-tasks/{first_out['task_id']}/resolve",
        json={"resolution": "APPROVED", "note": "Price confirmed."},
        headers=auth_headers(env.reviewer),
    )
    assert resolved.status_code == 200, resolved.text
    await env.client.post(
        "/api/v1/rules/evaluate",
        json={"document_ids": [ids["PO"]]},
        headers=auth_headers(env.analyst),
    )
    data = await findings(env, ids["PO"])
    assert data["open_task"] is None
    assert (await env.detail(ids["PO"]))["status"] == "COMPLETED"
    async with env.maker() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "review.requested", AuditLog.entity_id == ids["PO"]
            )
        )
        assert audit is not None
        assert audit.details["reason_chars"] == len(arguments["reason"])
        assert arguments["reason"] not in str(audit.details)


async def test_invalid_calls_are_rejected_and_every_call_is_logged(
    env: Env, tmp_path: Path
) -> None:
    tools = registry(env)
    caller = as_user(env.reviewer)
    document_id = str(uuid.uuid4())
    cases: list[tuple[str, Any, ToolCallStatus, str]] = [
        ("run_shell", {"command": "ls"}, ToolCallStatus.INVALID, "Unknown tool 'run_shell'."),
        (
            "get_document",
            {"document_id": document_id, "sql": "DROP TABLE documents"},
            ToolCallStatus.INVALID,
            "sql: Extra inputs are not permitted",
        ),
        (
            "search_documents",
            {"query": "invoices\x00"},
            ToolCallStatus.INVALID,
            "query: Value error, must not contain control characters",
        ),
        (
            "search_documents",
            {"query": "invoices", "limit": 500},
            ToolCallStatus.INVALID,
            "limit: Input should be less than or equal to 20",
        ),
        (
            "get_document_evidence",
            {"document_id": document_id, "field_path": "total; DELETE"},
            ToolCallStatus.INVALID,
            "field_path: String should match pattern",
        ),
        ("get_document", "not an object", ToolCallStatus.INVALID, "arguments: Input should be"),
        ("get_document", {"document_id": document_id}, ToolCallStatus.FAILED, NOT_FOUND),
    ]
    for name, arguments, status, message in cases:
        result = await tools.call(name, arguments, caller)
        assert result.status == status, (name, result.error)
        assert result.error is not None
        assert result.error.startswith(message), result.error
        assert "DROP TABLE" not in result.error  # rejected values are never echoed

    # A token scope narrows the role: knowledge search needs knowledge:read.
    scoped = as_user(env.reviewer, scopes=frozenset({"documents:read"}))
    narrowed = await tools.call("search_knowledge_base", {"query": "payment terms"}, scoped)
    assert narrowed.status == ToolCallStatus.DENIED
    # A deactivated user can no longer call anything.
    async with env.maker() as session, session.begin():
        await session.execute(update(User).where(User.id == env.viewer.id).values(is_active=False))
    gone = await tools.call("search_documents", {"query": "invoices"}, as_user(env.viewer))
    assert (gone.status, gone.error) == (ToolCallStatus.DENIED, "Not permitted.")

    async with env.maker() as session:
        rows = list(
            await session.scalars(
                select(AgentToolCall)
                .where(AgentToolCall.actor_id.in_([env.reviewer.id, env.viewer.id]))
                .order_by(AgentToolCall.created_at)
            )
        )
        assert [row.status for row in rows] == [
            *(status for _, _, status, _ in cases),
            ToolCallStatus.DENIED,
            ToolCallStatus.DENIED,
        ]
        assert all(row.via == ToolChannel.MCP and row.run_id is None for row in rows)
        assert (rows[0].tool_name, rows[0].arguments) == ("run_shell", {})
        assert rows[-3].arguments == {"document_id": document_id}  # validated arguments only
        audited = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "mcp.tool_called", AuditLog.actor_id == env.reviewer.id)
        )
        assert audited == len(cases) + 1


async def test_knowledge_search_tool_respects_departments(env: Env, tmp_path: Path) -> None:
    keys = await seed_knowledge_base(env)
    tools = registry(env)
    question = {"query": "concessions on limitation of liability negotiation", "top_k": 10}
    legal = await tools.call("search_knowledge_base", question, as_user(env.outsider))
    finance = await tools.call("search_knowledge_base", question, as_user(env.reviewer))
    legal_out, finance_out = ok(legal), ok(finance)
    playbook = "Legal Negotiation Playbook"
    assert any(p["title"].startswith(playbook) for p in legal_out["passages"])
    assert not any(p["title"].startswith(playbook) for p in finance_out["passages"])
    assert all(len(p["content"]) <= 1500 for p in legal_out["passages"])
    assert keys  # seeded under test-specific keys
