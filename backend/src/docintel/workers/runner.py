"""Background worker: claims jobs from the PostgreSQL queue and runs their handlers.

* `worker_concurrency` claim loops per process (I/O overlap); CPU-heavy steps run in threads, and
  PDFium is serialized by a process-wide lock - scale CPU work with more worker processes.
* Wake-ups: LISTEN/NOTIFY for low latency, with polling as the fallback (a missed notification
  costs at most one poll interval).
* Leases are extended by a heartbeat while a job runs. If the lease is lost (e.g. a long GC pause
  let another worker reclaim the job), results are discarded rather than written twice.
* SIGTERM/SIGINT stop claiming new work; in-flight jobs finish first.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from typing import Any, Protocol

import psycopg
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from docintel.agent.graph import AgentDeps
from docintel.agent.runner import AgentAnalysisHandler, build_agent_deps
from docintel.ai.errors import ProviderError
from docintel.core.config import Settings
from docintel.core.logging import get_logger
from docintel.core.metrics import JOB_DURATION, JOBS, observe_stages
from docintel.db.models import JobStatus, JobType
from docintel.knowledge.processing import KnowledgeProcessingHandler
from docintel.processing.ocr import OCRError
from docintel.processing.pipeline import (
    DocumentProcessingHandler,
    PermanentProcessingError,
    build_stages,
)
from docintel.processing.services import ProcessingServices, build_processing_services
from docintel.storage import DocumentStorage, StorageError
from docintel.workers.queue import JOBS_CHANNEL, ClaimedJob, JobQueue
from docintel.workflows.engine import WorkflowHandler

logger = get_logger(__name__)

_LISTENER_RETRY_SECONDS = 5.0
_LOOP_ERROR_BACKOFF_SECONDS = 2.0


class LeaseLostError(Exception):
    """Another worker reclaimed this job; stop working on it."""


class JobHandler(Protocol):
    async def prepare(self, session: AsyncSession, job: ClaimedJob) -> Any: ...

    async def execute(self, context: Any, on_stage: Any) -> None: ...

    async def on_success(self, session: AsyncSession, context: Any, finished_at: Any) -> None: ...

    async def on_failure(
        self, session: AsyncSession, job: ClaimedJob, *, new_status: Any, user_message: str
    ) -> None: ...


def classify_failure(exc: BaseException, job_id: uuid.UUID) -> tuple[bool, str]:
    """Return (retryable, user-safe message). Details stay in the logs."""
    if isinstance(exc, PermanentProcessingError):
        return False, str(exc)
    if isinstance(exc, StorageError):
        if exc.retryable:
            return True, "Temporary storage failure; processing will be retried."
        return False, "The stored file could not be read."
    if isinstance(exc, OCRError):
        if exc.retryable:
            return True, "Text recognition timed out; processing will be retried."
        return False, "Text recognition failed."
    if isinstance(exc, ProviderError):
        if exc.retryable:
            return True, "The AI provider is temporarily unavailable; processing will be retried."
        return False, "The AI provider rejected the request."
    if isinstance(exc, OperationalError | DBAPIError | TimeoutError | ConnectionError | OSError):
        return True, "Temporary infrastructure failure; processing will be retried."
    return True, f"Unexpected processing error (reference: job {job_id})."


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(
        self,
        *,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: DocumentStorage,
        worker_id: str | None = None,
        services: ProcessingServices | None = None,
        agent: AgentDeps | None = None,
    ) -> None:
        self._settings = settings
        self.services = services or build_processing_services(settings, sessionmaker=sessionmaker)
        agent = agent or build_agent_deps(
            settings, sessionmaker, llm=self.services.llm, embedder=self.services.embedder
        )
        self._sessionmaker = sessionmaker
        self._queue = JobQueue(
            worker_id=worker_id or default_worker_id(),
            lease_seconds=settings.job_lease_seconds,
            retry_base_seconds=settings.job_retry_base_seconds,
        )
        self._handlers: dict[JobType, JobHandler] = {
            JobType.DOCUMENT_PROCESSING: DocumentProcessingHandler(
                storage, build_stages(storage, self.services), self.services
            ),
            JobType.KNOWLEDGE_PROCESSING: KnowledgeProcessingHandler(
                storage, self.services, self.services.embedder, self.services.chunking
            ),
            JobType.AGENT_ANALYSIS: AgentAnalysisHandler(agent),
            JobType.WORKFLOW: WorkflowHandler(agent, sessionmaker, settings),
        }
        self._wakeup = asyncio.Event()
        self._heartbeat_file = settings.worker_heartbeat_file

    @property
    def worker_id(self) -> str:
        return self._queue.worker_id

    # ------------------------------------------------------------------------ public API
    async def run_once(self) -> bool:
        """Claim and process at most one job. Returns True if a job was claimed."""
        async with self._sessionmaker() as session, session.begin():
            job = await self._queue.claim(session, list(self._handlers))
        if job is None:
            return False
        await self._process(job)
        return True

    async def run_until_idle(self, max_jobs: int | None = None) -> int:
        """Process jobs until none are runnable (or `max_jobs` is reached)."""
        processed = 0
        while max_jobs is None or processed < max_jobs:
            if not await self.run_once():
                break
            processed += 1
        return processed

    async def run(self, stop: asyncio.Event) -> None:
        logger.info(
            "worker.started",
            worker_id=self.worker_id,
            concurrency=self._settings.worker_concurrency,
        )
        listener = asyncio.create_task(self._listen(stop), name="worker-listener")
        loops = [
            asyncio.create_task(self._loop(stop), name=f"worker-loop-{index}")
            for index in range(self._settings.worker_concurrency)
        ]
        await stop.wait()
        listener.cancel()
        await asyncio.gather(listener, *loops, return_exceptions=True)
        logger.info("worker.stopped", worker_id=self.worker_id)

    # ------------------------------------------------------------------------ loop
    def _touch_heartbeat(self) -> None:
        if self._heartbeat_file is not None:
            with contextlib.suppress(OSError):
                self._heartbeat_file.touch()

    async def _loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self._touch_heartbeat()
            self._wakeup.clear()
            try:
                if await self.run_once():
                    continue
            except Exception:
                logger.exception("worker.loop_error")
                await asyncio.sleep(_LOOP_ERROR_BACKOFF_SECONDS)
                continue
            waiters = [
                asyncio.ensure_future(self._wakeup.wait()),
                asyncio.ensure_future(stop.wait()),
            ]
            try:
                await asyncio.wait(
                    waiters,
                    timeout=self._settings.worker_poll_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for waiter in waiters:
                    waiter.cancel()

    async def _listen(self, stop: asyncio.Event) -> None:
        conninfo = (
            make_url(self._settings.database_url)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
        while not stop.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {JOBS_CHANNEL}")
                    async for _notification in conn.notifies():
                        self._wakeup.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # polling keeps working while the listener reconnects
                logger.warning("worker.listener_error", error_type=type(exc).__name__)
                await asyncio.sleep(_LISTENER_RETRY_SECONDS)

    # ------------------------------------------------------------------------ job execution
    async def _process(self, job: ClaimedJob) -> None:
        started = time.perf_counter()
        outcome = "error"  # the attempt raised (e.g. the database went away)
        try:
            outcome = await self._attempt(job)
        finally:
            JOBS.labels(job.job_type.value, outcome).inc()
            JOB_DURATION.labels(job.job_type.value, outcome).observe(time.perf_counter() - started)

    async def _attempt(self, job: ClaimedJob) -> str:
        """Run one attempt of a job; returns its outcome for the metrics."""
        log = logger.bind(job_id=str(job.id), job_type=job.job_type.value, attempt=job.attempts)
        handler = self._handlers[job.job_type]

        if job.attempts > job.max_attempts:
            log.warning("worker.job_exhausted")
            return await self._fail(
                job, "Processing was interrupted repeatedly; giving up.", False, {}
            )

        async with self._sessionmaker() as session, session.begin():
            context = await handler.prepare(session, job)
            if context is None:
                await self._queue.cancel(session, job.id, reason="document deleted")
                log.info("worker.job_skipped", reason="document deleted")
                return "skipped"

        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(job, lease_lost))

        async def on_stage(stage: str, timings: dict[str, Any]) -> None:
            if lease_lost.is_set():
                raise LeaseLostError
            async with self._sessionmaker() as session, session.begin():
                if not await self._queue.record_progress(
                    session, job.id, stage=stage, stage_timings=dict(timings)
                ):
                    raise LeaseLostError

        try:
            await handler.execute(context, on_stage)
        except LeaseLostError:
            log.warning("worker.lease_lost")
            return "lease_lost"
        except Exception as exc:
            retryable, message = classify_failure(exc, job.id)
            log.warning(
                "worker.job_failed",
                retryable=retryable,
                error_type=type(exc).__name__,
                exc_info=not isinstance(exc, PermanentProcessingError),
            )
            return await self._fail(job, message, retryable, context.stage_timings)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        try:
            async with self._sessionmaker() as session, session.begin():
                finished_at = await self._queue.complete(
                    session, job.id, stage_timings=context.stage_timings
                )
                if finished_at is None:
                    log.warning("worker.lease_lost_before_commit")
                    return "lease_lost"
                await handler.on_success(session, context, finished_at)
        except Exception as exc:
            retryable, message = classify_failure(exc, job.id)
            log.exception("worker.result_commit_failed")
            return await self._fail(job, message, retryable, context.stage_timings)
        log.info("worker.job_completed", stage_timings_ms=context.stage_timings)
        if job.job_type == JobType.DOCUMENT_PROCESSING:
            observe_stages(context.stage_timings)
        return "completed"

    async def _heartbeat(self, job: ClaimedJob, lease_lost: asyncio.Event) -> None:
        interval = max(1.0, self._settings.job_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            self._touch_heartbeat()
            async with self._sessionmaker() as session, session.begin():
                if not await self._queue.heartbeat(session, job.id):
                    lease_lost.set()
                    return

    async def _fail(
        self, job: ClaimedJob, message: str, retryable: bool, timings: dict[str, Any]
    ) -> str:
        """Record a failed attempt; returns "retried", "failed" or "lease_lost"."""
        async with self._sessionmaker() as session, session.begin():
            new_status = await self._queue.fail(
                session, job, error=message, retryable=retryable, stage_timings=dict(timings)
            )
            if new_status is None:
                logger.warning("worker.lease_lost_on_failure", job_id=str(job.id))
                return "lease_lost"
            await self._handlers[job.job_type].on_failure(
                session, job, new_status=new_status, user_message=message
            )
        return "failed" if new_status == JobStatus.FAILED else "retried"
