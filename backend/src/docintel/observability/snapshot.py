"""Gauges read from the database when `/metrics` is scraped (Module 24, ADR-073).

Counters in `docintel.core.metrics` describe what one process did; the state of the queue and
the review backlog lives in the database and is the same for every replica, so it is read at
scrape time instead (a few aggregate queries over indexed columns). Every replica reports the
same values: dashboards and alerts take `max` across instances. Label values come from enums
and are zero-filled, so a series does not vanish when its count drops to zero (which would
leave an alert on it silently inactive).

When the database cannot be read, the scrape still succeeds with
`docintel_snapshot_success 0`, so the process metrics stay visible during an outage.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from docintel.core.config import Settings
from docintel.core.logging import get_logger
from docintel.db.models import (
    Document,
    DocumentStatus,
    JobStatus,
    JobType,
    LLMCall,
    LLMCallStatus,
    ProcessingJob,
    ReviewPriority,
    ReviewTask,
    Workflow,
)
from docintel.db.models.matching import OPEN_TASK_STATUSES
from docintel.db.models.workflows import ACTIVE_WORKFLOW_STATUSES

logger = get_logger(__name__)

SNAPSHOT_TIMEOUT_SECONDS = 5.0
ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.PROCESSING)
FAILED_JOB_WINDOW = timedelta(hours=1)


class _Families(Collector):
    def __init__(self, families: Iterable[GaugeMetricFamily]) -> None:
        self._families = list(families)

    def collect(self) -> Iterator[GaugeMetricFamily]:
        yield from self._families


def _gauge(name: str, documentation: str, labels: list[str] | None = None) -> GaugeMetricFamily:
    return GaugeMetricFamily(f"docintel_{name}", documentation, labels=labels)


async def _queue(session: AsyncSession, now: datetime) -> list[GaugeMetricFamily]:
    jobs = _gauge("jobs", "Jobs waiting or running, by type and status.", ["job_type", "status"])
    counts = {
        (job_type, status): count
        for job_type, status, count in await session.execute(
            select(ProcessingJob.job_type, ProcessingJob.status, func.count())
            .where(ProcessingJob.status.in_(ACTIVE_JOB_STATUSES))
            .group_by(ProcessingJob.job_type, ProcessingJob.status)
        )
    }
    for job_type in JobType:
        for status in ACTIVE_JOB_STATUSES:
            jobs.add_metric([job_type.value, status.value], counts.get((job_type, status), 0))

    oldest = _gauge(
        "jobs_oldest_ready_age_seconds",
        "Seconds the oldest runnable queued job has waited (0 when none waits).",
        ["job_type"],
    )
    ready = dict(
        (
            await session.execute(
                select(ProcessingJob.job_type, func.min(ProcessingJob.run_after))
                .where(ProcessingJob.status == JobStatus.QUEUED, ProcessingJob.run_after <= now)
                .group_by(ProcessingJob.job_type)
            )
        ).all()
    )
    for job_type in JobType:
        since = ready.get(job_type)
        oldest.add_metric([job_type.value], max((now - since).total_seconds(), 0) if since else 0)

    expired = _gauge(
        "jobs_expired_leases",
        "Running jobs whose lease has expired (a worker stopped without finishing them).",
    )
    expired.add_metric(
        [],
        await session.scalar(
            select(func.count())
            .select_from(ProcessingJob)
            .where(
                ProcessingJob.status == JobStatus.PROCESSING,
                ProcessingJob.lease_expires_at < now,
            )
        )
        or 0,
    )

    failed = _gauge(
        "jobs_failed_last_hour",
        "Jobs that failed permanently in the last hour, by type.",
        ["job_type"],
    )
    failed_counts = dict(
        (
            await session.execute(
                select(ProcessingJob.job_type, func.count())
                .where(
                    ProcessingJob.status == JobStatus.FAILED,
                    ProcessingJob.finished_at >= now - FAILED_JOB_WINDOW,
                )
                .group_by(ProcessingJob.job_type)
            )
        ).all()
    )
    for job_type in JobType:
        failed.add_metric([job_type.value], failed_counts.get(job_type, 0))
    return [jobs, oldest, expired, failed]


async def _work(session: AsyncSession, now: datetime) -> list[GaugeMetricFamily]:
    documents = _gauge("documents", "Documents (not deleted) by processing status.", ["status"])
    by_status = dict(
        (
            await session.execute(
                select(Document.status, func.count())
                .where(Document.deleted_at.is_(None))
                .group_by(Document.status)
            )
        ).all()
    )
    for document_status in DocumentStatus:
        documents.add_metric([document_status.value], by_status.get(document_status, 0))

    open_tasks = _gauge("review_tasks_open", "Open review tasks by priority.", ["priority"])
    overdue = _gauge(
        "review_tasks_overdue", "Open review tasks past their due date, by priority.", ["priority"]
    )
    rows = (
        await session.execute(
            select(
                ReviewTask.priority,
                func.count(),
                func.count().filter(ReviewTask.due_at < now),
            )
            .where(ReviewTask.status.in_(OPEN_TASK_STATUSES))
            .group_by(ReviewTask.priority)
        )
    ).all()
    tasks = {priority: (total, late) for priority, total, late in rows}
    for priority in ReviewPriority:
        total, late = tasks.get(priority, (0, 0))
        open_tasks.add_metric([priority.value], total)
        overdue.add_metric([priority.value], late)

    workflows = _gauge("workflows_active", "Workflows not yet finished, by status.", ["status"])
    active = dict(
        (
            await session.execute(
                select(Workflow.status, func.count())
                .where(Workflow.status.in_(ACTIVE_WORKFLOW_STATUSES))
                .group_by(Workflow.status)
            )
        ).all()
    )
    for workflow_status in ACTIVE_WORKFLOW_STATUSES:
        workflows.add_metric([workflow_status.value], active.get(workflow_status, 0))
    return [documents, open_tasks, overdue, workflows]


async def _models(
    session: AsyncSession, settings: Settings, now: datetime
) -> list[GaugeMetricFamily]:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    calls = _gauge(
        "llm_calls_today",
        "Model calls since midnight UTC (all processes), by provider and status.",
        ["provider", "status"],
    )
    cost = _gauge(
        "llm_estimated_cost_usd_today",
        "Estimated model cost since midnight UTC at the configured list prices.",
        ["provider"],
    )
    rows = (
        await session.execute(
            select(
                LLMCall.provider,
                LLMCall.status,
                func.count(),
                func.coalesce(func.sum(LLMCall.estimated_cost_usd), 0),
            )
            .where(LLMCall.created_at >= day_start)
            .group_by(LLMCall.provider, LLMCall.status)
        )
    ).all()
    by_provider: dict[str, float] = {}
    seen: set[tuple[str, str]] = set()
    for provider, status, count, spent in rows:
        calls.add_metric([provider, status.value], count)
        seen.add((provider, status.value))
        by_provider[provider] = by_provider.get(provider, 0.0) + float(spent or 0)
    configured = settings.llm_provider.value
    for status in LLMCallStatus:
        if (configured, status.value) not in seen:
            calls.add_metric([configured, status.value], 0)
    by_provider.setdefault(configured, 0.0)
    for provider, total in sorted(by_provider.items()):
        cost.add_metric([provider], total)

    budget = _gauge(
        "llm_daily_request_budget", "LLM_DAILY_REQUEST_BUDGET (0 = no limit configured)."
    )
    budget.add_metric([], settings.llm_daily_request_budget)
    return [calls, cost, budget]


async def collect(session: AsyncSession, settings: Settings) -> list[GaugeMetricFamily]:
    """The snapshot gauges, plus whether reading them succeeded and how long it took."""
    started = time.perf_counter()
    families: list[GaugeMetricFamily] = []
    success = 1
    try:
        async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
            now = datetime.now(UTC)
            families += await _queue(session, now)
            families += await _work(session, now)
            families += await _models(session, settings, now)
    except Exception as exc:  # the scrape must still return the process metrics
        logger.warning("metrics.snapshot_failed", error_type=type(exc).__name__)
        families, success = [], 0
    ok = _gauge("snapshot_success", "1 if the database gauges of this scrape were read.")
    ok.add_metric([], success)
    took = _gauge("snapshot_duration_seconds", "Time spent reading the database gauges.")
    took.add_metric([], time.perf_counter() - started)
    return [*families, ok, took]


def exposition(families: Iterable[GaugeMetricFamily]) -> bytes:
    """Prometheus text format of a set of gauge families."""
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_Families(families))
    return generate_latest(registry)
