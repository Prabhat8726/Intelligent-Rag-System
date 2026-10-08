"""Sensitivity gate for external AI calls (C1, ADR-007).

Every call that would send document content to an external provider asks the gate first. The
effective sensitivity is the higher of the label set by the uploader and what the content or
document type implies; it is never lowered. Above AI_EXTERNAL_MAX_SENSITIVITY nothing leaves the
platform: callers fall back to local processing or human review.
"""

from __future__ import annotations

from dataclasses import dataclass

from docintel.db.models import Sensitivity

SENSITIVITY_ORDER: tuple[Sensitivity, ...] = (
    Sensitivity.PUBLIC,
    Sensitivity.INTERNAL,
    Sensitivity.CONFIDENTIAL,
    Sensitivity.RESTRICTED,
)


def sensitivity_rank(value: Sensitivity) -> int:
    return SENSITIVITY_ORDER.index(value)


def max_sensitivity(*values: Sensitivity | None) -> Sensitivity:
    present = [value for value in values if value is not None]
    if not present:
        return Sensitivity.PUBLIC
    return max(present, key=sensitivity_rank)


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    effective_sensitivity: Sensitivity
    reason: str

    def to_json(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "effective_sensitivity": self.effective_sensitivity.value,
            "reason": self.reason,
        }


class ExternalAIGate:
    def __init__(
        self,
        *,
        max_sensitivity: Sensitivity,
        provider_configured: bool,
        provider_local: bool = False,
    ) -> None:
        self._max = max_sensitivity
        self._configured = provider_configured
        # A self-hosted model (Ollama) keeps content inside the deployment (ADR-029).
        self._local = provider_local

    @property
    def max_sensitivity(self) -> Sensitivity:
        return self._max

    def decide(self, *levels: Sensitivity | None) -> GateDecision:
        effective = max_sensitivity(*levels)
        if not self._configured:
            return GateDecision(False, effective, "no external AI provider configured")
        if self._local:
            return GateDecision(True, effective, "local model: content stays in the deployment")
        if sensitivity_rank(effective) > sensitivity_rank(self._max):
            return GateDecision(
                False,
                effective,
                f"{effective.value} content may not be sent to external AI "
                f"(AI_EXTERNAL_MAX_SENSITIVITY={self._max.value})",
            )
        return GateDecision(True, effective, "allowed")
