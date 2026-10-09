"""Rule evaluation: definitions, context, outcomes and the evaluator registry."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from docintel.core.logging import get_logger
from docintel.db.models import DocumentType, RuleOutcome, RuleSeverity
from docintel.matching.compare import ComparisonOutcome
from docintel.matching.duplicates import DuplicateMatch
from docintel.matching.facts import DocumentFacts

logger = get_logger(__name__)


# Shared with the database (business_rules.severity, rule_results.outcome).
Severity = RuleSeverity
Outcome = RuleOutcome

SEVERITY_RANK = {Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3, Severity.CRITICAL: 4}


NEEDS_REVIEW = frozenset({Outcome.FAIL, Outcome.WARN, Outcome.ERROR})


class RuleParams(BaseModel):
    """Base for evaluator parameters: unknown keys are rejected, values are validated."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class RuleDefinition:
    """A configured rule (a `business_rules` row, or a default from code)."""

    code: str
    rule_type: str
    name: str
    description: str
    applies_to: frozenset[DocumentType]
    severity: Severity
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    version: int = 1
    rule_id: str | None = None


@dataclass(slots=True)
class RuleContext:
    document: DocumentFacts
    reference_date: date
    comparison: ComparisonOutcome | None = None
    duplicates: list[DuplicateMatch] = field(default_factory=list)
    # The referenced purchase order exists in scope (None: no reference to look up).
    order_on_file: bool | None = None


@dataclass(slots=True)
class Finding:
    outcome: Outcome
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    # Comparison items behind the finding (keys), so the UI can point at them.
    items: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RuleResult:
    rule: RuleDefinition
    outcome: Outcome
    message: str
    evidence: dict[str, Any]
    items: list[str]

    @property
    def severity(self) -> Severity:
        return self.rule.severity

    @property
    def needs_review(self) -> bool:
        return self.outcome in NEEDS_REVIEW

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.rule.code,
            "rule_type": self.rule.rule_type,
            "rule_version": self.rule.version,
            "outcome": self.outcome.value,
            "severity": self.severity.value,
            "message": self.message,
            "evidence": self.evidence,
            "items": self.items,
        }


class Evaluator(Protocol):
    rule_type: ClassVar[str]
    params_model: ClassVar[type[RuleParams]]

    def evaluate(self, context: RuleContext, params: Any) -> Finding: ...


_REGISTRY: dict[str, Evaluator] = {}


def register(evaluator: Evaluator) -> Evaluator:
    if evaluator.rule_type in _REGISTRY:  # pragma: no cover - programming error
        msg = f"duplicate rule type {evaluator.rule_type}"
        raise ValueError(msg)
    _REGISTRY[evaluator.rule_type] = evaluator
    return evaluator


def evaluator_for(rule_type: str) -> Evaluator | None:
    from docintel.rules import evaluators  # noqa: F401 - registers the evaluators

    return _REGISTRY.get(rule_type)


def rule_types() -> dict[str, Evaluator]:
    from docintel.rules import evaluators  # noqa: F401

    return dict(_REGISTRY)


class InvalidRuleParamsError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_params(rule_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validated, normalized parameters (JSON-compatible) or InvalidRuleParamsError."""
    evaluator = evaluator_for(rule_type)
    if evaluator is None:
        raise InvalidRuleParamsError([f"unknown rule type '{rule_type}'"])
    try:
        model = evaluator.params_model.model_validate(params)
    except ValidationError as exc:
        raise InvalidRuleParamsError(
            [f"{'.'.join(str(p) for p in e['loc']) or 'params'}: {e['msg']}" for e in exc.errors()]
        ) from exc
    return model.model_dump(mode="json")


def parsed_params(rule: RuleDefinition) -> RuleParams:
    evaluator = evaluator_for(rule.rule_type)
    if evaluator is None:
        msg = f"unknown rule type '{rule.rule_type}'"
        raise InvalidRuleParamsError([msg])
    return evaluator.params_model.model_validate(rule.params)


def applicable(
    rules: Iterable[RuleDefinition], document_type: DocumentType
) -> list[RuleDefinition]:
    return [rule for rule in rules if rule.enabled and document_type in rule.applies_to]


def evaluate(rules: Sequence[RuleDefinition], context: RuleContext) -> list[RuleResult]:
    """Run every enabled rule that applies to the document; a failing rule yields ERROR."""
    results: list[RuleResult] = []
    for rule in applicable(rules, context.document.document_type):
        evaluator = evaluator_for(rule.rule_type)
        try:
            if evaluator is None:
                msg = f"unknown rule type '{rule.rule_type}'"
                raise InvalidRuleParamsError([msg])
            finding = evaluator.evaluate(
                context, evaluator.params_model.model_validate(rule.params)
            )
        except Exception as exc:
            logger.warning("rule_failed", code=rule.code, error=type(exc).__name__)
            finding = Finding(
                Outcome.ERROR,
                f"The rule could not be evaluated ({type(exc).__name__}).",
            )
        results.append(
            RuleResult(rule, finding.outcome, finding.message, finding.evidence, finding.items)
        )
    return results
