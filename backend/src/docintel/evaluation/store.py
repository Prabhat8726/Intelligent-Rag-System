"""Evaluation reports in the database (Phase 10, ADR-067).

Reports stay files first (`evaluation/reports/<suite>.json|.md`, ADR-027). This module records a
report as one `evaluations` row, either right after a run (`docintel evaluate --record`) or
from report files (`docintel evaluation import`, a directory or a tar stream on stdin for a
container). A report is validated, copied verbatim and stored once: the key is the SHA-256 of
its canonical JSON, so importing the same files again changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.db.models import Evaluation, EvaluationSource, User
from docintel.evaluation.report import Report

MAX_REPORT_BYTES = 5 * 1024 * 1024  # the largest committed report is far below this


class InvalidReportError(ValueError):
    """A file that is not an evaluation report (or a damaged one)."""


class ReportTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str
    header: list[str]
    rows: list[list[str]]


class ReportPayload(BaseModel):
    """The JSON a suite writes (`Report.to_json`). Reports from before Phase 10 have no `quick`
    flag or tables; their quick flag is read from where those suites kept it."""

    model_config = ConfigDict(extra="forbid")

    suite: str = Field(pattern=r"^[a-z][a-z_]{0,39}$")
    title: str = Field(min_length=1, max_length=200)
    quick: bool = False
    created_at: datetime
    git_revision: str | None = Field(default=None, max_length=64)
    environment: dict[str, Any]
    dataset: dict[str, Any]
    config: dict[str, Any]
    metrics: dict[str, Any]
    notes: list[str] = []
    tables: list[ReportTable] = []

    @model_validator(mode="before")
    @classmethod
    def _legacy_quick(cls, data: Any) -> Any:
        if isinstance(data, dict) and "quick" not in data:
            config, dataset = data.get("config"), data.get("dataset")
            legacy = (isinstance(config, dict) and config.get("quick") is True) or (
                isinstance(dataset, dict) and dataset.get("quick") is True
            )
            return {**data, "quick": legacy}
        return data

    @model_validator(mode="after")
    def _aware_time(self) -> ReportPayload:
        if self.created_at.tzinfo is None:
            msg = "created_at needs a time zone"
            raise ValueError(msg)
        return self


@dataclass(frozen=True, slots=True)
class ReportFile:
    payload: ReportPayload
    raw: dict[str, Any]  # the JSON as written, hashed and kept as is
    markdown: str

    @property
    def sha256(self) -> str:
        canonical = json.dumps(self.raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_report(json_bytes: bytes, markdown_bytes: bytes, *, name: str) -> ReportFile:
    if len(json_bytes) > MAX_REPORT_BYTES or len(markdown_bytes) > MAX_REPORT_BYTES:
        msg = f"{name}: larger than {MAX_REPORT_BYTES} bytes"
        raise InvalidReportError(msg)
    try:
        raw = json.loads(json_bytes)
        payload = ReportPayload.model_validate(raw)
        markdown = markdown_bytes.decode("utf-8")
    except (ValueError, ValidationError, UnicodeDecodeError) as exc:
        msg = f"{name}: not an evaluation report ({exc.__class__.__name__})"
        raise InvalidReportError(msg) from exc
    if f"{payload.suite}.json" != name:
        msg = f"{name}: the report says it is suite {payload.suite!r}"
        raise InvalidReportError(msg)
    return ReportFile(payload, raw, markdown)


def _pair(files: dict[str, bytes]) -> list[ReportFile]:
    reports = []
    for name in sorted(files):
        if not name.endswith(".json"):
            continue
        markdown = files.get(name.removesuffix(".json") + ".md")
        if markdown is None:
            msg = f"{name}: its Markdown report is missing"
            raise InvalidReportError(msg)
        reports.append(parse_report(files[name], markdown, name=name))
    return reports


def read_report_directory(directory: Path) -> list[ReportFile]:
    """Every `<suite>.json` with its `<suite>.md` in a directory (not recursive)."""
    files = {
        path.name: path.read_bytes()
        for path in directory.iterdir()
        if path.is_file() and path.suffix in (".json", ".md")
    }
    return _pair(files)


def read_report_tar(stream: IO[bytes]) -> list[ReportFile]:
    """Reports from a tar stream (`tar -cf - -C evaluation/reports .`): regular files only,
    by base name, nothing written to disk."""
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            name = PurePosixPath(member.name).name
            if not member.isfile() or not name.endswith((".json", ".md")):
                continue
            if member.size > MAX_REPORT_BYTES:
                msg = f"{name}: larger than {MAX_REPORT_BYTES} bytes"
                raise InvalidReportError(msg)
            extracted = archive.extractfile(member)
            if extracted is not None:
                files[name] = extracted.read()
    return _pair(files)


async def record_report(
    session: AsyncSession,
    report: ReportFile,
    *,
    source: EvaluationSource,
    recorded_by: User | None = None,
    gates: dict[str, Any] | None = None,
) -> tuple[Evaluation, bool]:
    """Store a report unless it is already stored. Returns (row, created). Caller commits."""
    sha = report.sha256
    existing = await session.scalar(
        select(Evaluation).where(
            Evaluation.suite == report.payload.suite, Evaluation.report_sha256 == sha
        )
    )
    if existing is not None:
        return existing, False
    payload = report.payload
    row = Evaluation(
        suite=payload.suite,
        title=payload.title,
        quick=payload.quick,
        git_revision=payload.git_revision,
        run_at=payload.created_at,
        dataset=payload.dataset,
        config=payload.config,
        environment=payload.environment,
        metrics=payload.metrics,
        notes=payload.notes,
        tables=[table.model_dump() for table in payload.tables],
        report_markdown=report.markdown,
        report_sha256=sha,
        gates=gates,
        source=source,
        recorded_by_id=recorded_by.id if recorded_by else None,
    )
    session.add(row)
    await session.flush()
    return row, True


async def record_reports(
    session: AsyncSession,
    reports: Iterable[ReportFile],
    *,
    source: EvaluationSource,
    recorded_by: User | None = None,
    gates: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[Evaluation, bool]]:
    """Record several reports (gates by suite). Caller commits."""
    return [
        await record_report(
            session,
            report,
            source=source,
            recorded_by=recorded_by,
            gates=(gates or {}).get(report.payload.suite),
        )
        for report in reports
    ]


def report_file_from(report: Report) -> ReportFile:
    """The ReportFile of a report just produced (identical to reading back its files)."""
    raw = json.loads(json.dumps(report.to_json()))
    return ReportFile(ReportPayload.model_validate(raw), raw, report.to_markdown())
