"""Workflow evaluation (Modules 17, 18, 30): proposals, human approval with maker-checker,
execution, audit trail and report reproducibility of the invoice processing and contract review
workflows, end to end through the job queue.

Synthetic invoice bundles (the agent suite's scenarios and seeds) and synthetic contract families
(three versions each) are uploaded and processed by the worker in a scratch database next to the
seed knowledge base. A manager starts each workflow; another manager decides. Expected outcomes
come from the generator's ground truth, never from the system:

* invoices: clean -> APPROVE_FOR_PAYMENT, planted discrepancy -> HOLD_FOR_REVIEW (or
  REQUEST_VENDOR_CLARIFICATION), duplicate -> REJECT_DUPLICATE. Proposing payment of a defective
  invoice counts as unsafe.
* contracts: a version that follows the Contract Management Guidelines (required clauses, notice
  of at most 90 days, Ohio law, not expiring) -> APPROVE_CONTRACT; any deviation ->
  REQUEST_LEGAL_REVIEW. Approving a deviating contract counts as unsafe. The contract rules'
  outcomes are scored per version, and the version comparison step against the recorded edits.
* A proposal held only because the document has an open review task (approval waits for the
  reviewer) is counted separately; a second reviewer then resolves the task and the workflow
  runs again, as in production.
* maker-checker: before the approver decides, the manager who started the workflow, the
  reviewer who uploaded the document and (for manager-level actions) a reviewer try to approve
  or reject through the service, and the starter's decision is written to the table directly.
  Every attempt must be refused; each refusal must be audited.
* audit: every state change of every action has its audit event.
* reports: the workflow's report re-renders to its stored hash, and the same report generated
  twice from unchanged data has the same SHA-256.

Deterministic mode only: model-assisted proposals need a key and are not measured here.
"""

from __future__ import annotations

import io
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import UploadFile
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.datastructures import Headers

from docintel.audit.service import SYSTEM_REQUEST
from docintel.core.config import Settings
from docintel.core.errors import PermissionDeniedError
from docintel.db.models import (
    AgentRun,
    AuditLog,
    Department,
    Document,
    Report,
    ReviewResolution,
    Role,
    RuleResultRecord,
    Sensitivity,
    User,
    WorkflowType,
)
from docintel.documents.service import DocumentService
from docintel.evaluation.agent_suite import (
    DATASET_SEED,
    EXPECTED_ACTIONS,
    HOLDOUT_SEED,
    SCENARIOS,
    Truth,
    _process,
    _worker,
)
from docintel.evaluation.report import Report as EvaluationReport
from docintel.evaluation.report import environment, pct
from docintel.evaluation.retrieval_suite import EVAL_DATE, _settings, scratch_database
from docintel.evaluation.versions_suite import NOT_CLAUSES, same_title
from docintel.reports.service import ReportService
from docintel.review.service import ReviewService, open_task
from docintel.storage import LocalStorage
from docintel.synthetic.contracts import generate_contract_versions
from docintel.workers.runner import Worker
from docintel.workflows.definitions import definition_for
from docintel.workflows.service import WorkflowService

CONTRACT_SEED = 73  # never used while developing the contract rules (seed 167 was)
CONTRACT_FAMILIES = 12
CONTRACT_RULES = (
    "CONTRACT_REQUIRED_CLAUSES",
    "CONTRACT_TERMINATION_NOTICE",
    "CONTRACT_GOVERNING_LAW",
    "CONTRACT_EXPIRY",
)
MAX_NOTICE_DAYS = 90
EXPIRY_WARN_DAYS = 30
APPROVED_LAW = "Ohio"
HELD_FOR_REVIEW = "open review task"  # in the rationale of an approval held for a reviewer
NOT_CHANGES = {key.title() for key in NOT_CLAUSES}  # "Preamble", "Signatures"


class _EvalDate(date):
    """`date` whose today() is the evaluation date: contract expiry does not drift."""

    @classmethod
    def today(cls) -> _EvalDate:
        return cls(EVAL_DATE.year, EVAL_DATE.month, EVAL_DATE.day)


@dataclass(slots=True)
class People:
    uploader: User  # reviewer who uploads every document (a maker)
    starter: User  # manager who starts every workflow (a maker)
    approver: User  # manager who decides (the checker)
    checker: User  # second reviewer: resolves review tasks; too junior for manager-level actions
    admin: User


@dataclass(slots=True)
class Case:
    case_id: str
    kind: str  # "invoice" or "contract"
    document_id: str
    workflow_type: WorkflowType
    expected: frozenset[str]
    scenario: str
    defects: list[str] = field(default_factory=list)
    version: int = 1
    runs: list[dict[str, Any]] = field(default_factory=list)  # first run, then after review
    probes: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)
    reports: dict[str, Any] = field(default_factory=dict)
    rules: dict[str, tuple[str, str | None]] = field(default_factory=dict)  # expected, found
    changes: dict[str, Any] = field(default_factory=dict)

    @property
    def final(self) -> dict[str, Any]:
        return self.runs[-1] if self.runs else {}


# ------------------------------------------------------------------------------ setup
async def _people(maker: async_sessionmaker[AsyncSession], uploader: User) -> People:
    async with maker() as session:
        finance = await session.scalar(select(Department).where(Department.name == "Finance"))
        assert finance is not None  # noqa: S101 - created by _process
        users = {
            name: User(
                email=f"{name}@eval.invalid",
                full_name=f"Evaluation {name}",
                password_hash="not-used",  # noqa: S106  (no login in the evaluation)
                role=role,
                department_id=finance.id,
            )
            for name, role in (
                ("starter", Role.MANAGER),
                ("approver", Role.MANAGER),
                ("checker", Role.REVIEWER),
            )
        }
        session.add_all(users.values())
        await session.commit()
        admin = await session.scalar(select(User).where(User.role == Role.ADMIN))
        assert admin is not None  # noqa: S101 - created by _process
    return People(uploader, users["starter"], users["approver"], users["checker"], admin)


# ------------------------------------------------------------------------------ one workflow
async def _run_workflow(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    worker: Worker,
    people: People,
    case: Case,
) -> None:
    async with maker() as session:
        workflow = await WorkflowService(session, settings).start(
            people.starter, case.workflow_type, uuid.UUID(case.document_id), meta=SYSTEM_REQUEST
        )
        await session.commit()
        workflow_id = workflow.id
    started = time.perf_counter()
    await worker.run_until_idle()
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    case.runs.append({"workflow_id": str(workflow_id), "latency_ms": elapsed})
    await _record(maker, settings, people, case)


async def _record(
    maker: async_sessionmaker[AsyncSession], settings: Settings, people: People, case: Case
) -> None:
    """The latest run's workflow, action and investigation, as stored."""
    run = case.runs[-1]
    async with maker() as session:
        workflow = await WorkflowService(session, settings).get(
            people.admin, uuid.UUID(run["workflow_id"])
        )
        action = workflow.actions[0] if workflow.actions else None
        investigation = (
            await session.get(AgentRun, workflow.agent_run_id) if workflow.agent_run_id else None
        )
        result = (investigation.result or {}) if investigation else {}
        run.update(
            status=workflow.status.value,
            outcome=workflow.outcome,
            error=workflow.error,
            steps={step.step_name: step.status.value for step in workflow.steps},
            compared=next(
                (s.output for s in workflow.steps if s.step_name == "compare_versions"), None
            ),
            recommendation=(result.get("recommendation") or {}).get("action"),
            confidence=(result.get("confidence") or {}).get("level"),
            action=action.action_type.value if action else None,
            action_status=action.status.value if action else None,
            required_role=action.required_role if action else None,
            held_for_review=bool(action and HELD_FOR_REVIEW in action.rationale),
            rationale=action.rationale[:300] if action else None,
            transitions=[t.to_status.value for t in action.transitions] if action else [],
        )


async def _resolve_review(
    maker: async_sessionmaker[AsyncSession], settings: Settings, people: People, document_id: str
) -> bool:
    """The second reviewer accepts what the document's open review task lists."""
    async with maker() as session:
        task = await open_task(session, uuid.UUID(document_id))
        if task is None:
            return False
        task_id = task.id
        await session.rollback()
        await ReviewService(session, settings).resolve(
            people.checker, task_id, ReviewResolution.APPROVED, "Values checked.", SYSTEM_REQUEST
        )
        await session.commit()
    return True


async def _try_decide(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor: User,
    workflow_id: uuid.UUID,
    *,
    approve: bool,
) -> bool:
    """True when the service refused the decision."""
    async with maker() as session:
        try:
            await WorkflowService(session, settings).decide(
                actor,
                workflow_id,
                approve=approve,
                reason=None if approve else "Evaluation probe.",
                meta=SYSTEM_REQUEST,
            )
        except PermissionDeniedError:
            return True
    return False


async def _probe_and_decide(
    maker: async_sessionmaker[AsyncSession], settings: Settings, people: People, case: Case
) -> None:
    """Maker-checker probes on a proposal awaiting approval, then the checker approves."""
    run = case.final
    workflow_id = uuid.UUID(run["workflow_id"])
    attempts: list[tuple[str, User, bool]] = [
        ("starter approves", people.starter, True),
        ("starter rejects", people.starter, False),
        ("uploader approves", people.uploader, True),
    ]
    if run["required_role"] == Role.MANAGER.value:
        attempts.append(("reviewer approves a manager-level action", people.checker, True))
    refused = {
        label: await _try_decide(maker, settings, user, workflow_id, approve=approve)
        for label, user, approve in attempts
    }
    async with maker() as session, session.begin():
        try:
            async with session.begin_nested():
                await session.execute(
                    text(
                        "UPDATE workflow_actions SET decided_by_id = :maker, decided_at = now(), "
                        "status = 'APPROVED' WHERE workflow_id = :workflow"
                    ),
                    {"maker": people.starter.id, "workflow": workflow_id},
                )
            refused["starter's decision written to the table"] = False
        except DBAPIError:
            refused["starter's decision written to the table"] = True
        denials = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "workflow.approval_denied",
                AuditLog.details["workflow_id"].astext == str(workflow_id),
            )
        )
    service_attempts = len(attempts)
    case.probes = {
        "attempts": len(refused),
        "refused": sum(refused.values()),
        "bypasses": [label for label, ok in refused.items() if not ok],
        "service_refusals_audited": (denials or 0) == service_attempts,
    }
    if case.probes["bypasses"]:  # a maker decided: the proposal is no longer pending
        case.decision = {"status": "BYPASSED"}
        await _record(maker, settings, people, case)
        return
    started = time.perf_counter()
    async with maker() as session:
        decided = await WorkflowService(session, settings).decide(
            people.approver, workflow_id, approve=True, reason="Checked.", meta=SYSTEM_REQUEST
        )
        action = decided.actions[0]
        case.decision = {
            "status": decided.status.value,
            "outcome": decided.outcome,
            "action_status": action.status.value,
            "error": action.error,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    await _record(maker, settings, people, case)


async def _audit(maker: async_sessionmaker[AsyncSession], case: Case) -> None:
    """Every action state change of every run has its audit event."""
    transitions = 0
    events = 0
    async with maker() as session:
        for run in case.runs:
            transitions += len(run["transitions"])
            events += (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.action.like("workflow.action.%"),
                        AuditLog.details["workflow_id"].astext == run["workflow_id"],
                    )
                )
                or 0
            )
    case.audit = {"transitions": transitions, "events": events, "complete": events == transitions}


async def _reports(
    maker: async_sessionmaker[AsyncSession], settings: Settings, people: People, case: Case
) -> None:
    workflow_id = uuid.UUID(case.final["workflow_id"])
    report_type = definition_for(case.workflow_type).report_type
    async with maker() as session:
        stored = list(
            await session.scalars(select(Report).where(Report.workflow_id == workflow_id))
        )
        workflow_reports = [ReportService.verify(report) for report in stored]
    hashes: list[str] = []
    verified: list[bool] = []
    for _ in range(2):
        async with maker() as session:
            report = await ReportService(session, settings).generate(
                people.approver, report_type, uuid.UUID(case.document_id), meta=SYSTEM_REQUEST
            )
            await session.commit()
            hashes.append(report.content_sha256)
            verified.append(ReportService.verify(report))
    case.reports = {
        "workflow_reports": len(workflow_reports),
        "workflow_reports_verified": sum(workflow_reports),
        "regenerated_identical": hashes[0] == hashes[1],
        "regenerated_verified": all(verified),
    }


async def _complete(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    worker: Worker,
    people: People,
    case: Case,
) -> None:
    """Run the workflow; if approval waits for a reviewer, resolve the task and run it again;
    probe and decide a pending approval; check the audit trail and the reports."""
    await _run_workflow(maker, settings, worker, people, case)
    if case.final["held_for_review"] and await _resolve_review(
        maker, settings, people, case.document_id
    ):
        await _run_workflow(maker, settings, worker, people, case)
    if case.final["status"] == "AWAITING_APPROVAL":
        await _probe_and_decide(maker, settings, people, case)
    await _audit(maker, case)
    if case.final["status"] == "COMPLETED":
        await _reports(maker, settings, people, case)


# ------------------------------------------------------------------------------ invoices
def invoice_cases(ids: dict[str, str], truths: dict[str, Truth]) -> list[Case]:
    cases: list[Case] = []
    for doc_id in sorted(ids):
        truth = truths[doc_id]
        if truth["document_type"] != "INVOICE":
            continue
        defects = [defect["code"] for defect in truth["defects"]]
        expected = frozenset({"APPROVE_FOR_PAYMENT"})
        for code in defects:
            expected = EXPECTED_ACTIONS.get(code, frozenset({"HOLD_FOR_REVIEW"}))
        scenario = str(truth["scenario"])
        if scenario == "DUPLICATE_INVOICE":  # the original and its copy
            scenario += " (copy)" if defects else " (original)"
        cases.append(
            Case(
                doc_id,
                "invoice",
                ids[doc_id],
                WorkflowType.INVOICE_PROCESSING,
                expected,
                scenario,
                defects=defects,
            )
        )
    return cases


# ------------------------------------------------------------------------------ contracts
def expected_contract_rules(version: dict[str, Any], today: date) -> dict[str, str]:
    """What the contract rules must conclude for a version, from the generator's record."""
    truth = version["guidelines"]
    notice = truth["termination_notice_days"]
    law = truth["governing_law"]
    expires = date.fromisoformat(version["expiration_date"])
    return {
        "CONTRACT_REQUIRED_CLAUSES": "FAIL" if truth["missing_required"] else "PASS",
        "CONTRACT_TERMINATION_NOTICE": "NOT_APPLICABLE"
        if notice is None
        else "FAIL"
        if notice > MAX_NOTICE_DAYS
        else "PASS",
        "CONTRACT_GOVERNING_LAW": "NOT_APPLICABLE"
        if law is None
        else "PASS"
        if APPROVED_LAW in law
        else "WARN",
        "CONTRACT_EXPIRY": "FAIL"
        if expires < today
        else "WARN"
        if expires <= today + timedelta(days=EXPIRY_WARN_DAYS)
        else "PASS",
    }


def contract_deviations(expected: dict[str, str]) -> list[str]:
    return [code for code, outcome in expected.items() if outcome in ("FAIL", "WARN")]


async def _upload(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: LocalStorage,
    people: People,
    content: bytes,
    filename: str,
    document_id: str | None,
) -> str:
    upload = UploadFile(
        io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": "application/pdf"}),
    )
    async with maker() as session:
        service = DocumentService(session, storage, settings)
        if document_id is None:
            document = await service.upload(
                actor=people.uploader,
                upload=upload,
                sensitivity=Sensitivity.INTERNAL,
                department_id=None,
                meta=SYSTEM_REQUEST,
            )
        else:
            document = await service.upload_version(
                actor=people.uploader,
                document_id=uuid.UUID(document_id),
                upload=upload,
                meta=SYSTEM_REQUEST,
            )
    return str(document.id)


async def _stored_rules(
    maker: async_sessionmaker[AsyncSession], document_id: str
) -> tuple[str | None, dict[str, str]]:
    """The document's type and the latest outcome of each contract rule on its current version."""
    async with maker() as session:
        document = await session.get(Document, uuid.UUID(document_id))
        assert document is not None  # noqa: S101
        rows = await session.scalars(
            select(RuleResultRecord)
            .where(
                RuleResultRecord.document_version_id == document.current_version_id,
                RuleResultRecord.rule_code.in_(CONTRACT_RULES),
            )
            .order_by(RuleResultRecord.evaluated_at)
        )
        outcomes = {row.rule_code: row.outcome.value for row in rows}
        kind = document.document_type.value if document.document_type else None
    return kind, outcomes


def score_changes(compared: dict[str, Any] | None, truth: dict[str, list[str]]) -> dict[str, Any]:
    """Changed clause titles of the compare_versions step against the recorded edits."""
    found = [
        (item["change"].lower(), item["title"])
        for item in (compared or {}).get("clauses", [])
        if item["title"] not in NOT_CHANGES
    ]
    expected = [(change, title) for change, titles in truth.items() for title in titles]
    remaining = list(expected)
    hits = 0
    for change, title in found:
        match = next(
            (item for item in remaining if item[0] == change and same_title(title, item[1])), None
        )
        if match is not None:
            remaining.remove(match)
            hits += 1
    return {
        "tp": hits,
        "fp": len(found) - hits,
        "fn": len(expected) - hits,
        "exact": hits == len(found) == len(expected),
    }


async def _contracts(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    storage: LocalStorage,
    worker: Worker,
    people: People,
    root: Path,
    *,
    families: int,
) -> list[Case]:
    manifest = generate_contract_versions(root, seed=CONTRACT_SEED, families=families)
    cases: list[Case] = []
    for family in manifest["contracts"]:
        document_id: str | None = None
        for index, (name, version) in enumerate(
            zip(family["files"], family["versions"], strict=True)
        ):
            content = (root / name).read_bytes()
            document_id = await _upload(
                maker, settings, storage, people, content, name, document_id
            )
            await worker.run_until_idle()
            expected_rules = expected_contract_rules(version, EVAL_DATE)
            deviations = contract_deviations(expected_rules)
            case = Case(
                f"{family['family']}-v{version['version']}",
                "contract",
                document_id,
                WorkflowType.CONTRACT_REVIEW,
                frozenset({"REQUEST_LEGAL_REVIEW"} if deviations else {"APPROVE_CONTRACT"}),
                "deviates" if deviations else "follows the guidelines",
                defects=deviations,
                version=version["version"],
            )
            kind, found = await _stored_rules(maker, document_id)
            case.rules = {
                code: (outcome, found.get(code)) for code, outcome in expected_rules.items()
            }
            if kind != "CONTRACT":
                case.runs.append({"status": "NOT_STARTED", "error": f"classified as {kind}"})
                cases.append(case)
                continue
            await _complete(maker, settings, worker, people, case)
            if index > 0:
                case.changes = score_changes(
                    case.final.get("compared"), family["changes"][index - 1]
                )
            cases.append(case)
    return cases


# ------------------------------------------------------------------------------ scoring
def score(case: Case) -> dict[str, Any]:
    first = case.runs[0] if case.runs else {}
    final = case.final
    proposed = final.get("action")
    approval = proposed in ("APPROVE_FOR_PAYMENT", "APPROVE_CONTRACT")
    expected_approval = bool({"APPROVE_FOR_PAYMENT", "APPROVE_CONTRACT"} & case.expected)
    return {
        "started": final.get("status") != "NOT_STARTED",
        "first_action": first.get("action"),
        "held_for_review_first": bool(first.get("held_for_review")),
        "action": proposed,
        "correct": proposed in case.expected,
        "unsafe": approval and not expected_approval,
        # The investigation recommended an approval the workflow then held for a reviewer.
        "unsafe_recommendation": first.get("recommendation") == "APPROVE_FOR_PAYMENT"
        and bool(case.defects),
        "final_status": case.decision.get("status") or final.get("status"),
        "outcome": case.decision.get("outcome") or final.get("outcome"),
        "executed": (case.decision.get("action_status") or final.get("action_status"))
        == "EXECUTED",
        "needed_approval": bool(case.probes),
        "bypasses": case.probes.get("bypasses", []),
        "probes": case.probes.get("attempts", 0),
        "refused": case.probes.get("refused", 0),
        "refusals_audited": case.probes.get("service_refusals_audited"),
        "audit_complete": case.audit.get("complete"),
        "transitions": case.audit.get("transitions", 0),
        "audit_events": case.audit.get("events", 0),
        "workflow_report_verified": (
            case.reports["workflow_reports_verified"] == case.reports["workflow_reports"] >= 1
        )
        if case.reports
        else None,
        "report_reproducible": (
            case.reports["regenerated_identical"] and case.reports["regenerated_verified"]
        )
        if case.reports
        else None,
        "rules_correct": sum(1 for want, got in case.rules.values() if want == got),
        "rules_scored": len(case.rules),
        "rule_misses": [
            f"{code}: expected {want}, got {got}"
            for code, (want, got) in case.rules.items()
            if want != got
        ],
        "changes": case.changes or None,
        "latency_ms": [run["latency_ms"] for run in case.runs if "latency_ms" in run],
        "decision_latency_ms": case.decision.get("latency_ms"),
        "errors": [run["error"] for run in case.runs if run.get("error")],
    }


def _rate(values: Sequence[bool | None]) -> float | None:
    known = [value for value in values if value is not None]
    return round(sum(known) / len(known), 4) if known else None


def _percentile(values: Sequence[float], share: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return round(ordered[index], 1)


def summarize(cases: list[Case]) -> dict[str, Any]:
    scored = [(case, score(case)) for case in cases]
    values = [v for _, v in scored]
    expected_approval = [
        v for case, v in scored if {"APPROVE_FOR_PAYMENT", "APPROVE_CONTRACT"} & case.expected
    ]
    expected_stop = [v for v in values if v not in expected_approval]
    latencies = [ms for v in values for ms in v["latency_ms"]]
    decisions = [v["decision_latency_ms"] for v in values if v["decision_latency_ms"] is not None]
    changes = [v["changes"] for v in values if v["changes"]]
    change_counts = Counter[str]()
    for item in changes:
        change_counts.update({key: item[key] for key in ("tp", "fp", "fn")})
    tp, fp, fn = change_counts["tp"], change_counts["fp"], change_counts["fn"]
    return {
        "workflows": len(values),
        "started": sum(v["started"] for v in values),
        "correct": _rate([v["correct"] for v in values]),
        "correct_when_approval_expected": _rate([v["correct"] for v in expected_approval]),
        "correct_when_a_stop_expected": _rate([v["correct"] for v in expected_stop]),
        "unsafe_proposals": sum(v["unsafe"] for v in values),
        "unsafe_recommendations_held": sum(v["unsafe_recommendation"] for v in values),
        "held_for_review_first": sum(v["held_for_review_first"] for v in values),
        "needed_approval": sum(v["needed_approval"] for v in values),
        "executed": sum(v["executed"] for v in values),
        "completed": sum(v["final_status"] == "COMPLETED" for v in values),
        "maker_checker": {
            "attempts": sum(v["probes"] for v in values),
            "refused": sum(v["refused"] for v in values),
            "bypasses": sum(len(v["bypasses"]) for v in values),
            "refusals_audited": _rate([v["refusals_audited"] for v in values]),
        },
        "audit": {
            "transitions": sum(v["transitions"] for v in values),
            "events": sum(v["audit_events"] for v in values),
            "complete": _rate([v["audit_complete"] for v in values]),
        },
        "reports": {
            "workflow_report_verified": _rate([v["workflow_report_verified"] for v in values]),
            "regenerated_identical": _rate([v["report_reproducible"] for v in values]),
        },
        "rules": {
            "correct": sum(v["rules_correct"] for v in values),
            "scored": sum(v["rules_scored"] for v in values),
        },
        "version_changes": {
            "steps": len(changes),
            "exact": _rate([item["exact"] for item in changes]),
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        },
        "latency_ms": {
            "workflow_p50": _percentile(latencies, 0.5),
            "workflow_p95": _percentile(latencies, 0.95),
            "decision_p50": _percentile(decisions, 0.5),
            "decision_p95": _percentile(decisions, 0.95),
        },
        "errors": sum(len(v["errors"]) for v in values),
        "cases": [
            {"case": case.case_id, "scenario": case.scenario, "defects": case.defects, **v}
            for case, v in scored
        ],
    }


# ------------------------------------------------------------------------------ the run
@dataclass(slots=True)
class DatasetRun:
    name: str
    invoices: list[Case]
    contracts: list[Case]


async def _run_dataset(
    database_url: str, name: str, seed: int, *, quick: bool, contracts: bool
) -> DatasetRun:
    with tempfile.TemporaryDirectory(prefix="docintel-workflow-eval-") as tmp:
        async with scratch_database(database_url) as url:
            engine = create_async_engine(url)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            try:
                settings = _settings(url, Path(tmp) / "storage")
                storage = LocalStorage(Path(tmp) / "storage")
                # A fixed "today": policy versions and contract expiry do not drift.
                with (
                    patch("docintel.agent.tools.catalog.today", lambda: EVAL_DATE),
                    patch("docintel.matching.service.date", _EvalDate),
                ):
                    uploader, ids, truths = await _process(
                        maker,
                        settings,
                        storage,
                        Path(tmp) / "data",
                        seed=seed,
                        bundles=1 if quick else 2,
                    )
                    people = await _people(maker, uploader)
                    worker = _worker(maker, settings, storage)
                    invoices = invoice_cases(ids, truths)
                    for case in invoices:
                        await _complete(maker, settings, worker, people, case)
                    contract_cases = (
                        await _contracts(
                            maker,
                            settings,
                            storage,
                            worker,
                            people,
                            Path(tmp) / "contracts",
                            families=3 if quick else CONTRACT_FAMILIES,
                        )
                        if contracts
                        else []
                    )
            finally:
                await engine.dispose()
    return DatasetRun(name, invoices, contract_cases)


def _overall_rows(metrics: dict[str, Any]) -> list[list[str]]:
    checker = metrics["maker_checker"]
    audit = metrics["audit"]
    reports = metrics["reports"]
    latency = metrics["latency_ms"]
    rows = [
        ["Workflows started / completed", f"{metrics['started']} / {metrics['completed']}"],
        ["Final proposal as expected", pct(metrics["correct"])],
        ["... when an approval was expected", pct(metrics["correct_when_approval_expected"])],
        ["... when a stop was expected", pct(metrics["correct_when_a_stop_expected"])],
        ["Unsafe proposals (approval where a stop was expected)", str(metrics["unsafe_proposals"])],
        [
            "Held at first because a review task was open (then resolved and re-run)",
            str(metrics["held_for_review_first"]),
        ],
        ["Proposals that needed a person's approval", str(metrics["needed_approval"])],
        ["Actions executed", str(metrics["executed"])],
        [
            "Maker-checker attempts refused / bypasses",
            f"{checker['refused']}/{checker['attempts']} / {checker['bypasses']}",
        ],
        ["Refusals with an audit event", pct(checker["refusals_audited"])],
        [
            "Action state changes with an audit event",
            f"{audit['events']}/{audit['transitions']} ({pct(audit['complete'])} of workflows)",
        ],
        ["Workflow report re-renders to its stored hash", pct(reports["workflow_report_verified"])],
        ["Report generated twice: identical SHA-256", pct(reports["regenerated_identical"])],
        [
            "Workflow run p50 / p95 (ms, start to proposal, includes the job queue)",
            f"{latency['workflow_p50']} / {latency['workflow_p95']}",
        ],
        [
            "Approval p50 / p95 (ms, decision, execution and report)",
            f"{latency['decision_p50']} / {latency['decision_p95']}",
        ],
        ["Workflow errors", str(metrics["errors"])],
    ]
    if metrics["rules"]["scored"]:
        rules = metrics["rules"]
        changes = metrics["version_changes"]
        rows[5:5] = [
            [
                "Contract rule outcomes as expected",
                f"{rules['correct']}/{rules['scored']} ({pct(rules['correct'] / rules['scored'])})",
            ],
            [
                "Version comparison step: exact change lists / precision / recall",
                f"{pct(changes['exact'])} / {pct(changes['precision'])} / "
                f"{pct(changes['recall'])} ({changes['steps']} steps)",
            ],
        ]
    return rows


def _scenario_rows(cases: list[Case]) -> list[list[str]]:
    groups: dict[str, list[tuple[Case, dict[str, Any]]]] = defaultdict(list)
    for case in cases:
        groups[case.scenario].append((case, score(case)))
    return [
        [
            scenario,
            str(len(items)),
            "/".join(sorted(items[0][0].expected)),
            ", ".join(
                f"{action} x{n}"
                for action, n in sorted(Counter(str(v["action"]) for _, v in items).items())
            ),
            pct(_rate([v["correct"] for _, v in items])),
            ", ".join(
                f"{outcome} x{n}"
                for outcome, n in sorted(Counter(str(v["outcome"]) for _, v in items).items())
            ),
        ]
        for scenario, items in sorted(groups.items())
    ]


def _rule_rows(cases: list[Case]) -> list[list[str]]:
    per_rule: dict[str, Counter[str]] = defaultdict(Counter)
    for case in cases:
        for code, (want, got) in case.rules.items():
            per_rule[code]["scored"] += 1
            per_rule[code]["correct"] += want == got
            per_rule[code][f"expected {want}"] += 1
    return [
        [
            code,
            f"{counts['correct']}/{counts['scored']}",
            ", ".join(
                f"{key.removeprefix('expected ')} x{n}"
                for key, n in sorted(counts.items())
                if key.startswith("expected ")
            ),
        ]
        for code, counts in sorted(per_rule.items())
    ]


async def run_workflow_suite(
    output: Path, *, database_url: str, quick: bool = False
) -> EvaluationReport:
    started = time.perf_counter()
    runs = [
        await _run_dataset(database_url, "development", DATASET_SEED, quick=quick, contracts=True)
    ]
    if not quick:
        runs.append(
            await _run_dataset(database_url, "held-out", HOLDOUT_SEED, quick=quick, contracts=False)
        )
    metrics: dict[str, Any] = {}
    for run in runs:
        metrics[f"invoices ({run.name})"] = summarize(run.invoices)
        if run.contracts:
            metrics["contracts"] = summarize(run.contracts)
    tables: list[tuple[str, list[str], list[list[str]]]] = [
        (f"Invoice processing ({run.name} dataset)", ["Measure", "Value"],
         _overall_rows(metrics[f"invoices ({run.name})"]))
        for run in runs
    ]  # fmt: skip
    contracts = runs[0].contracts
    tables.append(
        (
            f"Contract review (generator seed {CONTRACT_SEED}, three versions per contract)",
            ["Measure", "Value"],
            _overall_rows(metrics["contracts"]),
        )
    )
    for run in runs:
        tables.append(
            (
                f"Invoice processing per scenario ({run.name})",
                ["Scenario", "Workflows", "Expected", "Proposed", "Correct", "Outcome"],
                _scenario_rows(run.invoices),
            )
        )
    tables.append(
        (
            "Contract review per ground truth",
            ["Contract version", "Workflows", "Expected", "Proposed", "Correct", "Outcome"],
            _scenario_rows(contracts),
        )
    )
    tables.append(
        (
            "Contract rules per version",
            ["Rule", "As expected", "Expected outcomes"],
            _rule_rows(contracts),
        )
    )
    misses = [
        [run.name, case.case_id, "/".join(sorted(case.expected)), str(values["action"]),
         "; ".join(values["rule_misses"] + values["errors"])[:160] or "-"]
        for run in runs
        for case in [*run.invoices, *run.contracts]
        if not (values := score(case))["correct"]
    ]  # fmt: skip
    tables.append(
        (
            "Workflows with an unexpected proposal",
            ["Dataset", "Case", "Expected", "Proposed", "Rule differences / errors"],
            misses or [["-", "-", "-", "-", "-"]],
        )
    )
    report = EvaluationReport(
        quick=quick,
        suite="workflow",
        title="Workflow automation evaluation",
        dataset={
            "invoice_development_seed": DATASET_SEED,
            "invoice_held_out_seed": None if quick else HOLDOUT_SEED,
            "bundles_per_scenario": 1 if quick else 2,
            "scenarios": [scenario.value for scenario in SCENARIOS],
            "invoices": {run.name: len(run.invoices) for run in runs},
            "contract_seed": CONTRACT_SEED,
            "contract_families": 3 if quick else CONTRACT_FAMILIES,
            "contract_versions": len(contracts),
            "reference_date": EVAL_DATE.isoformat(),
        },
        config={
            "mode": "deterministic (no LLM): keyword planner, rule-based analysis and proposals",
            "embedding": "hashing (offline)",
            "people": "uploader (reviewer), starter (manager), approver (manager), "
            "second reviewer; all in Finance",
            "workflow_definitions": {
                workflow_type.value: definition_for(workflow_type).version
                for workflow_type in WorkflowType
            },
        },
        metrics=metrics,
        environment=environment(),
        notes=[
            "Expected outcomes come from the generator's ground truth (planted invoice defects; "
            "the clauses, notice period, governing law and expiry each contract version was "
            "written with), not from the system.",
            "An approval is never proposed while the document has an open review task; such "
            "proposals are counted, the task is resolved by a second reviewer and the workflow "
            "runs again. The scores use the final run.",
            "Maker-checker probes go through the same service the API calls; the direct table "
            "write checks the database constraint behind it.",
            "The contract generator was changed in this phase (guideline ground truth; half of "
            "the contracts under the company's own law; notice periods up to 120 days); seed "
            f"{CONTRACT_SEED} was not used while developing the contract rules.",
            "The documents are synthetic and come from the templates the extractors and rules "
            "were developed on: these figures show that the workflows and their controls behave "
            "as designed end to end, not accuracy on real-world documents.",
            "A confirmed price or tax-rate difference against the order is put to the vendor "
            "(REQUEST_VENDOR_CLARIFICATION, decided by a reviewer or above; invoice processing "
            "procedure 3.2, 3.3); other discrepancies are held for review. No contract in this "
            "dataset expires within 30 days of the reference date, so the expiry rule is only "
            "exercised as PASS here (its other outcomes are unit-tested).",
            "Model-assisted proposals (Gemini or a local model): Not yet measured - no model was "
            "available in the build environment.",
            f"Run time: {round(time.perf_counter() - started, 1)} s.",
        ],
        tables=tables,
    )
    report.write(output)
    return report
