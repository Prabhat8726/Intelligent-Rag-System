"""Request rate limit counters (Phase 11, ADR-074).

One row per key (scope and caller) and fixed window, shared by every API replica. The table is
UNLOGGED: counters are short-lived and losing them in a crash only resets the current window.
Expired rows are deleted as new windows are written.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from docintel.db.base import Base


class RateLimitCounter(Base):
    __tablename__ = "rate_limit_counters"
    __table_args__ = (
        Index("ix_rate_limit_counters_expires_at", "expires_at"),
        CheckConstraint("hits >= 1", name="hits_positive"),
        {"prefixes": ["UNLOGGED"]},
    )

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(primary_key=True)
    expires_at: Mapped[datetime]
    hits: Mapped[int]
