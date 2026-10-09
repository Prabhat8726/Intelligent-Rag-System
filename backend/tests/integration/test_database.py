"""Schema-level guarantees: migrations round-trip, models match migrations, audit is append-only."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import docintel.db.models  # noqa: F401  (register tables)
from docintel.audit.service import SYSTEM_REQUEST, AuditAction, record_audit_event
from docintel.db import migrations_runner
from docintel.db.base import Base
from docintel.db.models import AuditOutcome
from tests.conftest import base_database_url, create_database, drop_database

pytestmark = pytest.mark.integration


@pytest.fixture
def scratch_database() -> Iterator[str]:
    base_url = base_database_url()
    url = create_database(base_url)
    try:
        yield url
    finally:
        drop_database(url, base_url)


async def test_migrations_upgrade_downgrade_upgrade(scratch_database: str) -> None:
    await asyncio.to_thread(migrations_runner.upgrade, scratch_database)
    await asyncio.to_thread(migrations_runner.downgrade, scratch_database, "base")
    engine = create_engine(scratch_database)
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) == {"alembic_version"}
        has_vector = conn.execute(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one()
        assert has_vector == 0
    await asyncio.to_thread(migrations_runner.upgrade, scratch_database)
    with engine.connect() as conn:
        assert {"departments", "users", "audit_logs"} <= set(inspect(conn).get_table_names())
    engine.dispose()


async def test_phase5_migration_opens_tasks_for_documents_waiting_for_review(
    scratch_database: str,
) -> None:
    await asyncio.to_thread(migrations_runner.upgrade, scratch_database, "0004")
    engine = create_engine(scratch_database)
    owner, uncertain_type, unread, done = (uuid.uuid4() for _ in range(4))
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash, role) "
                "VALUES (:id, 'a@example.test', 'A', 'x', 'ANALYST')"
            ),
            {"id": owner},
        )
        for document_id, status, reasons in (
            (uncertain_type, "REVIEW_REQUIRED", ["CLASSIFICATION_UNCERTAIN"]),
            (unread, "REVIEW_REQUIRED", ["LOW_OCR_CONFIDENCE", "MISSING_REQUIRED_FIELDS"]),
            (done, "COMPLETED", []),
        ):
            conn.execute(
                text(
                    "INSERT INTO documents (id, display_filename, status, sensitivity, source, "
                    "owner_id, review_reasons) VALUES (:id, 'f.pdf', :status, 'INTERNAL', "
                    "'UPLOAD', :owner, CAST(:reasons AS jsonb))"
                ),
                {
                    "id": document_id,
                    "status": status,
                    "owner": owner,
                    "reasons": json.dumps(reasons),
                },
            )
    await asyncio.to_thread(migrations_runner.upgrade, scratch_database)
    with engine.connect() as conn:
        tasks = {
            row.document_id: row
            for row in conn.execute(
                text(
                    "SELECT document_id, task_type, status, priority, reason_keys FROM review_tasks"
                )
            )
        }
        rules = conn.execute(text("SELECT count(*) FROM business_rules")).scalar_one()
    engine.dispose()
    assert set(tasks) == {uncertain_type, unread}
    assert (tasks[uncertain_type].task_type, tasks[uncertain_type].priority) == (
        "CLASSIFICATION_REVIEW",
        "NORMAL",
    )
    assert (tasks[unread].task_type, tasks[unread].priority, tasks[unread].status) == (
        "EXTRACTION_REVIEW",
        "HIGH",
        "OPEN",
    )
    assert tasks[unread].reason_keys == [
        "reason:LOW_OCR_CONFIDENCE",
        "reason:MISSING_REQUIRED_FIELDS",
    ]
    assert rules == 19


# Alembic cannot compare generated-column expressions; it warns and skips them.
@pytest.mark.filterwarnings("ignore:Computed default on .* cannot be modified:UserWarning")
def test_models_match_migrations(database_url: str) -> None:
    """Equivalent of `alembic check`: autogenerate finds nothing to change."""
    engine = create_engine(database_url)
    with engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(
                conn, opts={"compare_type": True, "compare_server_default": True}
            ),
            Base.metadata,
        )
    engine.dispose()
    assert diff == []


async def _write_audit(session: AsyncSession) -> int:
    entry = record_audit_event(
        session,
        action=AuditAction.USER_CREATED,
        outcome=AuditOutcome.SUCCESS,
        meta=SYSTEM_REQUEST,
        details={"note": "immutability test"},
    )
    await session.flush()
    return entry.id


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_logs SET action = 'tampered' WHERE id = :id",
        "DELETE FROM audit_logs WHERE id = :id",
        "TRUNCATE audit_logs",
    ],
)
async def test_audit_log_is_append_only(db_session: AsyncSession, statement: str) -> None:
    entry_id = await _write_audit(db_session)
    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(text(statement), {"id": entry_id})
    remaining = await db_session.scalar(
        text("SELECT action FROM audit_logs WHERE id = :id"), {"id": entry_id}
    )
    assert remaining == "user.created"


async def test_check_constraints_enforced(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_users_role"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash, role) "
                    "VALUES (gen_random_uuid(), 'x@example.test', 'X', 'h', 'SUPERUSER')"
                )
            )
    with pytest.raises(IntegrityError, match="ck_users_email_lowercase"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash, role) "
                    "VALUES (gen_random_uuid(), 'Upper@Example.test', 'X', 'h', 'VIEWER')"
                )
            )
