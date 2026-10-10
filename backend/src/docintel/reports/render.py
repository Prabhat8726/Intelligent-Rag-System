"""Snapshot -> Markdown, deterministically.

The same snapshot always renders to the same bytes: only lists (which keep their order) and
explicitly sorted keys are iterated - never a mapping's own order, which JSONB does not keep.
Text from documents is escaped so it cannot add links, HTML or table cells.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from typing import Any

# Only what could add a link, an image, HTML, code or a table cell (emphasis is harmless).
_ESCAPE = re.compile(r"([\\`\[\]<>|])")
OUTCOME_ORDER = {"FAIL": 0, "ERROR": 1, "WARN": 2, "PASS": 3, "NOT_APPLICABLE": 4}
STEP_MARK = {
    "COMPLETED": "done",
    "SKIPPED": "skipped",
    "FAILED": "failed",
    "RUNNING": "in progress",
}
TYPE_TITLES = {
    "INVOICE_VERIFICATION": "Invoice verification",
    "CONTRACT_REVIEW": "Contract review",
    "DOCUMENT_COMPARISON": "Document comparison",
    "COMPLIANCE_REVIEW": "Compliance review",
    "AI_ANALYSIS": "AI analysis",
}


WORKFLOW_TITLES = {"INVOICE_PROCESSING": "Invoice processing", "CONTRACT_REVIEW": "Contract review"}


def normalized(data: dict[str, Any]) -> dict[str, Any]:
    """The snapshot as it will come back from the database (JSON round trip)."""
    loaded: dict[str, Any] = json.loads(json.dumps(data, sort_keys=True))
    return loaded


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def text(value: Any) -> str:
    """Escaped single-line text (also safe inside a table cell)."""
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}"
    return _ESCAPE.sub(r"\\\1", " ".join(str(value).split()))


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(text(cell) for cell in row) + " |" for row in rows)
    return lines


def _documents(snapshot: dict[str, Any]) -> list[str]:
    documents = snapshot.get("documents") or []
    if not documents:
        return []
    return [
        "## Source documents",
        "",
        *table(
            ["Role", "File", "Type", "Version", "Status", "Sensitivity", "Uploaded by", "Uploaded"],
            (
                [
                    d["role"],
                    d["filename"],
                    d["document_type"],
                    d["version_number"],
                    d["status"],
                    d["sensitivity"],
                    d["uploaded_by"],
                    d["uploaded_at"],
                ]
                for d in documents
            ),
        ),
        "",
        "SHA-256 of each file: "
        + "; ".join(f"{text(d['filename'])} {text(d['sha256'])}" for d in documents)
        + ".",
        "",
    ]


def _fields(snapshot: dict[str, Any]) -> list[str]:
    fields = snapshot.get("fields")
    if fields is None:
        return []
    if not fields:
        return ["## Extracted values", "", "No extraction.", ""]
    return [
        "## Extracted values",
        "",
        *table(
            ["Field", "Value", "Corrected", "Confidence", "Evidence", "Page", "Quote"],
            (
                [
                    f["field"],
                    f["value"],
                    f["corrected"],
                    f["confidence"],
                    f["evidence"],
                    f["page"],
                    f["quote"],
                ]
                for f in fields
            ),
        ),
        "",
    ]


def _rules(snapshot: dict[str, Any]) -> list[str]:
    rules = snapshot.get("rules")
    if rules is None:
        return []
    if not rules:
        return ["## Rule results", "", "No rule applies to this document.", ""]
    ordered = sorted(rules, key=lambda r: (OUTCOME_ORDER.get(r["outcome"], 9), r["code"]))
    counts: dict[str, int] = {}
    for rule in rules:
        counts[rule["outcome"]] = counts.get(rule["outcome"], 0) + 1
    summary = ", ".join(
        f"{counts[key]} {key}" for key in sorted(counts, key=lambda key: OUTCOME_ORDER.get(key, 9))
    )
    return [
        "## Rule results",
        "",
        f"{summary}.",
        "",
        *table(
            ["Outcome", "Rule", "Code", "Severity", "Message"],
            ([r["outcome"], r["name"], r["code"], r["severity"], r["message"]] for r in ordered),
        ),
        "",
    ]


def _comparison(snapshot: dict[str, Any]) -> list[str]:
    comparison = snapshot.get("comparison")
    if "comparison" not in snapshot:
        return []
    if not comparison:
        return ["## Comparison", "", "No purchase order or delivery note was compared.", ""]
    summary = ", ".join(
        f"{comparison['summary'][key]} {key}" for key in sorted(comparison["summary"])
    )
    members = ", ".join(f"{text(m['filename'])} ({m['role']})" for m in comparison["members"])
    lines = [
        "## Comparison",
        "",
        f"{text(comparison['type'])} comparison ({comparison['origin'].lower()}, "
        f"{text(comparison['created_at'])}) of {members}: {summary or 'no items'}.",
        "",
    ]
    if comparison["items"]:
        lines += table(
            ["Check", "Line", "Status", "Value", "Compared with", "Explanation"],
            (
                [i["check"], i["line"], i["status"], i["left"], i["right"], i["explanation"]]
                for i in comparison["items"]
            ),
        )
        lines.append("")
    return lines


def _contract(snapshot: dict[str, Any]) -> list[str]:
    contract = snapshot.get("contract")
    if not contract:
        return []
    lines = ["## Contract clauses", ""]
    clauses = contract.get("clauses") or []
    lines.append(
        "; ".join(f"{text(c['number'])}. {text(c['title'])}" for c in clauses) + "."
        if clauses
        else "No numbered clauses were found."
    )
    lines.append("")
    changes = contract.get("changes")
    if changes:
        summary = ", ".join(
            f"{changes['summary'][key]} {key.lower()}" for key in sorted(changes["summary"])
        )
        lines += [
            f"### Changes since version {changes['from_version']}",
            "",
            f"{summary}.",
            "",
        ]
        if changes["changed"]:
            lines += table(
                ["Clause", "Change"], ([c["title"], c["change"]] for c in changes["changed"])
            )
            lines.append("")
    return lines


def _analysis(snapshot: dict[str, Any]) -> list[str]:
    if "analysis" not in snapshot:
        return []
    analysis = snapshot.get("analysis")
    if not analysis:
        return ["## AI analysis", "", "No investigation was run.", ""]
    recommendation = analysis["recommendation"]
    confidence = analysis["confidence"]
    lines = [
        "## AI analysis",
        "",
        f"Request: {text(analysis['query'])} (investigation {analysis['run_id'][:8]}, "
        f"finished {text(analysis['finished_at'])}).",
        "",
        f"Summary ({text(analysis['summary_source'])}): {text(analysis['summary'])}",
        "",
        "Findings:",
        "",
    ]
    lines += [
        f"- [{text(f['category'])}] {text(f['statement'])}"
        + (f" ({', '.join(text(label) for label in f['evidence'])})" if f["evidence"] else "")
        for f in analysis["findings"]
    ] or ["- none"]
    lines += ["", "Policy sources:", ""]
    lines += [
        f"- {text(s['label'])}: {text(s['title'])} {text(s['version'])}, {text(s['section'])} "
        f"(in force from {text(s['effective_from'])})"
        for s in analysis["sources"]
    ] or ["- none"]
    factors = "; ".join(text(f) for f in confidence["factors"]) or "no weakness found"
    lines += [
        "",
        f"Confidence: {text(confidence['level'])} ({text(confidence['score'])}) - {factors}.",
        "",
        f"Recommendation: {text(recommendation['action'])} ({text(recommendation['source'])}) - "
        f"{text(recommendation['rationale'])}",
        "",
    ]
    model = analysis.get("model")
    lines += [
        "Model: "
        + (
            f"{text(model.get('provider'))} {text(model.get('model'))}"
            if model
            else "none (deterministic rules and retrieval only)"
        )
        + ".",
        "",
    ]
    return lines


def _action(action: dict[str, Any]) -> list[str]:
    lines = [
        f"#### {text(action['type'])} - {text(action['status'])}",
        "",
        f"- Risk {text(action['risk'])}"
        + (
            f"; needs the approval of a {text(action['required_role'])}"
            if action["required_role"]
            else "; no approval needed"
        ),
        f"- Proposed by {text(action['proposed_by'])} (confidence {text(action['confidence'])}): "
        f"{text(action['rationale'])}",
    ]
    if action["decided_by"]:
        lines.append(
            f"- Decided by {text(action['decided_by'])} at {text(action['decided_at'])}: "
            f"{text(action['decision_reason'])}"
        )
    if action["executed_at"]:
        lines.append(f"- Executed at {text(action['executed_at'])}")
    result = action.get("result") or {}
    for key in sorted(result):
        lines.append(f"- {key.replace('_', ' ').capitalize()}: {text(result[key])}")
    if action["error"]:
        lines.append(f"- Error: {text(action['error'])}")
    lines += [
        "",
        *table(
            ["When", "From", "To", "By", "Reason"],
            (
                [t["at"], t["from"] or "(new)", t["to"], _by(t), t["reason"]]
                for t in action["transitions"]
            ),
        ),
        "",
    ]
    return lines


def _step_state(step: dict[str, Any]) -> str:
    if step["step"] == "approval" and step["status"] == "RUNNING":
        return "waiting for approval"
    status = str(step["status"])
    return STEP_MARK.get(status, status.lower())


def _by(transition: dict[str, Any]) -> str:
    """'manager@...' for a person; 'SYSTEM for analyst@...' when acting for someone."""
    by, kind = transition["by"], transition["actor_type"]
    return f"{kind} for {by}" if kind != "USER" and by != kind else by


def _workflow(workflow: dict[str, Any]) -> list[str]:
    title = WORKFLOW_TITLES.get(workflow["type"], text(workflow["type"]))
    steps = ", ".join(f"{text(step['step'])} ({_step_state(step)})" for step in workflow["steps"])
    lines = [
        f"### {title} workflow {workflow['id'][:8]} - {text(workflow['status'])}"
        + (f" ({text(workflow['outcome'])})" if workflow["outcome"] else ""),
        "",
        f"Started by {text(workflow['initiated_by'])} ({workflow['trigger'].lower()}) at "
        f"{text(workflow['created_at'])}; finished {text(workflow['finished_at'])}. "
        f"Definition version {workflow['definition_version']}.",
        "",
        f"Steps: {steps}.",
        "",
    ]
    for action in workflow["actions"]:
        lines += _action(action)
    return lines


def _workflows(snapshot: dict[str, Any]) -> list[str]:
    if "workflows" in snapshot:
        workflows = snapshot.get("workflows") or []
    elif snapshot.get("workflow"):
        workflows = [snapshot["workflow"]]
    else:
        return []
    lines = ["## Workflows and decisions", ""]
    if not workflows:
        return [*lines, "No workflow has run for this document.", ""]
    for workflow in workflows:
        lines += _workflow(workflow)
    return lines


def _reviews(snapshot: dict[str, Any]) -> list[str]:
    reviews = snapshot.get("reviews")
    if reviews is None:
        return []
    lines = ["## Human reviews", ""]
    if not reviews["tasks"] and not reviews["requests"]:
        return [*lines, "No review task or request.", ""]
    if reviews["tasks"]:
        lines += table(
            ["Opened", "Type", "Priority", "Status", "Resolution", "By", "Resolved", "Note"],
            (
                [
                    t["created_at"],
                    t["type"],
                    t["priority"],
                    t["status"],
                    t["resolution"],
                    t["resolved_by"],
                    t["resolved_at"],
                    t["note"],
                ]
                for t in reviews["tasks"]
            ),
        )
        lines.append("")
    if reviews["requests"]:
        lines += ["Review requests:", ""]
        lines += [
            f"- {text(r['created_at'])} {text(r['requested_by'])} ({text(r['priority'])}): "
            f"{text(r['reason'])}"
            for r in reviews["requests"]
        ]
        lines.append("")
    return lines


def render(snapshot: dict[str, Any]) -> str:
    """Markdown for a (normalized) snapshot."""
    kind = TYPE_TITLES.get(snapshot["report_type"], snapshot["report_type"])
    lines = [
        f"# {text(snapshot['title'])}",
        "",
        f"{kind} report - data as of {text(snapshot['as_of'])} - template version "
        f"{snapshot['template_version']}.",
        "",
    ]
    if snapshot.get("requested_by"):
        lines += [f"Investigation requested by {text(snapshot['requested_by'])}.", ""]
    for section in (
        _documents,
        _fields,
        _contract,
        _comparison,
        _rules,
        _analysis,
        _workflows,
        _reviews,
    ):
        lines += section(snapshot)
    lines += [
        "---",
        "",
        "Rendered from the stored snapshot of this report; rendering the snapshot again gives "
        "the same text (its SHA-256 is recorded with the report). Values come from the "
        "platform's extraction, rules and decisions; no external system was consulted.",
        "",
    ]
    return "\n".join(lines)
