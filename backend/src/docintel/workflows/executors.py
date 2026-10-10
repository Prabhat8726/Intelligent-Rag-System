"""Allowlisted executors: the only code that carries out a workflow action.

Each executor re-checks, against the data as it is now, that the action still makes sense
(the document version it was proposed for is current; an approval still has nothing failing
against it) and refuses otherwise. They change only this platform's records - no payment
system, mailbox or e-mail is connected - and say so in their result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.audit.service import RequestMeta
from docintel.core.config import Settings
from docintel.db.models import (
    Document,
    DocumentType,
    ReviewPriority,
    ReviewResolution,
    RuleOutcome,
    RuleResultRecord,
    User,
    Workflow,
    WorkflowAction,
    WorkflowActionType,
)
from docintel.review.service import ReviewRequestService, ReviewService, open_task

ATTENTION = (RuleOutcome.FAIL, RuleOutcome.WARN, RuleOutcome.ERROR)
NO_EXTERNAL_SYSTEM = "Recorded in this platform only; no external system is connected."


class ExecutionRefusedError(Exception):
    """The action no longer makes sense; the message is shown to the user."""


@dataclass(slots=True)
class ExecutionContext:
    session: AsyncSession
    settings: Settings
    workflow: Workflow
    action: WorkflowAction
    document: Document  # locked (department, then document)
    actor: User  # the approver; for actions without approval, the workflow's initiator
    meta: RequestMeta


Executor = Callable[[ExecutionContext], Awaitable[dict[str, Any]]]


def _require_current(context: ExecutionContext) -> None:
    document = context.document
    if document.deleted_at is not None:
        raise ExecutionRefusedError("The document was deleted.")
    if document.current_version_id != context.action.document_version_id:
        msg = (
            "A newer version of the document was uploaded after the proposal; "
            "start a new workflow for it."
        )
        raise ExecutionRefusedError(msg)


async def _attention(context: ExecutionContext) -> list[str]:
    """Stored rule results of the version that are not PASS (or not applicable)."""
    rows = await context.session.scalars(
        select(RuleResultRecord).where(
            RuleResultRecord.document_id == context.document.id,
            RuleResultRecord.document_version_id == context.action.document_version_id,
            RuleResultRecord.outcome.in_(ATTENTION),
        )
    )
    return [f"{row.rule_code}: {row.message}" for row in rows]


async def _require_clean(context: ExecutionContext, what: str) -> None:
    problems = await _attention(context)
    if problems:
        shown = "; ".join(problems[:3])
        msg = f"The {what} no longer passes its checks: {shown}"
        raise ExecutionRefusedError(msg[:1000])
    if await open_task(context.session, context.document.id) is not None:
        msg = f"The {what} has an open review task; a reviewer must resolve it first."
        raise ExecutionRefusedError(msg)


def _subject(context: ExecutionContext) -> dict[str, Any]:
    return dict(context.action.payload.get("document") or {})


def _issue_text(statement: str) -> str:
    """'Rule name (CODE, SEVERITY): FAIL - message' -> 'message' (what a vendor can act on)."""
    head, separator, message = statement.partition(" - ")
    return message.strip() if separator and "):" in head else statement.strip()


async def _request_review(
    context: ExecutionContext, reason: str, priority: ReviewPriority
) -> dict[str, Any]:
    outcome = await ReviewRequestService(context.session, context.settings).request(
        context.actor,
        context.document.id,
        reason=reason[:1000],
        priority_level=priority,
        meta=context.meta,
        agent_run_id=context.action.agent_run_id,
    )
    return {
        "review_request_id": str(outcome.request.id),
        "review_task_id": str(outcome.task.id) if outcome.task else None,
        "created": outcome.created,
    }


# ------------------------------------------------------------------------------ executors
async def approve_for_payment(context: ExecutionContext) -> dict[str, Any]:
    _require_current(context)
    if context.document.document_type != DocumentType.INVOICE:
        raise ExecutionRefusedError("Only invoices can be approved for payment.")
    await _require_clean(context, "invoice")
    return {
        "decision": "APPROVED_FOR_PAYMENT",
        "payment_reference": f"PAY-{context.action.id.hex[:10].upper()}",
        "invoice": _subject(context),
        "approved_by": context.actor.email,
        "note": f"Released for the next payment run. {NO_EXTERNAL_SYSTEM}",
    }


async def reject_duplicate(context: ExecutionContext) -> dict[str, Any]:
    _require_current(context)
    task = await open_task(context.session, context.document.id)
    task_id: str | None = None
    if task is not None:
        note = (
            f"Rejected as a duplicate: workflow action {str(context.action.id)[:8]} approved by "
            f"{context.actor.email}."
        )
        resolved = await ReviewService(context.session, context.settings).resolve(
            context.actor, task.id, ReviewResolution.REJECTED, note, context.meta
        )
        task_id = str(resolved.id)
    return {
        "decision": "REJECTED_AS_DUPLICATE",
        "review_task_id": task_id,
        "issues": list(context.action.payload.get("issues", [])),
        "note": f"Not to be paid. Tell the vendor if they expect payment. {NO_EXTERNAL_SYSTEM}",
    }


def clarification_letter(subject: dict[str, Any], issues: list[str]) -> str:
    """A draft for the vendor, from the recorded discrepancies (deterministic)."""
    vendor = subject.get("vendor_name") or "Supplier"
    number = subject.get("document_number") or subject.get("filename") or "your invoice"
    dated = f" dated {subject['document_date']}" if subject.get("document_date") else ""
    lines = [f"Dear {vendor},", ""]
    lines.append(
        f"We could not match invoice {number}{dated} to our purchase order and delivery records:"
    )
    if issues:
        lines.extend(f"- {_issue_text(issue)}" for issue in issues)
    else:
        lines.append("- (the discrepancies are listed in the attached report)")
    lines += [
        "",
        f"Please confirm the correct values or send a corrected invoice or a credit note, "
        f"quoting {number}.",
        "",
        "Kind regards,",
        "Accounts Payable",
    ]
    return "\n".join(lines)


async def request_vendor_clarification(context: ExecutionContext) -> dict[str, Any]:
    _require_current(context)
    issues = list(context.action.payload.get("issues", []))
    letter = clarification_letter(_subject(context), issues)
    review = await _request_review(
        context,
        f"Waiting for the vendor's clarification (workflow {str(context.workflow.id)[:8]}): "
        + "; ".join(_issue_text(issue) for issue in issues[:3]),
        ReviewPriority.NORMAL,
    )
    return {
        "decision": "AWAITING_VENDOR_CLARIFICATION",
        "message": letter,
        "sent": False,
        **review,
        "note": "Send the message from your mailbox; the invoice stays in the review queue "
        f"until the clarification is recorded. {NO_EXTERNAL_SYSTEM}",
    }


async def hold_for_review(context: ExecutionContext) -> dict[str, Any]:
    _require_current(context)
    review = await _request_review(
        context,
        f"Workflow {str(context.workflow.id)[:8]}: {context.action.rationale}",
        ReviewPriority.NORMAL,
    )
    return {"decision": "SENT_TO_REVIEW", **review}


async def request_legal_review(context: ExecutionContext) -> dict[str, Any]:
    _require_current(context)
    review = await _request_review(
        context,
        f"Legal review (workflow {str(context.workflow.id)[:8]}): {context.action.rationale}",
        ReviewPriority.HIGH,
    )
    return {"decision": "SENT_TO_LEGAL_REVIEW", **review}


async def approve_contract(context: ExecutionContext) -> dict[str, Any]:
    _require_current(context)
    if context.document.document_type != DocumentType.CONTRACT:
        raise ExecutionRefusedError("Only contracts can be approved as contracts.")
    await _require_clean(context, "contract")
    return {
        "decision": "CONTRACT_APPROVED",
        "contract": _subject(context),
        "approved_by": context.actor.email,
        "note": f"Approved for signature. {NO_EXTERNAL_SYSTEM}",
    }


EXECUTORS: dict[WorkflowActionType, Executor] = {
    WorkflowActionType.APPROVE_FOR_PAYMENT: approve_for_payment,
    WorkflowActionType.REJECT_DUPLICATE: reject_duplicate,
    WorkflowActionType.REQUEST_VENDOR_CLARIFICATION: request_vendor_clarification,
    WorkflowActionType.HOLD_FOR_REVIEW: hold_for_review,
    WorkflowActionType.REQUEST_LEGAL_REVIEW: request_legal_review,
    WorkflowActionType.APPROVE_CONTRACT: approve_contract,
}
