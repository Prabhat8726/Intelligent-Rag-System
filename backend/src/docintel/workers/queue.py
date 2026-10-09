"""PostgreSQL job queue (ADR-002).

* enqueue: inserted in the caller's transaction (atomic with the domain change) and announced
  with NOTIFY, which PostgreSQL delivers only when that transaction commits.
* claim: `FOR UPDATE SKIP LOCKED` hands each job to exactly one worker; a lease bounds how long
  a crashed worker can hold it - expired leases are reclaimed by any worker.
* complete/fail/heartbeat only succeed for the current lease holder, so a worker whose lease
  was reclaimed cannot overwrite the new owner's state.
* failures are retried with exponential backoff until max_attempts, then marked FAILED.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.db.models import JobStatus, JobType, ProcessingJob

JOBS_CHANNEL = "docintel_jobs"
MAX_BACKOFF_SECONDS = 3600.0
_ERROR_LIMIT = 1000

_CLAIM_SQL = text(
    """
    UPDATE processing_jobs AS j
    SET status = 'PROCESSING',
        locked_by = :worker_id,
        locked_at = now(),
        lease_expires_at = now() + (:lease_seconds * interval '1 second'),
        attempts = j.attempts + 1,
        started_at = now(),
        updated_at = now(),
        last_error = CASE WHEN j.status = 'PROCESSING'
                          THEN 'lease expired; job reclaimed'
                          ELSE j.last_error END
    WHERE j.id = (
        SELECT id FROM processing_jobs
        WHERE job_type = ANY(:job_types)
          AND ((status = 'QUEUED' AND run_after <= now())
               OR (status = 'PROCESSING' AND lease_expires_at < now()))
        ORDER BY priority DESC, run_after, created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING j.id, j.job_type, j.document_id, j.document_version_id, j.knowledge_document_id,
              j.payload, j.attempts, j.max_attempts, j.requested_by_id
    """
)


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: uuid.UUID
    job_type: JobType
    document_id: uuid.UUID | None
    document_version_id: uuid.UUID | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    requested_by_id: uuid.UUID | None
    knowledge_document_id: uuid.UUID | None = None


def backoff_seconds(attempt: int, base_seconds: float) -> float:
    """Exponential backoff with up to 10% jitter: base, 2*base, 4*base, ... capped at 1 hour."""
    delay = min(MAX_BACKOFF_SECONDS, base_seconds * (2.0 ** max(0, attempt - 1)))
    return delay + random.uniform(0, delay * 0.1)  # noqa: S311  (jitter, not cryptography)


async def enqueue_job(
    session: AsyncSession,
    *,
    job_type: JobType,
    max_attempts: int,
    document_id: uuid.UUID | None = None,
    document_version_id: uuid.UUID | None = None,
    requested_by_id: uuid.UUID | None = None,
    knowledge_document_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
) -> ProcessingJob:
    """Add a job inside the caller's transaction. Raises IntegrityError if an active job for
    the same (job_type, document_version) or knowledge document already exists."""
    job = ProcessingJob(
        id=uuid.uuid4(),
        job_type=job_type,
        status=JobStatus.QUEUED,
        document_id=document_id,
        document_version_id=document_version_id,
        knowledge_document_id=knowledge_document_id,
        requested_by_id=requested_by_id,
        payload=payload or {},
        priority=priority,
        max_attempts=max_attempts,
    )
    session.add(job)
    await session.flush()
    await session.execute(
        text("SELECT pg_notify(:channel, :job_type)"),
        {"channel": JOBS_CHANNEL, "job_type": job_type.value},
    )
    return job


async def cancel_queued_jobs(
    session: AsyncSession,
    *,
    document_id: uuid.UUID | None = None,
    knowledge_document_id: uuid.UUID | None = None,
) -> int:
    if (document_id is None) == (knowledge_document_id is None):
        msg = "pass exactly one of document_id and knowledge_document_id"
        raise ValueError(msg)
    target = (
        ProcessingJob.document_id == document_id
        if document_id is not None
        else ProcessingJob.knowledge_document_id == knowledge_document_id
    )
    result = await session.execute(
        update(ProcessingJob)
        .where(target, ProcessingJob.status == JobStatus.QUEUED)
        .values(status=JobStatus.CANCELLED, finished_at=func.now(), last_error="cancelled")
        .returning(ProcessingJob.id)
    )
    return len(result.all())


class JobQueue:
    """Worker-side queue operations. Each method runs in the session/transaction it is given."""

    def __init__(self, *, worker_id: str, lease_seconds: int, retry_base_seconds: float) -> None:
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds

    async def claim(self, session: AsyncSession, job_types: list[JobType]) -> ClaimedJob | None:
        row = (
            (
                await session.execute(
                    _CLAIM_SQL,
                    {
                        "worker_id": self.worker_id,
                        "lease_seconds": self.lease_seconds,
                        "job_types": [job_type.value for job_type in job_types],
                    },
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return ClaimedJob(
            id=row["id"],
            job_type=JobType(row["job_type"]),
            document_id=row["document_id"],
            document_version_id=row["document_version_id"],
            payload=dict(row["payload"] or {}),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            requested_by_id=row["requested_by_id"],
            knowledge_document_id=row["knowledge_document_id"],
        )

    async def cancel(self, session: AsyncSession, job_id: uuid.UUID, *, reason: str) -> bool:
        result = await session.execute(
            self._owned(job_id)
            .values(
                status=JobStatus.CANCELLED,
                finished_at=func.now(),
                locked_by=None,
                lease_expires_at=None,
                last_error=reason[:_ERROR_LIMIT],
                updated_at=func.now(),
            )
            .returning(ProcessingJob.id)
        )
        return result.first() is not None

    def _owned(self, job_id: uuid.UUID) -> Any:
        return update(ProcessingJob).where(
            ProcessingJob.id == job_id,
            ProcessingJob.locked_by == self.worker_id,
            ProcessingJob.status == JobStatus.PROCESSING,
        )

    async def heartbeat(self, session: AsyncSession, job_id: uuid.UUID) -> bool:
        result = await session.execute(
            self._owned(job_id)
            .values(
                lease_expires_at=func.now() + timedelta(seconds=self.lease_seconds),
                updated_at=func.now(),
            )
            .returning(ProcessingJob.id)
        )
        return result.first() is not None

    async def record_progress(
        self, session: AsyncSession, job_id: uuid.UUID, *, stage: str, stage_timings: dict[str, Any]
    ) -> bool:
        result = await session.execute(
            self._owned(job_id)
            .values(stage=stage, stage_timings=stage_timings, updated_at=func.now())
            .returning(ProcessingJob.id)
        )
        return result.first() is not None

    async def complete(
        self, session: AsyncSession, job_id: uuid.UUID, *, stage_timings: dict[str, Any]
    ) -> datetime | None:
        """Mark done; returns finished_at, or None if this worker no longer holds the lease."""
        result = await session.execute(
            self._owned(job_id)
            .values(
                status=JobStatus.COMPLETED,
                stage="done",
                stage_timings=stage_timings,
                finished_at=func.now(),
                locked_by=None,
                lease_expires_at=None,
                last_error=None,
                updated_at=func.now(),
            )
            .returning(ProcessingJob.finished_at)
        )
        row = result.first()
        return row[0] if row else None

    async def fail(
        self,
        session: AsyncSession,
        job: ClaimedJob,
        *,
        error: str,
        retryable: bool,
        stage_timings: dict[str, Any],
    ) -> JobStatus | None:
        """Requeue with backoff (retryable, attempts left) or mark FAILED.

        Returns the new status, or None if this worker no longer holds the lease.
        """
        retry = retryable and job.attempts < job.max_attempts
        values: dict[str, Any] = {
            "locked_by": None,
            "lease_expires_at": None,
            "last_error": error[:_ERROR_LIMIT],
            "stage_timings": stage_timings,
            "updated_at": func.now(),
        }
        if retry:
            delay = backoff_seconds(job.attempts, self.retry_base_seconds)
            values["status"] = JobStatus.QUEUED
            values["run_after"] = func.now() + timedelta(seconds=delay)
        else:
            values["status"] = JobStatus.FAILED
            values["finished_at"] = func.now()
        result = await session.execute(
            self._owned(job.id).values(**values).returning(ProcessingJob.status)
        )
        row = result.first()
        return JobStatus(row[0]) if row else None
