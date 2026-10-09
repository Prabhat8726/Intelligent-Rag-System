"""Worker + queue behaviour against real PostgreSQL with committed transactions.

These tests do not use the rollback-per-test session: claims, leases and NOTIFY only behave
realistically across separately committed transactions.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi import UploadFile
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.datastructures import Headers

from docintel.audit.service import SYSTEM_REQUEST
from docintel.core.config import Settings
from docintel.db.models import (
    AuditLog,
    Department,
    Document,
    DocumentPage,
    DocumentStatus,
    DocumentType,
    DocumentVersion,
    JobStatus,
    JobType,
    ProcessingJob,
    ReviewTask,
    Role,
    Sensitivity,
    User,
)
from docintel.documents.service import DocumentService
from docintel.storage import LocalStorage, StorageUnavailableError
from docintel.workers.queue import JOBS_CHANNEL, JobQueue, backoff_seconds
from docintel.workers.runner import Worker, classify_failure
from tests.conftest import make_settings
from tests.factories.files import (
    image_bytes,
    image_only_pdf_bytes,
    invoice_pdf_bytes,
    mixed_pdf_bytes,
    pdf_bytes,
)

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------------------ fixtures
@pytest.fixture
async def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(delete(ProcessingJob))  # deterministic claim order per test
    return sessions


@pytest.fixture
def worker_settings(database_url: str, storage_root: Path) -> Settings:
    return make_settings(
        database_url=database_url,
        storage_local_root=storage_root,
        job_max_attempts=3,
        job_retry_base_seconds=0,
        job_lease_seconds=60,
        worker_poll_interval_seconds=30,
    )


@pytest.fixture
def storage(storage_root: Path) -> LocalStorage:
    return LocalStorage(storage_root)


@pytest.fixture
async def uploader(maker: async_sessionmaker[AsyncSession]) -> User:
    async with maker() as session, session.begin():
        department = Department(id=uuid.uuid4(), name=f"Ops-{uuid.uuid4().hex[:8]}")
        user = User(
            id=uuid.uuid4(),
            email=f"worker-test-{uuid.uuid4().hex[:8]}@example.test",
            full_name="Worker Test",
            password_hash="not-used",
            role=Role.ANALYST,
            department_id=department.id,
        )
        session.add_all([department, user])
    return user


async def ingest(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    settings: Settings,
    user: User,
    content: bytes,
    filename: str = "doc.pdf",
    content_type: str = "application/pdf",
) -> uuid.UUID:
    upload = UploadFile(
        file=io.BytesIO(content), filename=filename, headers=Headers({"content-type": content_type})
    )
    async with maker() as session:
        actor = await session.get(User, user.id)
        assert actor is not None
        document = await DocumentService(session, storage, settings).upload(
            actor=actor,
            upload=upload,
            sensitivity=Sensitivity.INTERNAL,
            department_id=None,
            meta=SYSTEM_REQUEST,
        )
        return document.id


async def load(
    maker: async_sessionmaker[AsyncSession], document_id: uuid.UUID
) -> tuple[Document, DocumentVersion, list[ProcessingJob]]:
    async with maker() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        version = await session.get(DocumentVersion, document.current_version_id)
        assert version is not None
        jobs = list(
            await session.scalars(
                select(ProcessingJob)
                .where(ProcessingJob.document_id == document_id)
                .order_by(ProcessingJob.created_at)
            )
        )
        return document, version, jobs


def make_worker(
    settings: Settings,
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_id: str = "worker-A",
) -> Worker:
    return Worker(settings=settings, sessionmaker=maker, storage=storage, worker_id=worker_id)


PROCESSED = (DocumentStatus.COMPLETED, DocumentStatus.REVIEW_REQUIRED)


def assert_processed(document: Document) -> None:
    """Processing worked: any review is for business rules (a lone invoice has no order)."""
    assert document.status in PROCESSED
    assert set(document.review_reasons) <= {"RULE_VIOLATION"}


# ------------------------------------------------------------------------------ happy paths
async def test_native_pdf_is_inspected_and_completed(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
) -> None:
    document_id = await ingest(
        maker, storage, worker_settings, uploader, invoice_pdf_bytes(pages=2)
    )

    assert await make_worker(worker_settings, maker, storage).run_until_idle() == 1

    document, version, jobs = await load(maker, document_id)
    # Read cleanly; held only because the invoice names no purchase order (INV_MISSING_PO).
    assert document.status == DocumentStatus.REVIEW_REQUIRED
    assert document.review_reasons == ["RULE_VIOLATION"]
    async with maker() as session:
        held = await session.scalar(select(ReviewTask).where(ReviewTask.document_id == document_id))
        assert held is not None
        assert [reason["code"] for reason in held.reasons] == ["INV_MISSING_PO"]
    assert document.document_type == DocumentType.INVOICE
    assert document.last_processed_at is not None
    assert document.processing_error is None
    assert version.inspection is not None
    assert version.inspection["kind"] == "native_pdf"
    assert version.inspection["page_count"] == 2
    assert version.inspection["pages_needing_ocr"] == []
    assert [page["method"] for page in version.inspection["pages"]] == ["NATIVE", "NATIVE"]
    assert version.inspection["pages"][0]["unit"] == "pt"
    (job,) = jobs
    assert job.status == JobStatus.COMPLETED
    assert job.attempts == 1
    assert set(job.stage_timings) == {
        "integrity",
        "inspect",
        "extract",
        "previews",
        "classify",
        "fields",
    }
    assert job.locked_by is None
    assert job.finished_at is not None
    async with maker() as session:
        actions = list(
            await session.scalars(
                select(AuditLog.action).where(AuditLog.entity_id == str(document_id))
            )
        )
    assert actions == ["document.uploaded", "document.processing.completed"]


@pytest.mark.parametrize(
    ("content", "filename", "content_type", "kind", "ocr_pages"),
    [
        (image_only_pdf_bytes(2), "scan.pdf", "application/pdf", "scanned_pdf", [1, 2]),
        (
            mixed_pdf_bytes(["Purchase order PO-77 for 40 units", None, "Terms: net 30 days."]),
            "mixed.pdf",
            "application/pdf",
            "mixed_pdf",
            [2],
        ),
        (image_bytes("TIFF", frames=2), "fax.tiff", "image/tiff", "image", [1, 2]),
        (image_bytes("JPEG"), "receipt.jpg", "image/jpeg", "image", [1]),
    ],
)
async def test_pages_needing_ocr_are_identified_per_page(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
    content: bytes,
    filename: str,
    content_type: str,
    kind: str,
    ocr_pages: list[int],
) -> None:
    document_id = await ingest(
        maker, storage, worker_settings, uploader, content, filename, content_type
    )
    await make_worker(worker_settings, maker, storage).run_until_idle()
    document, version, _ = await load(maker, document_id)
    assert version.inspection is not None
    assert version.inspection["kind"] == kind
    assert version.inspection["pages_needing_ocr"] == ocr_pages
    async with maker() as session:
        pages = list(
            await session.scalars(
                select(DocumentPage)
                .where(DocumentPage.document_version_id == version.id)
                .order_by(DocumentPage.page_number)
            )
        )
    assert [page.page_number for page in pages if page.extraction_method == "OCR"] == ocr_pages
    if kind != "mixed_pdf":  # blank test images: nothing to read
        assert document.status == DocumentStatus.REVIEW_REQUIRED
        assert document.review_reasons == ["NO_TEXT_FOUND"]


# ------------------------------------------------------------------------------ failures
async def test_tampered_blob_fails_permanently_without_retry(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
) -> None:
    document_id = await ingest(maker, storage, worker_settings, uploader, pdf_bytes())
    _, version, _ = await load(maker, document_id)
    (storage.root / version.storage_key).write_bytes(pdf_bytes(text="tampered content"))

    await make_worker(worker_settings, maker, storage).run_until_idle()

    document, _, (job,) = await load(maker, document_id)
    assert document.status == DocumentStatus.FAILED
    assert document.processing_error == "The stored file failed its integrity check."
    assert job.status == JobStatus.FAILED
    assert job.attempts == 1
    async with maker() as session:
        failed = await session.scalar(
            select(AuditLog).where(
                AuditLog.entity_id == str(document_id),
                AuditLog.action == "document.processing.failed",
            )
        )
    assert failed is not None
    assert failed.actor_type == "WORKER"


async def test_missing_blob_fails_permanently(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
) -> None:
    document_id = await ingest(maker, storage, worker_settings, uploader, pdf_bytes())
    _, version, _ = await load(maker, document_id)
    (storage.root / version.storage_key).unlink()

    await make_worker(worker_settings, maker, storage).run_until_idle()
    document, _, (job,) = await load(maker, document_id)
    assert document.status == DocumentStatus.FAILED
    assert document.processing_error == "The stored file is missing."
    assert job.status == JobStatus.FAILED


class FlakyStorage(LocalStorage):
    """Fails `download_to` with a transient error the first `failures` times."""

    def __init__(self, root: Path, failures: int) -> None:
        super().__init__(root)
        self.failures = failures

    async def download_to(self, key: str, destination: Path) -> None:
        if self.failures > 0:
            self.failures -= 1
            msg = "simulated S3 throttling"
            raise StorageUnavailableError(msg)
        await super().download_to(key, destination)


async def test_transient_failure_is_retried_then_succeeds(
    maker: async_sessionmaker[AsyncSession],
    storage_root: Path,
    worker_settings: Settings,
    uploader: User,
) -> None:
    flaky = FlakyStorage(storage_root, failures=1)
    document_id = await ingest(maker, flaky, worker_settings, uploader, invoice_pdf_bytes())
    worker = make_worker(worker_settings, maker, flaky)

    assert await worker.run_once()
    document, _, (job,) = await load(maker, document_id)
    assert job.status == JobStatus.QUEUED
    assert job.attempts == 1
    assert job.last_error == "Temporary storage failure; processing will be retried."
    assert document.status == DocumentStatus.PENDING
    assert document.processing_error is None

    assert await worker.run_once()
    document, _, (job,) = await load(maker, document_id)
    assert job.status == JobStatus.COMPLETED
    assert job.attempts == 2
    assert_processed(document)


async def test_retries_are_exhausted_then_failed(
    maker: async_sessionmaker[AsyncSession],
    storage_root: Path,
    worker_settings: Settings,
    uploader: User,
) -> None:
    always_down = FlakyStorage(storage_root, failures=10)
    document_id = await ingest(maker, always_down, worker_settings, uploader, pdf_bytes())

    processed = await make_worker(worker_settings, maker, always_down).run_until_idle()

    assert processed == 3  # job_max_attempts
    document, _, (job,) = await load(maker, document_id)
    assert job.status == JobStatus.FAILED
    assert job.attempts == 3
    assert document.status == DocumentStatus.FAILED


async def test_backoff_delays_the_retry(
    maker: async_sessionmaker[AsyncSession],
    storage_root: Path,
    database_url: str,
    uploader: User,
) -> None:
    settings = make_settings(
        database_url=database_url, storage_local_root=storage_root, job_retry_base_seconds=600
    )
    flaky = FlakyStorage(storage_root, failures=1)
    document_id = await ingest(maker, flaky, settings, uploader, pdf_bytes())
    worker = make_worker(settings, maker, flaky)
    assert await worker.run_once()
    assert not await worker.run_once()  # not runnable until run_after
    _, _, (job,) = await load(maker, document_id)
    assert job.run_after > datetime.now(UTC) + timedelta(minutes=9)


def test_backoff_grows_exponentially_and_is_capped() -> None:
    assert 30 <= backoff_seconds(1, 30) <= 33
    assert 60 <= backoff_seconds(2, 30) <= 66
    assert 120 <= backoff_seconds(3, 30) <= 132
    assert backoff_seconds(30, 30) <= 3600 * 1.1


@pytest.mark.parametrize(
    ("exc", "retryable", "message"),
    [
        (StorageUnavailableError("x"), True, "Temporary storage failure"),
        (TimeoutError(), True, "Temporary infrastructure failure"),
        (KeyError("bug"), True, "Unexpected processing error"),
    ],
)
def test_failure_classification(exc: Exception, retryable: bool, message: str) -> None:
    is_retryable, user_message = classify_failure(exc, uuid.uuid4())
    assert is_retryable is retryable
    assert user_message.startswith(message)
    assert "bug" not in user_message  # internal details never reach users


# ------------------------------------------------------------------------------ queue semantics
async def test_deleted_document_job_is_cancelled_without_processing(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
) -> None:
    document_id = await ingest(maker, storage, worker_settings, uploader, pdf_bytes())
    async with maker() as session, session.begin():
        await session.execute(
            update(Document).where(Document.id == document_id).values(deleted_at=func.now())
        )
    await make_worker(worker_settings, maker, storage).run_until_idle()
    document, version, (job,) = await load(maker, document_id)
    assert job.status == JobStatus.CANCELLED
    assert version.inspection is None
    assert document.status == DocumentStatus.PENDING


async def test_skip_locked_gives_concurrent_workers_different_jobs(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
) -> None:
    first = await ingest(maker, storage, worker_settings, uploader, pdf_bytes(text="one"))
    second = await ingest(maker, storage, worker_settings, uploader, pdf_bytes(text="two"))
    queue_a = JobQueue(worker_id="A", lease_seconds=60, retry_base_seconds=0)
    queue_b = JobQueue(worker_id="B", lease_seconds=60, retry_base_seconds=0)

    async with maker() as session_a, session_a.begin():
        job_a = await queue_a.claim(session_a, [JobType.DOCUMENT_PROCESSING])
        # session_a still holds its row lock; B must skip it, not wait.
        async with maker() as session_b, session_b.begin():
            job_b = await asyncio.wait_for(
                queue_b.claim(session_b, [JobType.DOCUMENT_PROCESSING]), timeout=5
            )
            async with maker() as session_c, session_c.begin():
                job_c = await queue_b.claim(session_c, [JobType.DOCUMENT_PROCESSING])

    assert job_a is not None
    assert job_b is not None
    assert {job_a.document_id, job_b.document_id} == {first, second}
    assert job_c is None


async def test_expired_lease_is_reclaimed_and_stale_worker_cannot_complete(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
) -> None:
    document_id = await ingest(maker, storage, worker_settings, uploader, invoice_pdf_bytes())
    crashed = JobQueue(worker_id="crashed", lease_seconds=60, retry_base_seconds=0)
    async with maker() as session, session.begin():
        claimed = await crashed.claim(session, [JobType.DOCUMENT_PROCESSING])
    assert claimed is not None

    # Not reclaimable while the lease is valid.
    assert not await make_worker(worker_settings, maker, storage, "rescuer").run_once()

    async with maker() as session, session.begin():
        await session.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == claimed.id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )
    assert await make_worker(worker_settings, maker, storage, "rescuer").run_once()

    document, _, (job,) = await load(maker, document_id)
    assert job.status == JobStatus.COMPLETED
    assert job.attempts == 2
    assert_processed(document)
    async with maker() as session, session.begin():
        assert await crashed.complete(session, claimed.id, stage_timings={}) is None
        assert not await crashed.heartbeat(session, claimed.id)


async def test_job_reclaimed_too_often_is_failed_without_running(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
) -> None:
    document_id = await ingest(maker, storage, worker_settings, uploader, pdf_bytes())
    async with maker() as session, session.begin():
        await session.execute(
            update(ProcessingJob)
            .where(ProcessingJob.document_id == document_id)
            .values(attempts=3, status=JobStatus.PROCESSING, lease_expires_at=func.now())
        )
    await make_worker(worker_settings, maker, storage).run_until_idle()
    document, version, (job,) = await load(maker, document_id)
    assert job.status == JobStatus.FAILED
    assert job.attempts == 4
    assert version.inspection is None
    assert document.status == DocumentStatus.FAILED


async def test_notify_is_delivered_only_after_commit(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
    database_url: str,
) -> None:
    conninfo = (
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as listener:
        await listener.execute(f"LISTEN {JOBS_CHANNEL}")
        await ingest(maker, storage, worker_settings, uploader, pdf_bytes())
        received = [n async for n in listener.notifies(timeout=3, stop_after=1)]
    assert len(received) == 1
    assert received[0].payload == "DOCUMENT_PROCESSING"


async def test_running_worker_is_woken_by_notify_not_polling(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "worker.heartbeat"
    settings = worker_settings.model_copy(update={"worker_heartbeat_file": heartbeat})
    worker = make_worker(settings, maker, storage)  # poll interval is 30 s
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        await asyncio.sleep(0.5)  # let the loop go idle and the listener subscribe
        document_id = await ingest(maker, storage, settings, uploader, invoice_pdf_bytes())
        async with asyncio.timeout(10):
            while (await load(maker, document_id))[0].status not in PROCESSED:  # noqa: ASYNC110 (polling DB state)
                await asyncio.sleep(0.1)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    assert heartbeat.exists()
