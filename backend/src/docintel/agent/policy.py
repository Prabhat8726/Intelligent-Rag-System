"""determine_confidence, recommend and approval_gate: deterministic policy.

Confidence is computed from what the tools returned - never claimed by a model. The
recommendation is one of an allowlist of actions; a model may propose one, and guardrails
decide whether the proposal stands (e.g. an invoice with a failed rule can never be
recommended for payment). The risk table decides which actions need a person's approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from docintel.agent.analysis import ATTENTION, subjects
from docintel.agent.state import (
    ActionType,
    Confidence,
    ConfidenceFactor,
    ConfidenceLevel,
    Intent,
    InvestigationState,
    Recommendation,
    RiskLevel,
)

HIGH_SEVERITIES = ("HIGH", "CRITICAL")
# Rule types that compare the document with its counterparts (a vendor can explain them).
DISCREPANCY_TYPES = frozenset(
    {
        "line_unit_price",
        "line_quantity_ordered",
        "line_quantity_delivered",
        "line_not_ordered",
        "header_match",
    }
)


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    risk: RiskLevel
    requires_approval: bool
    required_role: str | None


ACTIONS: dict[ActionType, ActionPolicy] = {
    ActionType.APPROVE_FOR_PAYMENT: ActionPolicy(RiskLevel.HIGH, True, "MANAGER"),
    ActionType.REJECT_DUPLICATE: ActionPolicy(RiskLevel.HIGH, True, "MANAGER"),
    ActionType.REQUEST_VENDOR_CLARIFICATION: ActionPolicy(RiskLevel.MEDIUM, True, "REVIEWER"),
    ActionType.HOLD_FOR_REVIEW: ActionPolicy(RiskLevel.LOW, False, None),
    ActionType.NO_ACTION: ActionPolicy(RiskLevel.NONE, False, None),
}


@dataclass(slots=True)
class Situation:
    """What the evidence says, in the terms the guardrails need."""

    target: str | None  # the subject document an action would apply to
    target_type: str | None
    evaluated: bool
    failing: list[dict[str, Any]]
    attention: list[dict[str, Any]]  # WARN and ERROR
    severe: bool
    strong_duplicate: bool
    discrepancy: bool
    comparison_issues: int  # differences found by a comparison the request asked for


def situation(state: InvestigationState) -> Situation:
    documents = subjects(state)
    worst: tuple[int, str | None, str | None] = (-1, None, None)
    failing: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    evaluated = False
    strong_duplicate = False
    for doc_id, document in documents:
        rules = state.get("rules", {}).get(doc_id) or {}
        evaluated = evaluated or bool(rules.get("evaluated"))
        results = rules.get("results", [])
        doc_failing = [r for r in results if r["outcome"] == "FAIL"]
        failing.extend(doc_failing)
        attention.extend(r for r in results if r["outcome"] in ("WARN", "ERROR"))
        duplicate = any(d["strong"] for d in rules.get("duplicates", [])) or any(
            r["rule_type"] == "duplicate_document" and r["outcome"] == "FAIL" for r in results
        )
        strong_duplicate = strong_duplicate or duplicate
        weight = 2 * len(doc_failing) + sum(1 for r in results if r["outcome"] in ATTENTION)
        if weight > worst[0]:
            worst = (weight, doc_id, document.get("document_type"))
    comparison_issues = sum(
        count
        for comparison in state.get("comparisons", [])
        for status, count in comparison["summary"].items()
        if status in ("MISMATCH", "MISSING")
    )
    if comparison_issues and not failing:
        # The subject the comparison is about: its invoice, else its first member.
        members = [m for c in state.get("comparisons", []) for m in c["members"]]
        lead = next((m for m in members if m["role"] == "INVOICE"), members[0] if members else None)
        if lead is not None:
            documents_by_id = dict(documents)
            worst = (
                0,
                lead["document_id"],
                (documents_by_id.get(lead["document_id"]) or {}).get("document_type"),
            )
    return Situation(
        target=worst[1],
        target_type=worst[2],
        evaluated=evaluated,
        failing=failing,
        attention=attention,
        severe=any(r["severity"] in HIGH_SEVERITIES for r in failing),
        strong_duplicate=strong_duplicate,
        discrepancy=any(r["rule_type"] in DISCREPANCY_TYPES for r in failing)
        or comparison_issues > 0,
        comparison_issues=comparison_issues,
    )


# ------------------------------------------------------------------------------ confidence
def assess_confidence(state: InvestigationState, *, dropped_model_findings: int) -> Confidence:
    """1.0 minus one penalty per kind of weakness - the weakest input of each kind counts (a
    conclusion is as reliable as its weakest document, not the sum of all of them)."""
    worst: dict[str, ConfidenceFactor] = {}

    def factor(name: str, effect: float, detail: str) -> None:
        if name not in worst or effect < worst[name].effect:
            worst[name] = ConfidenceFactor(factor=name, effect=effect, detail=detail)

    intent = state.get("plan", {}).get("intent")
    documents = subjects(state)
    if not documents and intent != Intent.POLICY_QUESTION:
        factor("identification", -1.0, "No document was identified.")
    elif state.get("identified_by") == "search":
        factor("identification", -0.1, "Documents were identified by search, not named.")
    for doc_id, document in documents:
        name = document["filename"]
        extraction = document.get("extraction")
        if extraction is None:
            factor("extraction", -0.3, f"No extracted fields for {name}.")
        elif extraction["review_level"] == "MANDATORY_REVIEW":
            factor("extraction", -0.3, f"The extraction of {name} requires review.")
        elif extraction["review_level"] == "ANALYST_REVIEW":
            factor(
                "extraction",
                -0.15,
                f"Some extracted values of {name} are uncertain "
                f"(overall confidence {float(extraction['overall_confidence']):.2f}).",
            )
        weak = [
            e for e in state.get("evidence", {}).get(doc_id, [])
            if e["evidence_status"] in ("NOT_FOUND", "UNVERIFIED", "INFERRED")
        ]  # fmt: skip
        if weak:
            factor("evidence", -0.1, f"Key values of {name} are not backed by a verified quote.")
        rules = state.get("rules", {}).get(doc_id) or {}
        if not rules.get("evaluated"):
            factor("rules", -0.2, f"The rules could not be evaluated for {name}.")
            continue
        errors = [r for r in rules["results"] if r["outcome"] == "ERROR"]
        warnings = [r for r in rules["results"] if r["outcome"] == "WARN"]
        if errors:
            factor("rules", -0.2, f"{len(errors)} rule(s) could not decide (ERROR) for {name}.")
        if warnings:
            factor(
                "rule_warnings",
                -0.1,
                f"{len(warnings)} rule(s) could not confirm a value (WARN) for {name}.",
            )
        uncertain = [
            i for i in (rules.get("comparison") or {}).get("issues", [])
            if i["status"] == "UNCERTAIN"
        ]  # fmt: skip
        if uncertain:
            factor("comparison", -0.1, f"{len(uncertain)} comparison item(s) are uncertain.")
        changed = [
            r for r in rules["results"]
            if r["stored_outcome"] is not None and r["stored_outcome"] != r["outcome"]
        ]  # fmt: skip
        if changed:
            factor(
                "freshness",
                -0.05,
                f"{len(changed)} outcome(s) differ from the last stored evaluation "
                "(data or rules changed since).",
            )
    needs_policy = intent == Intent.POLICY_QUESTION or bool(situation(state).failing)
    passages = [p for p in state.get("knowledge", []) if p.get("relevant", True)]
    if needs_policy and not passages:
        factor("knowledge", -0.1, "No policy passage in force was found for the question.")
    if dropped_model_findings:
        factor(
            "analysis",
            -0.1,
            f"{dropped_model_findings} model statement(s) failed validation and were removed.",
        )
    factors = list(worst.values())
    score = max(0.0, min(1.0, 1.0 + sum(f.effect for f in factors)))
    level = (
        ConfidenceLevel.HIGH
        if score >= 0.8
        else ConfidenceLevel.MEDIUM
        if score >= 0.55
        else ConfidenceLevel.LOW
    )
    return Confidence(level=level, score=round(score, 2), factors=factors)


# ------------------------------------------------------------------------------ recommendation
def guardrail(action: ActionType, facts: Situation, confidence: ConfidenceLevel) -> str | None:
    """Why `action` is not allowed here (None: allowed)."""
    if action == ActionType.APPROVE_FOR_PAYMENT:
        if facts.target_type != "INVOICE":
            return "only invoices can be approved for payment"
        if not facts.evaluated:
            return "the rules have not been evaluated"
        if facts.failing or facts.attention or facts.comparison_issues:
            return "a rule failed, could not confirm a value, or a comparison found differences"
        if facts.strong_duplicate:
            return "the invoice may be a duplicate"
        if confidence != ConfidenceLevel.HIGH:
            return "confidence is not HIGH"
    elif action == ActionType.REJECT_DUPLICATE:
        if not facts.strong_duplicate:
            return "no duplicate is established"
    elif action == ActionType.REQUEST_VENDOR_CLARIFICATION:
        if not facts.discrepancy:
            return "there is no discrepancy with an order or delivery to clarify"
    elif action == ActionType.HOLD_FOR_REVIEW:
        if facts.target is None:
            return "there is no document to review"
    elif action == ActionType.NO_ACTION and facts.target is not None:
        if facts.failing or facts.strong_duplicate or facts.comparison_issues:
            return "a failed rule, a duplicate or a difference cannot be left without action"
    return None


def baseline_action(facts: Situation, confidence: ConfidenceLevel) -> tuple[ActionType, str]:
    if facts.target is None:
        return ActionType.NO_ACTION, "No document needs an action."
    if facts.strong_duplicate:
        return ActionType.REJECT_DUPLICATE, "The document duplicates an earlier one."
    if facts.failing:
        names = "; ".join(r["rule_name"] for r in facts.failing[:3])
        return ActionType.HOLD_FOR_REVIEW, f"Failed rule(s) need a reviewer's decision: {names}."
    if facts.comparison_issues:
        return ActionType.HOLD_FOR_REVIEW, "The comparison found differences a reviewer must check."
    if facts.attention:
        return ActionType.HOLD_FOR_REVIEW, "Some checks could not confirm the values."
    if confidence == ConfidenceLevel.LOW:
        return ActionType.HOLD_FOR_REVIEW, "The evidence is too weak to decide automatically."
    if (
        guardrail(ActionType.APPROVE_FOR_PAYMENT, facts, confidence) is None
        and facts.target_type == "INVOICE"
    ):
        return (
            ActionType.APPROVE_FOR_PAYMENT,
            "Every applicable rule passed and the evidence is strong; payment needs approval.",
        )
    return ActionType.NO_ACTION, "Every applicable rule passed."


def recommend(
    state: InvestigationState,
    confidence: Confidence,
    *,
    proposed: ActionType | None,
    rationale: str | None,
    rationale_evidence: list[str],
) -> Recommendation:
    facts = situation(state)
    notes: list[str] = []
    action, reason = baseline_action(facts, confidence.level)
    source: str = "rules"
    if proposed is not None:
        blocked = guardrail(proposed, facts, confidence.level)
        if blocked is None:
            if proposed != action:
                notes.append(f"The rules alone would recommend {action.value}.")
            action, source = proposed, "model"
            reason = rationale or reason
        else:
            notes.append(f"The model proposed {proposed.value}, not allowed: {blocked}.")
    policy = ACTIONS[action]
    return Recommendation(
        action=action,
        target_document_id=facts.target if action != ActionType.NO_ACTION else None,
        rationale=reason,
        evidence=rationale_evidence if source == "model" else [],
        risk=policy.risk,
        requires_approval=policy.requires_approval,
        required_role=policy.required_role,
        source="model" if source == "model" else "rules",
        guardrail_notes=notes,
    )


def gate(recommendation: Recommendation, *, allow_safe_actions: bool) -> str:
    """approval_gate: 'execute' a low-risk action, 'propose' one needing approval, or 'none'."""
    if recommendation.requires_approval:
        return "propose"
    if recommendation.action == ActionType.HOLD_FOR_REVIEW and allow_safe_actions:
        return "execute"
    return "none"
