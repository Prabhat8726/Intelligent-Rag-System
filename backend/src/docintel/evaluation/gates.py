"""Regression gates over evaluation reports (Phase 10, ADR-069).

`evaluation/gates.toml` lists bounds on report metrics; a run (`docintel evaluate --gates`), a
directory of reports (`docintel evaluation gates`) or CI fails when one is broken. A gate
applies to full runs, quick runs or both, since quick runs leave some datasets out. A metric
that is missing or not a number fails its gate, so renaming a metric cannot pass silently.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Mode = Literal["full", "quick"]


class Gate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    suite: str = Field(pattern=r"^[a-z][a-z_]{0,39}$")
    metric: tuple[str, ...] = Field(min_length=1)
    minimum: float | None = Field(default=None, alias="min")
    maximum: float | None = Field(default=None, alias="max")
    modes: frozenset[Mode] = frozenset({"full", "quick"})
    why: str = Field(min_length=1)

    @model_validator(mode="after")
    def _bounded(self) -> Gate:
        if self.minimum is None and self.maximum is None:
            msg = f"gate {self.label}: needs min or max"
            raise ValueError(msg)
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            msg = f"gate {self.label}: min is above max"
            raise ValueError(msg)
        return self

    @property
    def label(self) -> str:
        return f"{self.suite}: {' / '.join(self.metric)}"


class GateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: list[Gate]


def load_gates(path: Path) -> list[Gate]:
    return parse_gates(path.read_bytes())


def parse_gates(data: bytes) -> list[Gate]:
    """Gates from the TOML text of a gates file (raises ValueError when invalid)."""
    return GateFile.model_validate(tomllib.loads(data.decode("utf-8"))).gate


def _resolve(metrics: Mapping[str, Any], keys: Iterable[str]) -> Any:
    value: Any = metrics
    for key in keys:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def check_gate(gate: Gate, metrics: Mapping[str, Any]) -> dict[str, Any]:
    value = _resolve(metrics, gate.metric)
    number = (
        float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
    )
    problem = None
    if number is None or math.isnan(number):
        problem = "missing" if value is None else "not a number"
    elif gate.minimum is not None and number < gate.minimum:
        problem = f"below {gate.minimum:g}"
    elif gate.maximum is not None and number > gate.maximum:
        problem = f"above {gate.maximum:g}"
    return {
        "metric": list(gate.metric),
        "value": value if number is not None else None,
        "min": gate.minimum,
        "max": gate.maximum,
        "passed": problem is None,
        "problem": problem,
        "why": gate.why,
    }


def check_report(
    gates: Iterable[Gate], *, suite: str, quick: bool, metrics: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The gates of one report: {"passed", "mode", "checks"}; None when no gate applies."""
    mode: Mode = "quick" if quick else "full"
    applicable = [gate for gate in gates if gate.suite == suite and mode in gate.modes]
    if not applicable:
        return None
    checks = [check_gate(gate, metrics) for gate in applicable]
    return {"passed": all(check["passed"] for check in checks), "mode": mode, "checks": checks}


def failures(results: Mapping[str, dict[str, Any] | None]) -> list[str]:
    """One line per broken gate, for the console and CI logs."""
    lines = []
    for suite, result in sorted(results.items()):
        for check in (result or {}).get("checks", []):
            if not check["passed"]:
                lines.append(
                    f"{suite}: {' / '.join(check['metric'])} = {check['value']} "
                    f"({check['problem']}) - {check['why']}"
                )
    return lines
