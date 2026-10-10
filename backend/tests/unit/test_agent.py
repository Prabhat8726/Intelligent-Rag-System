"""Agent building blocks without a database: planning, validation of model output, guardrails,
confidence and the approval gate."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from docintel.agent.analysis import (
    Catalogue,
    ModelAnalysis,
    ModelFinding,
    build_catalogue,
    knowledge_references,
    validate_analysis,
)
from docintel.agent.planner import ModelPlan, rule_plan, search_phrase, validate_plan
from docintel.agent.policy import (
    assess_confidence,
    baseline_action,
    gate,
    guardrail,
    recommend,
    situation,
)
from docintel.agent.state import (
    ActionType,
    ConfidenceLevel,
    EvidenceItem,
    Intent,
    InvestigationState,
    Recommendation,
    RiskLevel,
)
from docintel.agent.tools.catalog import SearchDocumentsInput
from docintel.agent.tools.registry import effective_permissions, validation_message
from docintel.auth.permissions import Permission
from docintel.db.models import Role, User


# ------------------------------------------------------------------------------ planning
@pytest.mark.parametrize(
    ("query", "intent", "document_query"),
    [
        ("Can we pay the invoice from Kestrel Industrial Supply?", Intent.VERIFY_DOCUMENT,
         "invoice from Kestrel Industrial Supply"),
        ("Why does the Bluepeak invoice not match its purchase order?",
         Intent.INVESTIGATE_DISCREPANCY, "invoice from Bluepeak"),
        ("Is the Kestrel invoice from May 2026 a duplicate?", Intent.CHECK_DUPLICATE,
         "invoice in May 2026 from Kestrel"),
        ("Compare invoice INV-2026-0042 with its order", Intent.COMPARE_DOCUMENTS,
         "INV-2026-0042 invoice"),
        ("Find invoices from Harbor & Pine Packaging over 2,000", Intent.FIND_DOCUMENTS,
         "invoices from Harbor & Pine Packaging over 2,000"),
        ("Who must approve payment terms longer than 60 days?", Intent.POLICY_QUESTION, None),
        ("What is wrong with this invoice?", Intent.INVESTIGATE_DISCREPANCY, None),
        ("Above what amount do we need a purchase order?", Intent.POLICY_QUESTION, None),
        ("How fast must invoices be paid?", Intent.POLICY_QUESTION, None),
        ("What does Accounts Payable send to the Finance Manager at month-end?",
         Intent.POLICY_QUESTION, None),
        ("Anything new from Kestrel Industrial Supply?", Intent.VERIFY_DOCUMENT,
         "documents from Kestrel Industrial Supply"),
        ("Check PO-55012", Intent.VERIFY_DOCUMENT, "PO-55012"),
    ],
)  # fmt: skip
def test_keyword_plans(query: str, intent: Intent, document_query: str | None) -> None:
    plan = rule_plan(query, has_documents=False)
    assert plan.intent == intent
    assert plan.document_query == document_query
    assert plan.source == "rules"


def test_named_documents_are_never_searched_for() -> None:
    plan = rule_plan("Why does this invoice from Kestrel not match?", has_documents=True)
    assert plan.document_query is None
    assert search_phrase("Why is this wrong?", Intent.VERIFY_DOCUMENT) is None


def test_model_plans_are_bounded() -> None:
    output = ModelPlan(
        intent=Intent.VERIFY_DOCUMENT,
        document_query="x" * 1000,
        knowledge_questions=["a", "b", "c", "d", "  "],
        focus_fields=["unit_price", "Robert'); DROP TABLE x;--", "../etc", "total"],
    )
    plan = validate_plan(output, "query", has_documents=False)
    assert plan.source == "model"
    assert plan.document_query is not None
    assert len(plan.document_query) == 300
    assert plan.knowledge_questions == ["a", "b", "c"]
    assert plan.focus_fields == ["unit_price", "total"]
    with pytest.raises(ValidationError):
        ModelPlan.model_validate({"intent": "RUN_SHELL"})


# ------------------------------------------------------------------------------ state fixtures
def rule(code: str, outcome: str, severity: str = "HIGH", rule_type: str = "line_unit_price",
         **extra: Any) -> dict[str, Any]:  # fmt: skip
    return {
        "rule_code": code,
        "rule_name": code.replace("_", " ").title(),
        "rule_type": rule_type,
        "outcome": outcome,
        "severity": severity,
        "message": f"{code} {outcome.lower()}",
        "comparison_items": [],
        "stored_outcome": outcome,
        **extra,
    }


def state(
    results: list[dict[str, Any]],
    *,
    document_type: str = "INVOICE",
    review_level: str = "AUTO",
    duplicates: list[dict[str, Any]] | None = None,
    knowledge: list[dict[str, Any]] | None = None,
) -> InvestigationState:
    document = {
        "document_id": "d1",
        "filename": "INV.pdf",
        "document_type": document_type,
        "status": "COMPLETED",
        "vendor_name": "Kestrel",
        "document_date": "2026-05-01",
        "total": "100.00",
        "currency": "USD",
        "extraction": {"overall_confidence": 0.95, "review_level": review_level},
    }
    return {
        "query": "Can we pay this?",
        "plan": {"intent": "VERIFY_DOCUMENT"},
        "documents": {"d1": document},
        "document_roles": {"d1": "subject"},
        "identified_by": "request",
        "extractions": {
            "d1": {
                "fields": [
                    {
                        "field_path": "total",
                        "value": "100.00",
                        "confidence": 0.99,
                        "evidence_status": "VERIFIED",
                        "page": 1,
                        "required": True,
                    },
                ],
            }
        },
        "evidence": {},
        "rules": {
            "d1": {
                "evaluated": True,
                "results": results,
                "comparison": None,
                "duplicates": duplicates or [],
            }
        },
        "comparisons": [],
        "knowledge": knowledge or [],
    }


PASSING = [rule("INV_PO_UNIT_PRICE", "PASS"), rule("INV_PO_QUANTITY", "PASS")]
FAILING = [rule("INV_PO_UNIT_PRICE", "FAIL"), rule("INV_PO_QUANTITY", "PASS")]
QUANTITY_FAILING = [rule("INV_PO_UNIT_PRICE", "PASS"), rule("INV_PO_QUANTITY", "FAIL")]


# ------------------------------------------------------------------------------ guardrails
def test_payment_can_only_be_proposed_for_a_clean_confident_invoice() -> None:
    clean = situation(state(PASSING))
    assert guardrail(ActionType.APPROVE_FOR_PAYMENT, clean, ConfidenceLevel.HIGH) is None
    assert guardrail(ActionType.APPROVE_FOR_PAYMENT, clean, ConfidenceLevel.MEDIUM) == (
        "confidence is not HIGH"
    )
    failing = situation(state(FAILING))
    assert guardrail(ActionType.APPROVE_FOR_PAYMENT, failing, ConfidenceLevel.HIGH) == (
        "a rule failed, could not confirm a value, or a comparison found differences"
    )
    order = situation(state(PASSING, document_type="PURCHASE_ORDER"))
    assert guardrail(ActionType.APPROVE_FOR_PAYMENT, order, ConfidenceLevel.HIGH) == (
        "only invoices can be approved for payment"
    )
    assert guardrail(ActionType.NO_ACTION, failing, ConfidenceLevel.HIGH) is not None
    assert guardrail(ActionType.REJECT_DUPLICATE, failing, ConfidenceLevel.HIGH) == (
        "no duplicate is established"
    )
    assert guardrail(ActionType.REQUEST_VENDOR_CLARIFICATION, failing, ConfidenceLevel.HIGH) is None


def test_baseline_actions() -> None:
    assert baseline_action(situation(state(PASSING)), ConfidenceLevel.HIGH)[0] == (
        ActionType.APPROVE_FOR_PAYMENT
    )
    assert baseline_action(situation(state(PASSING)), ConfidenceLevel.LOW)[0] == (
        ActionType.HOLD_FOR_REVIEW
    )
    # Procedure 3.2/3.3: a confirmed price or tax difference is put to the vendor...
    action, reason = baseline_action(situation(state(FAILING)), ConfidenceLevel.HIGH)
    assert action == ActionType.REQUEST_VENDOR_CLARIFICATION
    assert "corrected invoice or a credit note" in reason
    tax = [rule("INV_PO_TAX_RATE", "FAIL", rule_type="header_match")]
    assert baseline_action(situation(state(tax)), ConfidenceLevel.MEDIUM)[0] == (
        ActionType.REQUEST_VENDOR_CLARIFICATION
    )
    # ...anything else failing, an unconfirmed value or weak evidence goes to a reviewer.
    for results, level in (
        (QUANTITY_FAILING, ConfidenceLevel.HIGH),
        ([*FAILING, rule("INV_PO_QUANTITY", "FAIL")], ConfidenceLevel.HIGH),
        ([*FAILING, rule("INV_DELIVERED_QUANTITY", "WARN")], ConfidenceLevel.HIGH),
        (FAILING, ConfidenceLevel.LOW),
    ):
        assert baseline_action(situation(state(results)), level)[0] == (
            ActionType.HOLD_FOR_REVIEW
        ), (results, level)
    duplicate = state(
        [rule("INV_DUPLICATE", "FAIL", rule_type="duplicate_document")],
        duplicates=[{"document_id": "d0", "kind": "SAME_NUMBER", "strong": True}],
    )
    assert baseline_action(situation(duplicate), ConfidenceLevel.HIGH)[0] == (
        ActionType.REJECT_DUPLICATE
    )
    nothing = state([])
    nothing["documents"], nothing["document_roles"] = {}, {}
    assert baseline_action(situation(nothing), ConfidenceLevel.LOW)[0] == ActionType.NO_ACTION


def test_a_model_proposal_stands_only_if_allowed() -> None:
    failing = state(QUANTITY_FAILING)
    confidence = assess_confidence(failing, dropped_model_findings=0)
    refused = recommend(
        failing, confidence, proposed=ActionType.APPROVE_FOR_PAYMENT, rationale="ok",
        rationale_evidence=[],
    )  # fmt: skip
    assert (refused.action, refused.source) == (ActionType.HOLD_FOR_REVIEW, "rules")
    assert refused.guardrail_notes[0].startswith("The model proposed APPROVE_FOR_PAYMENT")
    accepted = recommend(
        failing,
        confidence,
        proposed=ActionType.REQUEST_VENDOR_CLARIFICATION,
        rationale="Ask the vendor for a credit note [D1.R1].",
        rationale_evidence=["D1.R1"],
    )
    assert (accepted.action, accepted.source) == (ActionType.REQUEST_VENDOR_CLARIFICATION, "model")
    assert (accepted.risk, accepted.requires_approval) == (RiskLevel.MEDIUM, True)
    assert accepted.guardrail_notes == ["The rules alone would recommend HOLD_FOR_REVIEW."]


def test_the_approval_gate() -> None:
    def make(action: ActionType, approval: bool) -> Recommendation:
        return Recommendation(
            action=action, target_document_id="d1", rationale="r", risk=RiskLevel.LOW,
            requires_approval=approval, required_role=None, source="rules",
        )  # fmt: skip

    assert gate(make(ActionType.HOLD_FOR_REVIEW, False), allow_safe_actions=True) == "execute"
    assert gate(make(ActionType.HOLD_FOR_REVIEW, False), allow_safe_actions=False) == "none"
    assert gate(make(ActionType.APPROVE_FOR_PAYMENT, True), allow_safe_actions=True) == "propose"
    assert gate(make(ActionType.NO_ACTION, False), allow_safe_actions=True) == "none"


# ------------------------------------------------------------------------------ confidence
def test_confidence_counts_the_weakest_input_of_each_kind() -> None:
    clean = assess_confidence(state(PASSING), dropped_model_findings=0)
    assert (clean.level, clean.score, clean.factors) == (ConfidenceLevel.HIGH, 1.0, [])
    warned = state(
        [rule("A", "WARN"), rule("B", "WARN"), rule("C", "PASS")], review_level="ANALYST_REVIEW"
    )
    confidence = assess_confidence(warned, dropped_model_findings=2)
    effects = {factor.factor: factor.effect for factor in confidence.factors}
    assert effects == {"extraction": -0.15, "rule_warnings": -0.1, "analysis": -0.1}
    assert (confidence.level, confidence.score) == (ConfidenceLevel.MEDIUM, 0.65)


# ------------------------------------------------------------------------------ model output
PASSAGE = {
    "chunk_id": "c1",
    "title": "Procurement Policy",
    "version_label": "2026",
    "effective_from": "2026-01-01",
    "effective_to": None,
    "section_path": "4. Price variance",
    "content": "Invoice prices may exceed the order price by at most 2 percent.",
    "sensitivity": "INTERNAL",
}


def catalogue() -> Catalogue:
    return build_catalogue(state(FAILING, knowledge=[PASSAGE]))


def analyze(*findings: ModelFinding, summary: str = "A price differs.") -> Any:
    output = ModelAnalysis(
        summary=summary,
        findings=list(findings),
        recommended_action=ActionType.HOLD_FOR_REVIEW,
    )
    return validate_analysis(output, catalogue(), {"K1"})


def test_model_findings_must_cite_known_evidence_and_keep_to_its_numbers() -> None:
    labels = {item.label for item in catalogue().items}
    assert {"D1", "D1.F1", "D1.R1", "D1.R2", "K1"} <= labels
    result = analyze(
        ModelFinding(category="RETRIEVED_KNOWLEDGE",
                     statement="Invoice prices may exceed the order price by at most 2 percent.",
                     evidence=["[k1]"]),
        ModelFinding(category="RETRIEVED_KNOWLEDGE",
                     statement="Prices may exceed by 5 percent.", evidence=["K1"]),
        ModelFinding(category="AI_INFERENCE", statement="The total is 100.00.",
                     evidence=["D1.F1"]),
        ModelFinding(category="AI_INFERENCE", statement="The total is 250.00.",
                     evidence=["D1.F1"]),
        ModelFinding(category="AI_INFERENCE", statement="Something.", evidence=["X7"]),
        ModelFinding(category="AI_INFERENCE", statement="The unit price matches.",
                     evidence=["D1.R1"]),
        ModelFinding(category="AI_INFERENCE", statement="No issues were found.",
                     evidence=["D1.R2"]),
    )  # fmt: skip
    kept = [(f.category.value, f.statement, f.grounded) for f in result.findings]
    assert kept == [
        ("RETRIEVED_KNOWLEDGE",
         "Invoice prices may exceed the order price by at most 2 percent.", True),
        ("RETRIEVED_KNOWLEDGE", "Prices may exceed by 5 percent.", False),
        ("AI_INFERENCE", "The total is 100.00.", True),
    ]  # fmt: skip
    assert result.dropped == 4
    assert result.findings[0].evidence == ["K1"]


def test_unsafe_summaries_are_replaced() -> None:
    assert analyze(summary="Total 100.00, one price differs.").summary is not None
    assert analyze(summary="Total 999.00.").summary is None
    assert analyze(summary="Everything is fine: approved for payment.").summary is None


def test_withheld_passages_cannot_be_cited() -> None:
    output = ModelAnalysis(
        summary="s",
        findings=[ModelFinding(category="RETRIEVED_KNOWLEDGE", statement="x", evidence=["K1"])],
        recommended_action=ActionType.NO_ACTION,
    )
    assert validate_analysis(output, catalogue(), set()).findings == []


def test_policy_references_read_as_sentences() -> None:
    faq = {**PASSAGE, "chunk_id": "c2", "title": "AP FAQ", "section_path": "Q6. Can I convert it?"}
    investigation = state(FAILING, knowledge=[PASSAGE, faq])
    statements = [
        finding.statement
        for finding in knowledge_references(investigation, build_catalogue(investigation), set())
    ]
    assert statements == [
        "Relevant guidance: Procurement Policy - 4. Price variance.",
        "Relevant guidance: AP FAQ - Q6. Can I convert it?",
    ]


def test_catalogue_items_are_typed() -> None:
    kinds = {item.kind for item in catalogue().items}
    assert kinds == {"DOCUMENT", "FIELD", "RULE", "KNOWLEDGE"}
    assert all(isinstance(item, EvidenceItem) for item in catalogue().items)


# ------------------------------------------------------------------------------ tool registry
def test_token_scopes_only_narrow_a_role() -> None:
    reviewer = User(role=Role.REVIEWER)
    assert effective_permissions(reviewer, None) >= {Permission.REVIEWS_WORK}
    narrowed = effective_permissions(reviewer, frozenset({"documents:read", "users:manage"}))
    assert narrowed == {Permission.DOCUMENTS_READ}


def test_validation_messages_never_echo_values() -> None:
    with pytest.raises(ValidationError) as caught:
        SearchDocumentsInput.model_validate({"query": "secret 4111 1111", "limit": 99, "x": 1})
    message = validation_message(caught.value)
    assert "limit: Input should be less than or equal to 20" in message
    assert "x: Extra inputs are not permitted" in message
    assert "4111" not in message
