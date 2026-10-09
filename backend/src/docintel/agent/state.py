"""Explicit investigation state (Module 14) and the structured result it produces.

The graph state is a TypedDict of JSON-compatible values: every node reads what earlier nodes
recorded and returns only the keys it changes, so a run can be inspected step by step and its
result stored as is. Model reasoning is never part of it.
"""

from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

GRAPH_VERSION = "investigation-v1"


class Intent(StrEnum):
    VERIFY_DOCUMENT = "VERIFY_DOCUMENT"  # can this document be processed / paid?
    INVESTIGATE_DISCREPANCY = "INVESTIGATE_DISCREPANCY"  # why does something not match?
    CHECK_DUPLICATE = "CHECK_DUPLICATE"
    COMPARE_DOCUMENTS = "COMPARE_DOCUMENTS"
    POLICY_QUESTION = "POLICY_QUESTION"  # what do policies say (no document needed)
    FIND_DOCUMENTS = "FIND_DOCUMENTS"  # which documents match criteria


class FindingCategory(StrEnum):
    OBSERVED_FACT = "OBSERVED_FACT"  # read from a document (extraction, evidence)
    RULE_RESULT = "RULE_RESULT"  # a deterministic rule or comparison outcome
    RETRIEVED_KNOWLEDGE = "RETRIEVED_KNOWLEDGE"  # what a cited policy passage says
    AI_INFERENCE = "AI_INFERENCE"  # the model's interpretation, citing the facts it uses
    UNCERTAINTY = "UNCERTAINTY"  # what could not be established


class ActionType(StrEnum):
    """The only actions an investigation may recommend (allowlist)."""

    APPROVE_FOR_PAYMENT = "APPROVE_FOR_PAYMENT"
    HOLD_FOR_REVIEW = "HOLD_FOR_REVIEW"
    REQUEST_VENDOR_CLARIFICATION = "REQUEST_VENDOR_CLARIFICATION"
    REJECT_DUPLICATE = "REJECT_DUPLICATE"
    NO_ACTION = "NO_ACTION"


class RiskLevel(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ConfidenceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Plan(Strict):
    intent: Intent
    document_query: str | None = Field(default=None, max_length=300)
    # Document numbers named in the request: identification keeps only exact matches.
    identifiers: list[str] = Field(default_factory=list, max_length=5)
    knowledge_questions: list[str] = Field(default_factory=list, max_length=3)
    focus_fields: list[str] = Field(default_factory=list, max_length=10)
    source: Literal["model", "rules"] = "rules"


class EvidenceItem(Strict):
    """Something a finding can cite: a document, field, rule outcome, comparison item or
    knowledge passage. `text` is what was shown to the model (bounded)."""

    label: str
    kind: Literal["DOCUMENT", "FIELD", "RULE", "COMPARISON", "KNOWLEDGE"]
    document_id: str | None = None
    ref: str
    text: str


class Finding(Strict):
    category: FindingCategory
    statement: str
    evidence: list[str] = Field(default_factory=list)
    source: Literal["rules", "model"] = "rules"
    grounded: bool = True


class ConfidenceFactor(Strict):
    factor: str
    effect: float
    detail: str


class Confidence(Strict):
    level: ConfidenceLevel
    score: float
    factors: list[ConfidenceFactor]


class Recommendation(Strict):
    action: ActionType
    target_document_id: str | None
    rationale: str
    evidence: list[str] = Field(default_factory=list)
    risk: RiskLevel
    requires_approval: bool
    required_role: str | None
    source: Literal["rules", "model"]
    guardrail_notes: list[str] = Field(default_factory=list)


class ActionRecord(Strict):
    action: ActionType
    status: Literal["EXECUTED", "PROPOSED", "SKIPPED", "FAILED"]
    detail: str
    tool_call_id: str | None = None
    review_task_id: str | None = None
    required_role: str | None = None


class InvestigationState(TypedDict, total=False):
    # request
    run_id: str
    user_id: str
    query: str
    requested_documents: list[str]
    allow_safe_actions: bool
    # plan
    plan: dict[str, Any]
    # gathered facts (tool outputs, keyed by document id)
    documents: dict[str, dict[str, Any]]
    document_roles: dict[str, str]  # "subject" (requested or found) or "related" (counterpart)
    identified_by: str  # "request", "search" or "none"
    extractions: dict[str, dict[str, Any]]
    evidence: dict[str, list[dict[str, Any]]]
    rules: dict[str, dict[str, Any]]
    comparisons: list[dict[str, Any]]
    knowledge: list[dict[str, Any]]
    knowledge_queries: list[str]
    pending_questions: list[str]
    # conclusions
    analysis: dict[str, Any]
    confidence: dict[str, Any]
    recommendation: dict[str, Any]
    action_route: str  # approval_gate's decision: "execute", "propose" or "none"
    action: dict[str, Any] | None
    rounds: int
    # accumulated across nodes
    notices: Annotated[list[str], operator.add]
    tool_calls: Annotated[list[dict[str, Any]], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]
