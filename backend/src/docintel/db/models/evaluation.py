"""Recorded evaluation runs (Phase 10).

`docintel evaluate` writes each suite's report to `evaluation/reports/<suite>.json|.md` (ADR-027,
kept in git). A run can also be recorded here, in the database of the deployment it ran
against (`--record`), or report files can be imported (`docintel evaluation import`), so the
web app shows the measured results and their history. A row is a copy of one report: its
metrics are never computed or edited here, and the same report is stored only once.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docintel.db.base import Base, UUIDPrimaryKeyMixin
from docintel.db.models.identity import User
from docintel.db.models.types import str_enum


class EvaluationSource(StrEnum):
    RUN = "RUN"  # recorded by the run itself (`docintel evaluate --record`)
    IMPORT = "IMPORT"  # loaded from report files (`docintel evaluation import`)


class Evaluation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "evaluations"
    __table_args__ = (
        Index("ix_evaluations_suite_run_at", "suite", "run_at"),
        UniqueConstraint("suite", "report_sha256", name="uq_evaluations_report"),
        CheckConstraint("suite ~ '^[a-z][a-z_]{0,39}$'", name="suite_name"),
        CheckConstraint("report_sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
    )

    suite: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    quick: Mapped[bool]  # small smoke-test datasets: not comparable with full runs
    git_revision: Mapped[str | None] = mapped_column(String(64))
    run_at: Mapped[datetime]  # when the suite produced the report
    dataset: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    environment: Mapped[dict[str, Any]] = mapped_column(JSONB)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    notes: Mapped[list[str]] = mapped_column(JSONB)
    tables: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    report_markdown: Mapped[str] = mapped_column(Text)
    # SHA-256 of the report's canonical JSON: the same report is recorded once.
    report_sha256: Mapped[str] = mapped_column(String(64))
    # Regression gates checked when it was recorded (None: no gates for this suite or mode).
    gates: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source: Mapped[EvaluationSource] = mapped_column(str_enum(EvaluationSource, "source"))
    recorded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    recorded_by: Mapped[User | None] = relationship(lazy="joined")
