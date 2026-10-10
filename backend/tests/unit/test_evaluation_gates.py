"""Regression gates and the generated README table (Phase 10, ADR-069/070)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from docintel.cli import SUITES
from docintel.evaluation.gates import Gate, check_gate, check_report, failures, load_gates
from docintel.evaluation.headlines import HEADLINES
from docintel.evaluation.readme import BEGIN, END, ReadmeError, render_block, update_readme

ROOT = Path(__file__).resolve().parents[3]
GATES = ROOT / "evaluation" / "gates.toml"
REPORTS = ROOT / "evaluation" / "reports"


def gate(**values: Any) -> Gate:
    return Gate.model_validate({"suite": "agent", "metric": ["a", "b"], "why": "test", **values})


def test_a_gate_needs_a_bound_and_a_sane_range() -> None:
    with pytest.raises(ValidationError, match="needs min or max"):
        gate()
    with pytest.raises(ValidationError, match="min is above max"):
        gate(min=1, max=0)
    with pytest.raises(ValidationError):
        gate(max=0, modes=["nightly"])


@pytest.mark.parametrize(
    ("metrics", "passed", "problem"),
    [
        ({"a": {"b": 0}}, True, None),
        ({"a": {"b": 1}}, False, "above 0"),
        ({"a": {}}, False, "missing"),  # a renamed metric does not pass silently
        ({"a": {"b": None}}, False, "missing"),
        ({"a": {"b": "0"}}, False, "not a number"),
        ({"a": {"b": True}}, False, "not a number"),
    ],
)
def test_gate_checks(metrics: dict[str, Any], passed: bool, problem: str | None) -> None:
    result = check_gate(gate(max=0), metrics)
    assert result["passed"] is passed
    assert result["problem"] == problem


def test_gates_apply_by_suite_and_mode() -> None:
    gates = [gate(min=1.0, modes=["full"]), gate(max=0, metric=["c"]), gate(suite="ocr", max=0)]
    quick = check_report(gates, suite="agent", quick=True, metrics={"c": 0})
    assert quick is not None
    assert [c["metric"] for c in quick["checks"]] == [["c"]]  # the full-only gate is skipped
    full = check_report(gates, suite="agent", quick=False, metrics={"a": {"b": 0.5}, "c": 0})
    assert full is not None
    assert full["passed"] is False
    assert failures({"agent": full}) == ["agent: a / b = 0.5 (below 1) - test"]
    assert check_report(gates, suite="tables", quick=False, metrics={}) is None


def test_the_gate_file_is_valid_and_names_real_suites() -> None:
    gates = load_gates(GATES)
    assert {g.suite for g in gates} <= set(SUITES)
    assert len({(g.suite, g.metric) for g in gates}) == len(gates)  # no duplicate gate
    # Every suite has at least one gate, and the safety invariants run in quick mode (CI).
    assert {g.suite for g in gates} == set(SUITES)
    for suite in ("agent", "workflow", "retrieval", "system"):
        assert any(g.suite == suite and "quick" in g.modes and g.maximum == 0 for g in gates)


def _committed() -> dict[str, dict[str, Any]]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(REPORTS.glob("*.json"))
    }


def test_the_committed_reports_pass_their_gates() -> None:
    gates = load_gates(GATES)
    results = {
        suite: check_report(
            gates, suite=suite, quick=bool(data.get("quick")), metrics=data["metrics"]
        )
        for suite, data in _committed().items()
    }
    assert failures(results) == []


def test_the_readme_table_is_generated_from_the_committed_reports() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    block = render_block(_committed())
    assert update_readme(readme, block) == readme, "run `docintel evaluation readme`"
    assert {h.suite for h in HEADLINES} <= set(SUITES)


def _report(suite: str, **values: Any) -> dict[str, Any]:
    return {"suite": suite, "git_revision": "abc1234def56", "quick": False, "metrics": {}, **values}


def test_the_readme_refuses_quick_uncommitted_or_missing_reports() -> None:
    reports = {h.suite: _report(h.suite) for h in HEADLINES}
    reports["ocr"] = _report("ocr", quick=True)
    reports["agent"] = _report("agent", git_revision="abc1234def56+dirty")
    del reports["system"]
    with pytest.raises(ReadmeError) as error:
        render_block(reports)
    message = str(error.value)
    assert "ocr: a quick run" in message
    assert "agent: not from a clean checkout" in message
    assert "system: no report" in message


def test_a_missing_metric_fails_the_readme_instead_of_leaving_a_stale_number() -> None:
    reports = {h.suite: _report(h.suite) for h in HEADLINES}
    with pytest.raises(ReadmeError, match="cannot be rendered"):
        render_block(reports)


def test_the_block_replaces_only_the_text_between_the_markers() -> None:
    text = f"intro\n{BEGIN}\nold\n{END}\noutro\n"
    assert update_readme(text, f"{BEGIN}\nnew\n{END}") == f"intro\n{BEGIN}\nnew\n{END}\noutro\n"
    with pytest.raises(ReadmeError, match="exactly one pair"):
        update_readme("no markers", "x")
    with pytest.raises(ReadmeError, match="before the begin"):
        update_readme(f"{END}\n{BEGIN}", "x")
