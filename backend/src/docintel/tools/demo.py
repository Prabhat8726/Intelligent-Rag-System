"""The final demonstration (master prompt §50), reproducible from the command line: `make demo`.

Seventeen steps through the public REST API of a running stack, as three people would take
them: an analyst uploads a purchase order, a delivery note and an invoice with a planted price
difference; the platform classifies, reads, normalizes and compares them and finds the
difference; invoice processing retrieves the procurement policy, the agent analyses and
recommends; the analyst may not decide their own case (maker-checker), a reviewer approves;
the workflow result, its report, the audit trail and the dashboard show what happened.

Every step checks what it shows: a deviation (the mismatch not found, another proposal, a
maker allowed to decide, ...) stops the demo with a non-zero exit. Nothing is simulated; the
same flow runs in a browser in `make e2e`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.scenarios import Scenario

FINISHED_DOCUMENT = frozenset({"COMPLETED", "REVIEW_REQUIRED", "FAILED"})
SETTLED_WORKFLOW = frozenset({"AWAITING_APPROVAL", "COMPLETED", "REJECTED", "FAILED", "CANCELLED"})
EXPECTED_RULE = "INV_PO_UNIT_PRICE"
EXPECTED_ACTION = "REQUEST_VENDOR_CLARIFICATION"
TYPES = {"PURCHASE_ORDER": "PO", "DELIVERY_NOTE": "DN", "INVOICE": "INV"}


class DemoError(RuntimeError):
    """A step did not show what the demonstration expects."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"step {step}: {message}")


@dataclass(frozen=True, slots=True)
class DemoUsers:
    """Bearer tokens of the three people in the demonstration."""

    analyst: str
    reviewer: str
    admin: str


@dataclass(slots=True)
class DemoResult:
    documents: dict[str, str] = field(default_factory=dict)  # PO / DN / INV -> document id
    workflow_id: str | None = None
    report_id: str | None = None
    links: list[str] = field(default_factory=list)


async def login(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if response.status_code != httpx.codes.OK:
        msg = f"login as {email} failed (HTTP {response.status_code}); run `make seed-docker`"
        raise DemoError("0", msg)
    token: str = response.json()["access_token"]
    return token


async def _sleep() -> None:
    await asyncio.sleep(1.0)


class Demo:
    def __init__(
        self,
        client: httpx.AsyncClient,
        users: DemoUsers,
        *,
        web_url: str,
        wait: Callable[[], Awaitable[None]] = _sleep,
        out: Callable[[str], None] = print,
        timeout: float = 300.0,
    ) -> None:
        self._client = client
        self._users = users
        self._web = web_url.rstrip("/")
        self._wait = wait
        self._out = out
        self._timeout = timeout

    # ------------------------------------------------------------------------ helpers
    async def _call(
        self, method: str, path: str, token: str, *, step: str, expect: int = 200, **kwargs: Any
    ) -> Any:
        response = await self._client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if response.status_code != expect:
            detail = response.text[:300]
            msg = f"{method} {path}: HTTP {response.status_code} (expected {expect}): {detail}"
            raise DemoError(step, msg)
        return response.json() if response.content else None

    async def _until(self, step: str, what: str, check: Callable[[], Awaitable[Any | None]]) -> Any:
        deadline = time.monotonic() + self._timeout
        while True:
            value = await check()
            if value is not None:
                return value
            if time.monotonic() > deadline:
                raise DemoError(step, f"{what} did not happen within {self._timeout:.0f} s")
            await self._wait()

    def _say(self, step: str, title: str, *lines: str) -> None:
        self._out(f"\n[{step}] {title}")
        for line in lines:
            self._out(f"      {line}")

    # ------------------------------------------------------------------------ the demonstration
    async def run(self, folder: Path, *, seed: int) -> DemoResult:
        result = DemoResult()
        users = self._users

        # 1-2. A purchase order and an invoice with a controlled price difference.
        manifest = generate_dataset(
            folder, seed=seed, bundles_per_scenario=1, scenarios=[Scenario.UNIT_PRICE_MISMATCH]
        )
        entries = {TYPES[entry["document_type"]]: entry for entry in manifest["documents"]}
        truth = _truth(folder, entries["INV"])
        (defect,) = [d for d in truth["defects"] if d["code"] == "UNIT_PRICE_MISMATCH"]
        self._say(
            "1-2",
            "Generate a purchase order, its delivery note and an invoice with a price difference",
            f"seed {seed}: invoice line {defect['line_number']} ({defect['sku']}) is billed at "
            f"{defect['actual']} against {defect['expected']} on the order",
        )

        # 3. The analyst uploads them.
        run_tag = f"demo-{seed}"
        for short, entry in entries.items():
            path = folder / entry["file"]
            uploaded = await self._call(
                "POST",
                "/api/v1/documents",
                users.analyst,
                step="3",
                expect=201,
                files={"file": (f"{run_tag}-{short}.pdf", path.read_bytes(), entry["mime_type"])},
                data={"sensitivity": "INTERNAL"},
            )
            result.documents[short] = uploaded["id"]
        self._say("3", "Upload as the analyst", *(f"{k}: {v}" for k, v in result.documents.items()))

        # 4-7. Classified, read (text layer or OCR), fields extracted and normalized.
        lines = []
        for short, document_id in result.documents.items():
            detail = await self._processed(document_id)
            expected_type = entries[short]["document_type"]
            if detail["status"] == "FAILED" or detail["document_type"] != expected_type:
                msg = (
                    f"{short} ended {detail['status']} as {detail['document_type']} "
                    f"(expected {expected_type})"
                )
                raise DemoError("4", msg)
            classification = detail.get("classification") or {}
            lines.append(
                f"{short}: {detail['document_type']} ({_pct(classification.get('confidence'))}, "
                f"{classification.get('method', 'n/a')}), {detail['status']}"
            )
        self._say("4", "Classify (processed by the worker)", *lines)
        extraction = await self._call(
            "GET",
            f"/api/v1/documents/{result.documents['INV']}/extraction",
            users.analyst,
            step="5",
        )
        fields = {item["field_path"]: item for item in extraction["fields"]}
        self._say(
            "5-7",
            "Read the text, extract the fields, normalize them",
            f"extraction {extraction['method']}, overall confidence "
            f"{_pct(extraction['overall_confidence'])}, routed {extraction['review_level']}",
            *(
                f"{name}: {_value(fields[name])}"
                for name in (
                    "invoice_number",
                    "vendor_name",
                    "purchase_order_number",
                    "total",
                    "currency",
                )
                if name in fields
            ),
        )
        row = next(
            (
                path.removesuffix(".sku")
                for path, item in fields.items()
                if path.endswith(".sku") and item.get("original_value") == defect["sku"]
            ),
            None,
        )
        price = fields.get(f"{row}.unit_price") if row else None
        if price is None or Decimal(str(_typed(price))) != Decimal(defect["actual"]):
            shown = _value(price) if price else "not found"
            raise DemoError("6", f"the unit price of {defect['sku']} was read as {shown}")
        self._out(f"      line {defect['sku']}: unit price {_value(price)}")

        # 8-9. Compared with its order and delivery: the difference is a rule failure.
        findings = await self._call(
            "GET", f"/api/v1/documents/{result.documents['INV']}/findings", users.analyst, step="8"
        )
        failing = [r for r in findings["rule_results"] if r["outcome"] == "FAIL"]
        if not any(r["rule_code"] == EXPECTED_RULE for r in failing):
            codes = ", ".join(r["rule_code"] for r in failing) or "none"
            raise DemoError("9", f"{EXPECTED_RULE} did not fail (failing rules: {codes})")
        self._say(
            "8-9",
            "Compare with the order and the delivery note; detect the mismatch",
            *(
                f"comparison {c['comparison_type']}: {_summary(c['summary'])}"
                for c in findings["comparisons"]
            ),
            *(f"FAIL {r['rule_code']}: {r['message']}" for r in failing),
        )

        # 10-12. Invoice processing: policy retrieved, the agent analyses and recommends.
        started = await self._call(
            "POST",
            "/api/v1/workflows",
            users.analyst,
            step="10",
            expect=202,
            json={"workflow_type": "INVOICE_PROCESSING", "document_id": result.documents["INV"]},
        )
        result.workflow_id = started["id"]
        workflow = await self._settled(result.workflow_id)
        analysis = workflow.get("analysis") or {}
        sources = analysis.get("sources") or []
        if not sources:
            msg = "no policy was retrieved; load the knowledge base (make seed-knowledge)"
            raise DemoError("10", msg)
        self._say(
            "10",
            "Retrieve the procurement policy",
            *dict.fromkeys(
                f"{s['title']} {s['version_label'] or ''} § {s['section_path']}" for s in sources
            ),
        )
        recommendation = analysis.get("recommendation") or {}
        self._say(
            "11-12",
            "The agent analyses the evidence and recommends",
            f"summary: {analysis.get('summary', '')}",
            f"recommendation: {recommendation.get('action')} ({recommendation.get('risk')} risk, "
            f"from the {recommendation.get('source')}), confidence "
            f"{(analysis.get('confidence') or {}).get('level', 'n/a')}",
            f"rationale: {recommendation.get('rationale', '')}",
        )
        (action,) = workflow["actions"]
        if workflow["status"] != "AWAITING_APPROVAL" or action["action_type"] != EXPECTED_ACTION:
            msg = f"workflow {workflow['status']} proposing {action['action_type']}"
            raise DemoError("12", f"{msg} (expected {EXPECTED_ACTION} awaiting approval)")

        # 13. A person reviews: the analyst may not decide their own case; a reviewer may.
        mine = await self._call(
            "GET", f"/api/v1/workflows/{result.workflow_id}", users.analyst, step="13"
        )
        blockers = mine["actions"][0]["blockers"]
        refused = await self._client.post(
            f"/api/v1/workflows/{result.workflow_id}/approve",
            headers={"Authorization": f"Bearer {users.analyst}"},
            json={"reason": "Approving my own upload."},
        )
        if refused.status_code != httpx.codes.FORBIDDEN:
            raise DemoError("13", f"the analyst's own approval returned HTTP {refused.status_code}")
        self._say(
            "13",
            "A person reviews the proposal",
            f"the analyst is refused (maker-checker): {'; '.join(blockers)}",
            f"proposed: {action['title']} — needs a {action['required_role']}",
        )

        # 14-15. The reviewer approves; the executor records the result and a report.
        done = await self._call(
            "POST",
            f"/api/v1/workflows/{result.workflow_id}/approve",
            users.reviewer,
            step="14",
            json={"reason": "Price checked against the order: ask the vendor."},
        )
        executed = done["actions"][0]
        if (done["status"], executed["status"]) != ("COMPLETED", "EXECUTED"):
            raise DemoError("14", f"workflow {done['status']}, action {executed['status']}")
        self._say(
            "14", "The reviewer approves", f"workflow {done['status']}, outcome {done['outcome']}"
        )
        execution = executed.get("execution_result") or {}
        if not done["report_ids"]:
            raise DemoError("15", "the workflow produced no report")
        result.report_id = done["report_ids"][0]
        verified = await self._call(
            "POST", f"/api/v1/reports/{result.report_id}/verify", users.reviewer, step="15"
        )
        if not verified["matches"]:
            raise DemoError("15", "the workflow report does not re-render to its hash")
        message = str(execution.get("message", "")).strip().splitlines()
        self._say(
            "15",
            "The workflow result",
            "a message for the vendor, drafted and not sent:",
            *(f"  | {line}" for line in message[:6]),
            f"report {result.report_id}: verified, SHA-256 {verified['content_sha256'][:16]}…",
        )

        # 16. The audit log records who did what.
        events = await self._call(
            "GET",
            "/api/v1/audit-logs",
            users.admin,
            step="16",
            params={"action": "workflow.", "limit": 50},
        )
        ours = [e for e in events["items"] if _about(e, result.workflow_id, action["id"])]
        approved = [e for e in ours if e["action"] == "workflow.action.approved"]
        if not approved:
            raise DemoError("16", "no workflow.action.approved event for this workflow")
        self._say(
            "16",
            "The audit log records it",
            *(
                f"{e['occurred_at'][:19]} {e['action']} by "
                f"{(e['actor'] or {}).get('email') or e['actor_type'].lower()}"
                for e in reversed(ours)
            ),
        )

        # 17. The dashboard shows it.
        summary = await self._call(
            "GET", "/api/v1/dashboard/summary", users.reviewer, step="17", params={"days": 1}
        )
        failing_rules = {
            r["rule_code"]: r["documents"] for r in summary["discrepancies"]["by_rule"]
        }
        activity = [a for a in summary["activity"] if a["workflow_id"] == result.workflow_id]
        if EXPECTED_RULE not in failing_rules or not activity:
            raise DemoError("17", "the dashboard does not show the invoice's rule or the workflow")
        self._say(
            "17",
            "The dashboard displays it",
            f"documents failing {EXPECTED_RULE}: {failing_rules[EXPECTED_RULE]}; workflows "
            f"finished today: {summary['workflows']['finished_in_period']}",
            *(f"activity: {a['actor']} {a['action']}" for a in activity[:3]),
        )

        result.links = [
            f"{self._web}/documents/{result.documents['INV']}",
            f"{self._web}/workflows/{result.workflow_id}",
            f"{self._web}/reports/{result.report_id}",
            f"{self._web}/admin/audit",
            f"{self._web}/dashboard",
        ]
        self._say("done", "Open it in the web app", *result.links)
        return result

    async def _processed(self, document_id: str) -> dict[str, Any]:
        async def finished() -> dict[str, Any] | None:
            detail: dict[str, Any] = await self._call(
                "GET", f"/api/v1/documents/{document_id}", self._users.analyst, step="4"
            )
            return detail if detail["status"] in FINISHED_DOCUMENT else None

        result: dict[str, Any] = await self._until("4", f"processing of {document_id}", finished)
        return result

    async def _settled(self, workflow_id: str) -> dict[str, Any]:
        async def settled() -> dict[str, Any] | None:
            workflow: dict[str, Any] = await self._call(
                "GET", f"/api/v1/workflows/{workflow_id}", self._users.analyst, step="10"
            )
            return workflow if workflow["status"] in SETTLED_WORKFLOW else None

        result: dict[str, Any] = await self._until("10", "the investigation", settled)
        return result


def _truth(folder: Path, entry: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((folder / entry["ground_truth"]).read_text("utf-8"))
    return data


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.0f}%"


def _typed(field_: dict[str, Any]) -> Any:
    normalized = field_.get("normalized_value") or {}
    return normalized.get("value", field_.get("original_value"))


def _value(field_: dict[str, Any]) -> str:
    typed = _typed(field_)
    printed = field_.get("original_value")
    shown = f"{typed}" if typed == printed or printed is None else f"{typed} (printed {printed!r})"
    return f"{shown}, confidence {_pct(field_.get('confidence'))}"


def _summary(counts: dict[str, int]) -> str:
    return ", ".join(f"{count} {name.lower()}" for name, count in counts.items() if count)


def _about(event: dict[str, Any], workflow_id: str, action_id: str) -> bool:
    details = event.get("details") or {}
    return (
        event.get("entity_id") in (workflow_id, action_id)
        or details.get("workflow_id") == workflow_id
    )
