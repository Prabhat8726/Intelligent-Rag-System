"""End to end: synthetic generator -> REST API (real auth) -> queue -> worker -> results."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from docintel.api.app import create_app
from docintel.audit.service import SYSTEM_REQUEST
from docintel.auth.service import UserService
from docintel.db.models import (
    Comparison,
    ComparisonType,
    ProcessingJob,
    ReviewTask,
    ReviewTaskStatus,
    ReviewTaskType,
    Role,
    RuleOutcome,
    RuleResultRecord,
)
from docintel.storage import LocalStorage
from docintel.synthetic.generator import generate_dataset
from docintel.synthetic.scenarios import Scenario
from docintel.tools.ingest import REPORT_NAME, ingest_directory, summarize
from docintel.workers.runner import Worker
from tests.conftest import TEST_PASSWORD, make_settings

pytestmark = pytest.mark.integration


async def test_generated_documents_flow_through_api_queue_and_worker(
    engine: AsyncEngine, database_url: str, tmp_path: Path
) -> None:
    storage_root = tmp_path / "storage"
    settings = make_settings(
        database_url=database_url,
        storage_local_root=storage_root,
        worker_poll_interval_seconds=0.5,
        job_retry_base_seconds=0,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    email = f"e2e-{uuid.uuid4().hex[:8]}@example.test"
    async with sessions() as session:
        await session.execute(delete(ProcessingJob))
        service = UserService(session)
        department = await service.get_or_create_department(
            f"E2E-{uuid.uuid4().hex[:6]}", meta=SYSTEM_REQUEST
        )
        await service.create_user(
            email=email,
            full_name="E2E Analyst",
            password=TEST_PASSWORD,
            role=Role.ANALYST,
            department=department,
            meta=SYSTEM_REQUEST,
        )
        await session.commit()

    dataset = tmp_path / "dataset"
    manifest = generate_dataset(
        dataset,
        seed=3,
        scenarios=[Scenario.UNIT_PRICE_MISMATCH, Scenario.SCANNED_DOCUMENTS],
    )

    app = create_app(settings)
    worker = Worker(settings=settings, sessionmaker=sessions, storage=LocalStorage(storage_root))
    stop = asyncio.Event()
    async with app.router.lifespan_context(app):
        worker_task = asyncio.create_task(worker.run(stop))
        try:
            items = await ingest_directory(
                dataset,
                api_url="http://testserver",
                email=email,
                password=TEST_PASSWORD,
                timeout_seconds=30,
                poll_interval_seconds=0.2,
                transport=httpx.ASGITransport(app=app),
            )
        finally:
            stop.set()
            await asyncio.wait_for(worker_task, timeout=10)

    # Every document is processed. The price-mismatch bundle's invoice is held for its
    # discrepancy, found by matching it with its purchase order and delivery note; scans may
    # be held by extraction confidence or by differences their OCR reading cannot settle.
    by_doc = {item.doc_id: item for item in items}
    assert sum(summarize(items).values()) == manifest["document_count"]
    assert set(summarize(items)) <= {"COMPLETED", "REVIEW_REQUIRED"}
    for item in items:
        if item.status == "REVIEW_REQUIRED" and item.variant == "scanned":
            assert set(item.review_reasons or []) <= {
                "EXTRACTION_UNCERTAIN",
                "MISSING_REQUIRED_FIELDS",
                "EXTRACTION_INCONSISTENT",
                "LOW_OCR_CONFIDENCE",
                "RULE_VIOLATION",
            }, (item.doc_id, item.review_reasons)
    native = [item for item in items if item.variant == "native" and item.doc_id != "B0001-INV"]
    assert {item.status for item in native} == {"COMPLETED"}
    held = by_doc["B0001-INV"]
    assert (held.status, held.review_reasons) == ("REVIEW_REQUIRED", ["RULE_VIOLATION"])
    async with sessions() as session:
        document_id = uuid.UUID(str(held.document_id))
        failing = set(
            await session.scalars(
                select(RuleResultRecord.rule_code).where(
                    RuleResultRecord.document_id == document_id,
                    RuleResultRecord.outcome != RuleOutcome.PASS,
                    RuleResultRecord.outcome != RuleOutcome.NOT_APPLICABLE,
                )
            )
        )
        assert failing == {"INV_PO_UNIT_PRICE"}
        comparison = await session.scalar(
            select(Comparison).where(Comparison.subject_document_id == document_id)
        )
        assert comparison is not None
        assert comparison.comparison_type == ComparisonType.INVOICE_PO_DELIVERY
        assert comparison.summary["MISMATCH"] == 1
        task = await session.scalar(select(ReviewTask).where(ReviewTask.document_id == document_id))
        assert task is not None
        assert (task.status, task.task_type) == (
            ReviewTaskStatus.OPEN,
            ReviewTaskType.DISCREPANCY_REVIEW,
        )
    assert by_doc["B0001-INV"].inspection_kind == "native_pdf"
    assert by_doc["B0001-INV"].pages_needing_ocr == []
    assert by_doc["B0002-INV"].inspection_kind == "scanned_pdf"
    assert by_doc["B0002-DN"].inspection_kind == "image"
    assert all(item.processing_ms is not None for item in items)

    report = json.loads((dataset / REPORT_NAME).read_text())
    assert {row["doc_id"] for row in report["items"]} == {
        d["doc_id"] for d in manifest["documents"]
    }
    assert all(row["document_id"] for row in report["items"])
