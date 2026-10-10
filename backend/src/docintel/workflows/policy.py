"""What a workflow may do: the action allowlist with its risk table, who may approve, and how
an investigation's recommendation becomes the workflow's proposal (deterministic)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from docintel.db.models import ActionRisk, ProposerType, Role, WorkflowActionType, WorkflowType

A = WorkflowActionType


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    risk: ActionRisk
    requires_approval: bool
    required_role: Role | None
    outcome: str  # the workflow's outcome once the action is executed
    title: str


ACTION_POLICIES: dict[WorkflowActionType, ActionPolicy] = {
    A.APPROVE_FOR_PAYMENT: ActionPolicy(
        ActionRisk.HIGH, True, Role.MANAGER, "APPROVED_FOR_PAYMENT", "Approve for payment"
    ),
    A.REJECT_DUPLICATE: ActionPolicy(
        ActionRisk.HIGH, True, Role.MANAGER, "REJECTED_AS_DUPLICATE", "Reject as a duplicate"
    ),
    A.REQUEST_VENDOR_CLARIFICATION: ActionPolicy(
        ActionRisk.MEDIUM,
        True,
        Role.REVIEWER,
        "AWAITING_VENDOR_CLARIFICATION",
        "Ask the vendor to clarify",
    ),
    A.APPROVE_CONTRACT: ActionPolicy(
        ActionRisk.HIGH, True, Role.MANAGER, "CONTRACT_APPROVED", "Approve the contract"
    ),
    A.HOLD_FOR_REVIEW: ActionPolicy(
        ActionRisk.LOW, False, None, "SENT_TO_REVIEW", "Hold for a reviewer"
    ),
    A.REQUEST_LEGAL_REVIEW: ActionPolicy(
        ActionRisk.LOW, False, None, "SENT_TO_LEGAL_REVIEW", "Send to Legal review"
    ),
}

# Who may decide an action that needs a role (workflows:approve is needed as well).
APPROVER_ROLES: dict[Role, frozenset[Role]] = {
    Role.REVIEWER: frozenset({Role.REVIEWER, Role.MANAGER, Role.ADMIN}),
    Role.MANAGER: frozenset({Role.MANAGER, Role.ADMIN}),
}

NO_ACTION_OUTCOME = "NO_ACTION"
REJECTED_OUTCOME = "PROPOSAL_REJECTED"
FAILED_OUTCOME = "ACTION_FAILED"
MAX_ISSUES = 10
ISSUE_CHARS = 300


def role_may_approve(role: Role, required: Role | None) -> bool:
    if required is None:
        return True
    return role in APPROVER_ROLES.get(required, frozenset({required, Role.ADMIN}))


@dataclass(slots=True)
class Proposal:
    action_type: WorkflowActionType
    rationale: str
    proposer: ProposerType
    confidence_level: str | None
    confidence_score: float | None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def policy(self) -> ActionPolicy:
        return ACTION_POLICIES[self.action_type]


def _subject(result: dict[str, Any], document_id: str) -> dict[str, Any]:
    document: dict[str, Any] = next(
        (item for item in result.get("documents", []) if item.get("document_id") == document_id),
        {},
    )
    keys = ("filename", "document_type", "document_number", "vendor_name", "document_date")
    subject = {key: document.get(key) for key in keys}
    subject["total"] = document.get("total")
    subject["currency"] = document.get("currency")
    return subject


def _issues(result: dict[str, Any]) -> list[str]:
    """The rule findings the decision rests on (deterministic statements, bounded)."""
    return [
        finding["statement"][:ISSUE_CHARS]
        for finding in result.get("findings", [])
        if finding.get("category") == "RULE_RESULT"
        and not finding["statement"].startswith("All ")  # "All N applicable rules passed"
    ][:MAX_ISSUES]


def _join(issues: list[str], limit: int = 3) -> str:
    shown = "; ".join(issue.rstrip(".") for issue in issues[:limit])
    more = f" (and {len(issues) - limit} more)" if len(issues) > limit else ""
    return f"{shown}{more}." if shown else ""


def decide(
    workflow_type: WorkflowType,
    result: dict[str, Any],
    *,
    document_id: str,
    version_changes: dict[str, Any] | None = None,
    open_review: str | None = None,
) -> Proposal | None:
    """The workflow's proposal from a finished investigation of its document (None: nothing to
    do). The investigation's recommendation already passed the agent's guardrails.
    `open_review`: the type of the document's open review task, if any - an approval is not
    proposed while a person still has to review the document."""
    recommendation = result["recommendation"]
    confidence = result.get("confidence") or {}
    level = confidence.get("level")
    proposer = ProposerType.AGENT if recommendation.get("source") == "model" else ProposerType.RULES
    issues = _issues(result)
    payload: dict[str, Any] = {
        "document": _subject(result, document_id),
        "summary": str(result.get("summary") or "")[:500],
        "issues": issues,
    }
    if version_changes:
        payload["version_changes"] = version_changes

    def proposal(action: WorkflowActionType, rationale: str, by: ProposerType) -> Proposal:
        return Proposal(
            action_type=action,
            rationale=rationale[:2000],
            proposer=by,
            confidence_level=level,
            confidence_score=confidence.get("score"),
            payload=payload,
        )

    action = recommendation["action"]
    rationale = str(recommendation.get("rationale") or "")
    approval = action == "APPROVE_FOR_PAYMENT" or (
        workflow_type == WorkflowType.CONTRACT_REVIEW and action == "NO_ACTION" and level == "HIGH"
    )
    if approval and open_review:
        return proposal(
            A.HOLD_FOR_REVIEW,
            f"The checks passed, but the document has an open review task ({open_review}): "
            "approval is proposed once a reviewer has resolved it.",
            ProposerType.RULES,
        )
    if workflow_type == WorkflowType.INVOICE_PROCESSING:
        if action == "NO_ACTION":
            # A payment workflow always ends in a decision: weak evidence goes to a person.
            return proposal(
                A.HOLD_FOR_REVIEW,
                f"The checks did not stop the invoice, but confidence is {level or 'unknown'}: "
                "payment is only proposed with HIGH confidence.",
                ProposerType.RULES,
            )
        return proposal(WorkflowActionType(action), rationale, proposer)

    # Contract review.
    if action == "REJECT_DUPLICATE":
        return proposal(A.REJECT_DUPLICATE, rationale, proposer)
    if action == "NO_ACTION" and level == "HIGH":
        changed = ""
        if version_changes and version_changes.get("changed"):
            changed = (
                f" Version {version_changes['to_version']} changed "
                f"{version_changes['changed']} clause(s) since version "
                f"{version_changes['from_version']}: review them before approving."
            )
        return proposal(
            A.APPROVE_CONTRACT,
            "Every contract rule passed (required clauses, termination notice, governing "
            f"law, expiry) and the evidence is strong.{changed}",
            ProposerType.RULES,
        )
    if action in ("HOLD_FOR_REVIEW", "REQUEST_VENDOR_CLARIFICATION") and issues:
        return proposal(
            A.REQUEST_LEGAL_REVIEW,
            f"The contract deviates from the contract guidelines: {_join(issues)} "
            "Deviations need Legal's approval.",
            ProposerType.RULES,
        )
    return proposal(
        A.HOLD_FOR_REVIEW,
        rationale or f"The evidence is too weak to decide automatically (confidence {level}).",
        proposer,
    )
