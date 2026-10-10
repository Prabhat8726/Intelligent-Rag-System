"""Recorded evaluation reports (Phase 10, ADR-067): import, idempotency, the CLI."""

from __future__ import annotations

import argparse
import io
import json
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from docintel import cli
from docintel.db.models import Evaluation, EvaluationSource
from docintel.evaluation.report import Report
from docintel.evaluation.store import (
    InvalidReportError,
    read_report_directory,
    read_report_tar,
    record_report,
    record_reports,
    report_file_from,
)
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

COMMITTED_REPORTS = Path(__file__).resolve().parents[3] / "evaluation" / "reports"


def sample_report(*, suite: str = "ocr", quick: bool = True, value: float = 0.25) -> Report:
    return Report(
        suite=suite,
        title="OCR evaluation (test)",
        dataset={"name": "unit", "seed": 1},
        config={"engine": "test"},
        metrics={"clean": {"cer": value}},
        environment={"python": "3.13"},
        quick=quick,
        notes=["A note."],
        tables=[("Overall", ["Measure", "Value"], [["CER", f"{value:.2f}"]])],
    )


def tar_of(files: dict[str, bytes]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directory = tarfile.TarInfo("./reports")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        for name, content in files.items():
            info = tarfile.TarInfo(f"./reports/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    buffer.seek(0)
    return buffer


async def test_the_committed_reports_are_recorded_once(db_session: AsyncSession) -> None:
    reports = read_report_directory(COMMITTED_REPORTS)
    suites = {report.payload.suite for report in reports}
    assert {"ocr", "classification", "extraction", "agent", "workflow"} <= suites

    first = await record_reports(db_session, reports, source=EvaluationSource.IMPORT)
    assert all(created for _, created in first)
    again = await record_reports(db_session, reports, source=EvaluationSource.IMPORT)
    assert not any(created for _, created in again)
    assert [row.id for row, _ in first] == [row.id for row, _ in again]

    agent = next(row for row, _ in first if row.suite == "agent")
    assert agent.quick is False
    assert agent.git_revision
    assert agent.run_at.tzinfo is not None
    assert agent.metrics["development"]  # copied verbatim
    assert agent.report_markdown.startswith("# Agent investigation evaluation")
    assert agent.source is EvaluationSource.IMPORT


async def test_a_run_records_exactly_the_report_it_writes(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    report = sample_report()
    report.write(tmp_path)
    in_memory = report_file_from(report)
    (from_files,) = read_report_directory(tmp_path)
    assert in_memory.sha256 == from_files.sha256  # recording now or importing later: one row
    assert from_files.payload.quick is True
    assert from_files.payload.tables[0].rows == [["CER", "0.25"]]
    assert "--quick" in from_files.markdown

    row, created = await record_report(db_session, in_memory, source=EvaluationSource.RUN)
    assert created
    _, again = await record_report(db_session, from_files, source=EvaluationSource.IMPORT)
    assert not again
    assert row.tables == [
        {"heading": "Overall", "header": ["Measure", "Value"], "rows": [["CER", "0.25"]]}
    ]
    # A different run of the same suite is a new row.
    _, other = await record_report(
        db_session, report_file_from(sample_report(value=0.5)), source=EvaluationSource.RUN
    )
    assert other


def test_reports_arrive_as_a_tar_stream_without_touching_the_disk(tmp_path: Path) -> None:
    report = sample_report(suite="tables")
    report.write(tmp_path)
    files = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    files["README.txt"] = b"ignored"
    (parsed,) = read_report_tar(tar_of(files))
    assert parsed.payload.suite == "tables"
    assert parsed.sha256 == report_file_from(report).sha256


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"ocr.json": b"{not json", "ocr.md": b"# x"}, "not an evaluation report"),
        (
            {"ocr.json": json.dumps({"suite": "ocr"}).encode(), "ocr.md": b"# x"},
            "not an evaluation report",
        ),
        ({"ocr.json": b"{}"}, "Markdown report is missing"),
    ],
)
def test_damaged_or_foreign_files_are_refused(files: dict[str, bytes], message: str) -> None:
    with pytest.raises(InvalidReportError, match=message):
        read_report_tar(tar_of(files))


def test_a_report_must_be_named_after_its_suite(tmp_path: Path) -> None:
    report = sample_report(suite="tables")
    report.write(tmp_path)
    (tmp_path / "tables.json").rename(tmp_path / "ocr.json")
    (tmp_path / "tables.md").rename(tmp_path / "ocr.md")
    with pytest.raises(InvalidReportError, match="says it is suite 'tables'"):
        read_report_directory(tmp_path)


async def test_cli_import_is_idempotent(
    database_url: str, engine: AsyncEngine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(database_url=database_url)
    report = sample_report(suite="versions", value=0.123456)
    report.write(tmp_path)
    args = argparse.Namespace(path=str(tmp_path), recorded_by=None)
    try:
        assert await cli._evaluation_import(settings, args) == cli.EXIT_OK
        assert "[ OK ] recorded versions" in capsys.readouterr().out
        assert await cli._evaluation_import(settings, args) == cli.EXIT_OK
        assert "already recorded" in capsys.readouterr().out
        async with AsyncSession(engine) as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Evaluation)
                .where(Evaluation.report_sha256 == report_file_from(report).sha256)
            )
        assert count == 1

        unknown = argparse.Namespace(path=str(tmp_path), recorded_by="nobody@docintel.local")
        assert await cli._evaluation_import(settings, unknown) == cli.EXIT_USAGE
        empty = argparse.Namespace(path=str(tmp_path / "missing"), recorded_by=None)
        assert await cli._evaluation_import(settings, empty) == cli.EXIT_FAILURE
    finally:
        async with AsyncSession(engine) as session:
            await session.execute(
                delete(Evaluation).where(
                    Evaluation.report_sha256 == report_file_from(report).sha256
                )
            )
            await session.commit()
