"""Append-only audit log. UPDATE/DELETE/TRUNCATE are rejected by a database trigger."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address
from typing import Any

from sqlalchemy import BigInteger, Enum, ForeignKey, Identity, Index, String, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from docintel.db.base import Base


class ActorType(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"
    AGENT = "AGENT"
    WORKER = "WORKER"
    ANONYMOUS = "ANONYMOUS"


class AuditOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DENIED = "DENIED"


def _str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=20,
        values_callable=lambda enum: [member.value for member in enum],
        validate_strings=True,
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_occurred_at", text("occurred_at DESC")),
        Index("ix_audit_logs_actor_id_occurred_at", "actor_id", "occurred_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_action", "action"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    # RESTRICT: users with audit history are deactivated, never deleted.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    actor_type: Mapped[ActorType] = mapped_column(_str_enum(ActorType, "actor_type"))
    actor_role: Mapped[str | None] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[AuditOutcome] = mapped_column(_str_enum(AuditOutcome, "outcome"))
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[IPv4Address | IPv6Address | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
