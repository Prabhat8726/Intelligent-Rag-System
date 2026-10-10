"""Workflow policy, the action state machine, executors' texts and report rendering (pure)."""

from __future__ import annotations

import json
import random
import uuid
from typing import Any

import pytest

from docintel.agent.policy import ACTIONS
from docintel.agent.state import ActionType
from docintel.audit.service import SYSTEM_REQUEST
from docintel.db.models import (
    ActionRisk,
    ActionStatus,
    ActorType,
    ProposerType,
    Role,
    WorkflowAction,
    WorkflowActionType,
    WorkflowType,
)
from docintel.reports.render import normalized, render, sha256, text
from docintel.workflows.actions import TRANSITIONS, InvalidTransitionError, transition
from docintel.workflows.definitions import DEFINITIONS, REPORT
from docintel.workflows.executors import clarification_letter
from docintel.workflows.policy import ACTION_POLICIES, decide, role_may_approve

A = WorkflowActionType


def result(
    action: str, *, level: str = "HIGH", source: str = "rules", **extra: Any
) -> dict[str, Any]:
    return {
        "summary": "INV.pdf (invoice): checked.",
        "documents": [
            {
                "document_id": "d1",
                "filename": "INV.pdf",
                "document_type": "INVOICE",
                "document_number": "INV-1",
                "vendor_name": "Kestrel",
                "document_date": "2026-05-01",
                "total": "100.00",
                "currency": "USD",
            }
        ],
        "findings": [
            {
                "category": "RULE_RESULT",
                "statement": "Unit price differs (INV_PO_UNIT_PRICE, HIGH): FAIL - 5.00 vs 4.85.",
            },
            {"category": "RULE_RESULT", "statement": "All 12 applicable rules passed for X."},
        ],
        "confidence": {"level": level, "score": 0.9},
        "recommendation": {"action": action, "rationale": "Because.", "source": source},
        **extra,
    }


# ------------------------------------------------------------------------------ policy
def test_the_risk_table_agrees_with_the_agent() -> None:
    for action, policy in ACTIONS.items():
        if action == ActionType.NO_ACTION:
            continue
        mine = ACTION_POLICIES[WorkflowActionType(action.value)]
        assert mine.requires_approval == policy.requires_approval
        assert (mine.required_role.value if mine.required_role else None) == policy.required_role
        assert mine.risk.value == policy.risk.value
    for kind, rule in ACTION_POLICIES.items():
        assert rule.requires_approval == (rule.required_role is not None), kind
        assert rule.requires_approval == (rule.risk != ActionRisk.LOW), kind


@pytest.mark.parametrize(
    ("role", "required", "allowed"),
    [
        (Role.MANAGER, Role.MANAGER, True),
        (Role.ADMIN, Role.MANAGER, True),
        (Role.REVIEWER, Role.MANAGER, False),
        (Role.ANALYST, Role.MANAGER, False),
        (Role.REVIEWER, Role.REVIEWER, True),
        (Role.MANAGER, Role.REVIEWER, True),
        (Role.ANALYST, Role.REVIEWER, False),
        (Role.VIEWER, Role.REVIEWER, False),
    ],
)
def test_who_may_approve(role: Role, required: Role, allowed: bool) -> None:
    assert role_may_approve(role, required) is allowed


def test_invoice_proposals() -> None:
    invoice = WorkflowType.INVOICE_PROCESSING
    paid = decide(invoice, result("APPROVE_FOR_PAYMENT"), document_id="d1")
    assert paid is not None
    assert (paid.action_type, paid.proposer) == (A.APPROVE_FOR_PAYMENT, ProposerType.RULES)
    assert paid.payload["document"]["document_number"] == "INV-1"
    assert paid.payload["issues"] == [
        "Unit price differs (INV_PO_UNIT_PRICE, HIGH): FAIL - 5.00 vs 4.85."
    ]  # "All N rules passed" is not an issue
    model = decide(
        invoice, result("REQUEST_VENDOR_CLARIFICATION", source="model"), document_id="d1"
    )
    assert model is not None
    assert (model.action_type, model.proposer) == (
        A.REQUEST_VENDOR_CLARIFICATION,
        ProposerType.AGENT,
    )
    weak = decide(invoice, result("NO_ACTION", level="MEDIUM"), document_id="d1")
    assert weak is not None
    assert weak.action_type == A.HOLD_FOR_REVIEW  # a payment workflow always ends in a decision
    under_review = decide(
        invoice, result("APPROVE_FOR_PAYMENT"), document_id="d1", open_review="EXTRACTION_REVIEW"
    )
    assert under_review is not None
    assert under_review.action_type == A.HOLD_FOR_REVIEW
    assert "EXTRACTION_REVIEW" in under_review.rationale


def test_contract_proposals() -> None:
    contract = WorkflowType.CONTRACT_REVIEW
    changes = {"from_version": 1, "to_version": 2, "changed": 3}
    clean = decide(contract, result("NO_ACTION"), document_id="d1", version_changes=changes)
    assert clean is not None
    assert clean.action_type == A.APPROVE_CONTRACT
    assert "Version 2 changed 3 clause(s)" in clean.rationale
    deviating = decide(contract, result("HOLD_FOR_REVIEW"), document_id="d1")
    assert deviating is not None
    assert deviating.action_type == A.REQUEST_LEGAL_REVIEW
    assert "INV_PO_UNIT_PRICE" in deviating.rationale
    weak = decide(contract, result("NO_ACTION", level="LOW"), document_id="d1")
    assert weak is not None
    assert weak.action_type == A.HOLD_FOR_REVIEW
    duplicate = decide(contract, result("REJECT_DUPLICATE"), document_id="d1")
    assert duplicate is not None
    assert duplicate.action_type == A.REJECT_DUPLICATE


def test_definitions_end_with_their_report() -> None:
    for definition in DEFINITIONS.values():
        assert definition.steps[0] == "check_document"
        assert definition.steps[-1] == REPORT
        assert len(set(definition.steps)) == len(definition.steps)


# ------------------------------------------------------------------------------ state machine
def action(requires_approval: bool = True) -> WorkflowAction:
    return WorkflowAction(
        id=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        action_type=A.APPROVE_FOR_PAYMENT if requires_approval else A.HOLD_FOR_REVIEW,
        requires_approval=requires_approval,
        status=ActionStatus.PROPOSED,
    )


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, item: Any) -> None:
        self.added.append(item)


def test_the_transition_table() -> None:
    finished = {ActionStatus.REJECTED, ActionStatus.EXECUTED, ActionStatus.FAILED}
    assert all(status not in TRANSITIONS for status in finished)  # final states
    reachable = {to for targets in TRANSITIONS.values() for to in targets}
    assert reachable == set(ActionStatus)
    session = _Session()
    item = action()
    with pytest.raises(InvalidTransitionError, match="needs a person's approval"):
        transition(
            session,  # type: ignore[arg-type]
            item,
            ActionStatus.APPROVED,
            previous=ActionStatus.PROPOSED,
            actor=None,
            actor_type=ActorType.SYSTEM,
            reason=None,
            meta=SYSTEM_REQUEST,
        )
    with pytest.raises(InvalidTransitionError):
        transition(
            session,  # type: ignore[arg-type]
            item,
            ActionStatus.EXECUTED,
            previous=ActionStatus.AWAITING_APPROVAL,
            actor=None,
            actor_type=ActorType.SYSTEM,
            reason=None,
            meta=SYSTEM_REQUEST,
        )
    assert session.added == []  # nothing recorded for a refused transition
    low = action(requires_approval=False)
    transition(
        session,  # type: ignore[arg-type]
        low,
        ActionStatus.APPROVED,
        previous=ActionStatus.PROPOSED,
        actor=None,
        actor_type=ActorType.SYSTEM,
        reason="low risk",
        meta=SYSTEM_REQUEST,
    )
    assert low.status == ActionStatus.APPROVED
    assert len(session.added) == 2  # the transition row and the audit event


# ------------------------------------------------------------------------------ executors
def test_the_vendor_letter_quotes_messages_not_rule_codes() -> None:
    letter = clarification_letter(
        {"vendor_name": "Kestrel", "document_number": "INV-1", "document_date": "2026-05-01"},
        ["Unit price differs (INV_PO_UNIT_PRICE, HIGH): FAIL - Line 2: 5.00 vs 4.85 ordered."],
    )
    assert letter.startswith("Dear Kestrel,")
    assert "invoice INV-1 dated 2026-05-01" in letter
    assert "- Line 2: 5.00 vs 4.85 ordered." in letter
    assert "INV_PO_UNIT_PRICE" not in letter


# ------------------------------------------------------------------------------ rendering
def snapshot() -> dict[str, Any]:
    return {
        "template_version": 1,
        "report_type": "COMPLIANCE_REVIEW",
        "title": "Compliance review: a|b.pdf",
        "as_of": "2026-10-10T00:00:00+00:00",
        "documents": [
            {
                "id": "d1",
                "role": "subject",
                "filename": "a|b.pdf",
                "document_type": "INVOICE",
                "status": "COMPLETED",
                "sensitivity": "INTERNAL",
                "version_number": 1,
                "sha256": "0" * 64,
                "uploaded_by": "x@example.test",
                "uploaded_at": "2026-10-09T00:00:00+00:00",
            }
        ],
        "rules": [
            {
                "code": "R2",
                "name": "Second",
                "version": 1,
                "outcome": "PASS",
                "severity": "LOW",
                "message": "fine",
            },
            {
                "code": "R1",
                "name": "First",
                "version": 1,
                "outcome": "FAIL",
                "severity": "HIGH",
                "message": "[click](javascript:alert(1)) <script>x</script>",
            },
        ],
        "analysis": None,
        "reviews": {"tasks": [], "requests": []},
        "workflows": [],
    }


def shuffled(value: Any, rng: random.Random) -> Any:
    """The same data with every mapping's keys in another order (as JSONB may return it)."""
    if isinstance(value, dict):
        keys = list(value)
        rng.shuffle(keys)
        return {key: shuffled(value[key], rng) for key in keys}
    if isinstance(value, list):
        return [shuffled(item, rng) for item in value]
    return value


def test_rendering_is_deterministic_and_escapes_document_text() -> None:
    data = normalized(snapshot())
    content = render(data)
    for seed in range(5):
        assert render(shuffled(data, random.Random(seed))) == content
    assert render(json.loads(json.dumps(data))) == content
    assert len(sha256(content)) == 64
    # Failing rules first; text from documents cannot add links, HTML or table cells.
    assert content.index("| FAIL | First") < content.index("| PASS | Second")
    assert "\\[click\\](javascript:alert(1))" in content
    assert "<script>" not in content
    assert "a\\|b.pdf" in content
    assert text(None) == "-"
    assert text(0.5) == "0.50"
