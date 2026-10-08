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
    AUTHZ_DENIED = "authz.denied"
    USER_CREATED = "user.created"
    DEPARTMENT_CREATED = "department.created"


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
