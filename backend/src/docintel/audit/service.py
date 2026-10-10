"""Audit trail writer.

Records are added to the caller's session so they commit atomically with the change they
describe. `details` must hold identifiers and metadata only - never document content or secrets.
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from docintel.db.models import ActorType, AuditLog, AuditOutcome, User

_USER_AGENT_MAX = 512


class AuditAction(StrEnum):
    AUTH_LOGIN_SUCCEEDED = "auth.login.succeeded"
    AUTH_LOGIN_FAILED = "auth.login.failed"
    AUTH_ACCOUNT_LOCKED = "auth.account.locked"
    AUTH_LOGOUT = "auth.logout"
    AUTH_REFRESH_REUSED = "auth.refresh_reused"
    AUTHZ_DENIED = "authz.denied"
    USER_CREATED = "user.created"
    DEPARTMENT_CREATED = "department.created"
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_DOWNLOADED = "document.downloaded"
    DOCUMENT_DELETED = "document.deleted"
    DOCUMENT_REPROCESS_REQUESTED = "document.reprocess_requested"
    DOCUMENT_PROCESSING_COMPLETED = "document.processing.completed"
    DOCUMENT_PROCESSING_FAILED = "document.processing.failed"
    DOCUMENT_CLASSIFICATION_CORRECTED = "document.classification.corrected"
    DOCUMENT_FIELD_CORRECTED = "document.extraction.field_corrected"
    VENDOR_CREATED = "vendor.created"
    VENDOR_UPDATED = "vendor.updated"
    DOCUMENT_VERSION_UPLOADED = "document.version.uploaded"
    COMPARISON_CREATED = "comparison.created"
    RULE_UPDATED = "rule.updated"
    RULES_EVALUATED = "rules.evaluated"
    REVIEW_TASK_CLAIMED = "review_task.claimed"
    REVIEW_TASK_RELEASED = "review_task.released"
    REVIEW_TASK_RESOLVED = "review_task.resolved"
    KNOWLEDGE_UPLOADED = "knowledge.uploaded"
    KNOWLEDGE_PROCESSING_COMPLETED = "knowledge.processing.completed"
    KNOWLEDGE_PROCESSING_FAILED = "knowledge.processing.failed"
    KNOWLEDGE_ARCHIVED = "knowledge.archived"
    KNOWLEDGE_QUERIED = "knowledge.queried"
    REVIEW_REQUESTED = "review.requested"
    ANALYSIS_REQUESTED = "analysis.requested"
    ANALYSIS_COMPLETED = "analysis.completed"
    ANALYSIS_FAILED = "analysis.failed"
    API_TOKEN_CREATED = "api_token.created"  # noqa: S105 - an action name
    API_TOKEN_REVOKED = "api_token.revoked"  # noqa: S105 - an action name
    MCP_AUTH_FAILED = "mcp.auth_failed"
    MCP_TOOL_CALLED = "mcp.tool_called"
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_REJECTED = "workflow.rejected"
    WORKFLOW_FAILED = "workflow.failed"
    WORKFLOW_CANCELLED = "workflow.cancelled"
    WORKFLOW_ACTION_PROPOSED = "workflow.action.proposed"
    WORKFLOW_ACTION_AWAITING_APPROVAL = "workflow.action.awaiting_approval"
    WORKFLOW_ACTION_APPROVED = "workflow.action.approved"
    WORKFLOW_ACTION_REJECTED = "workflow.action.rejected"
    WORKFLOW_ACTION_EXECUTED = "workflow.action.executed"
    WORKFLOW_ACTION_FAILED = "workflow.action.failed"
    WORKFLOW_APPROVAL_DENIED = "workflow.approval_denied"
    REPORT_GENERATED = "report.generated"
    REPORT_DOWNLOADED = "report.downloaded"
    USER_UPDATED = "user.updated"
    USER_PASSWORD_RESET = "user.password_reset"  # noqa: S105 - an action name


@dataclass(frozen=True, slots=True)
class RequestMeta:
    """Who/where a request came from, captured at the API edge."""

    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None


SYSTEM_REQUEST = RequestMeta()


def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Client hosts are not always IP literals (e.g. unix sockets, test clients)."""
    if not value:
        return None
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def record_audit_event(
    session: AsyncSession,
    *,
    action: AuditAction,
    outcome: AuditOutcome,
    meta: RequestMeta,
    actor: User | None = None,
    actor_type: ActorType | None = None,
    entity_type: str | None = None,
    entity_id: str | uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    resolved_actor_type = actor_type or (ActorType.USER if actor else ActorType.ANONYMOUS)
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        actor_type=resolved_actor_type,
        actor_role=actor.role.value if actor else None,
        action=action.value,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        outcome=outcome,
        request_id=meta.request_id,
        ip_address=_parse_ip(meta.ip_address),
        user_agent=meta.user_agent[:_USER_AGENT_MAX] if meta.user_agent else None,
        details=details or {},
    )
    session.add(entry)
    return entry
