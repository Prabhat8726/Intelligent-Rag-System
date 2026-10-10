"""Purge of deleted documents and storage reconciliation (ADR-075), with committed transactions."""

from __future__ import annotations

import argparse
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from docintel import cli
from docintel.core.config import Settings
from docintel.db.models import (
    AuditLog,
    Department,
    Document,
    DocumentPage,
    DocumentVersion,
    RefreshToken,
    Role,
    User,
)
from docintel.documents import retention
from docintel.storage import LocalStorage, StorageUnavailableError
from tests.conftest import make_settings
from tests.factories.files import invoice_pdf_bytes
from tests.integration.test_worker import ingest, make_worker

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


@pytest.fixture
async def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def worker_settings(database_url: str, storage_root: Path) -> Settings:
    return make_settings(database_url=database_url, storage_local_root=storage_root)


@pytest.fixture
def storage(storage_root: Path) -> LocalStorage:
    return LocalStorage(storage_root)


@pytest.fixture
async def uploader(maker: async_sessionmaker[AsyncSession]) -> User:
    async with maker() as session, session.begin():
        department = Department(id=uuid.uuid4(), name=f"Retention-{uuid.uuid4().hex[:8]}")
        user = User(
            id=uuid.uuid4(),
            email=f"retention-{uuid.uuid4().hex[:8]}@example.test",
            full_name="Retention Test",
            password_hash="not-used",
            role=Role.ANALYST,
            department_id=department.id,
        )
        session.add_all([department, user])
    return user


async def processed(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    settings: Settings,
    user: User,
) -> tuple[uuid.UUID, list[str]]:
    """A processed invoice and the keys of its stored files (original and page previews)."""
    document_id = await ingest(maker, storage, settings, user, invoice_pdf_bytes(pages=2))
    await make_worker(settings, maker, storage).run_until_idle()
    async with maker() as session:
        original = await session.scalar(
            select(DocumentVersion.storage_key).where(DocumentVersion.document_id == document_id)
        )
        previews = await session.scalars(
            select(DocumentPage.preview_storage_key)
            .join(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
        )
        keys = [key for key in (original, *previews) if key]
    return document_id, keys


async def deleted_days_ago(
    maker: async_sessionmaker[AsyncSession], document_id: uuid.UUID, days: int
) -> None:
    async with maker() as session, session.begin():
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(deleted_at=NOW - timedelta(days=days))
        )


def stored(storage: LocalStorage, keys: list[str]) -> list[bool]:
    return [(storage.root / key).is_file() for key in keys]


async def test_documents_deleted_before_the_cutoff_are_purged_rows_then_files(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
) -> None:
    old, old_keys = await processed(maker, storage, worker_settings, uploader)
    recent, recent_keys = await processed(maker, storage, worker_settings, uploader)
    assert len(old_keys) == 3  # the original and two page previews
    await deleted_days_ago(maker, old, 40)
    await deleted_days_ago(maker, recent, 5)
    async with maker() as session, session.begin():
        session.add_all(
            [
                RefreshToken(
                    user_id=uploader.id,
                    family_id=uuid.uuid4(),
                    token_hash=secrets.token_hex(32),
                    created_at=NOW - timedelta(days=10),
                    expires_at=NOW - timedelta(days=2),
                    session_expires_at=NOW - timedelta(days=2),
                ),
                RefreshToken(
                    user_id=uploader.id,
                    family_id=uuid.uuid4(),
                    token_hash=secrets.token_hex(32),
                    expires_at=NOW + timedelta(hours=12),
                    session_expires_at=NOW + timedelta(days=7),
                ),
            ]
        )

    preview = await retention.purge_deleted_documents(
        maker, storage, older_than=timedelta(days=30), dry_run=True
    )
    assert (preview.documents, preview.files, preview.refresh_tokens) == (1, 3, 1)
    assert stored(storage, old_keys) == [True] * 3

    result = await retention.purge_deleted_documents(maker, storage, older_than=timedelta(days=30))

    assert (result.documents, result.files, result.files_failed) == (1, 3, 0)
    assert result.refresh_tokens == 1
    assert stored(storage, old_keys) == [False] * 3
    assert stored(storage, recent_keys) == [True] * 3
    async with maker() as session:
        assert await session.get(Document, old) is None
        assert await session.get(Document, recent) is not None
        versions = await session.scalar(
            select(func.count()).where(DocumentVersion.document_id == old)
        )
        assert versions == 0
        event = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "document.purged", AuditLog.entity_id == str(old)
            )
        )
        assert event is not None
        assert event.actor_type == "SYSTEM"
        tokens = await session.scalar(
            select(func.count()).where(RefreshToken.user_id == uploader.id)
        )
        assert tokens == 1  # the live session stays


class UndeletableStorage(LocalStorage):
    async def delete(self, key: str) -> None:
        msg = "simulated outage"
        raise StorageUnavailableError(msg)


async def test_a_failed_file_deletion_leaves_an_orphan_for_the_reconciliation(
    maker: async_sessionmaker[AsyncSession],
    storage_root: Path,
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
) -> None:
    document_id, keys = await processed(maker, storage, worker_settings, uploader)
    await deleted_days_ago(maker, document_id, 40)

    result = await retention.purge_deleted_documents(
        maker, UndeletableStorage(storage_root), older_than=timedelta(days=30)
    )

    assert (result.documents, result.files, result.files_failed) == (1, 0, 3)
    async with maker() as session:
        assert await session.get(Document, document_id) is None  # rows first: no dangling row
    reconciled = await retention.reconcile_storage(
        maker, storage, min_age=timedelta(0), delete_orphans=True
    )
    assert {orphan.key for orphan in reconciled.orphans} == set(keys)
    assert reconciled.deleted == 3
    assert stored(storage, keys) == [False] * 3


async def test_reconciliation_reports_orphans_and_missing_files(
    maker: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    worker_settings: Settings,
    uploader: User,
) -> None:
    document_id, keys = await processed(maker, storage, worker_settings, uploader)
    old_orphan = f"documents/{uuid.uuid4()}/v1/original.pdf"
    new_orphan = f"knowledge/{uuid.uuid4()}/original.md"
    for key in (old_orphan, new_orphan):
        (storage.root / key).parent.mkdir(parents=True, exist_ok=True)
        (storage.root / key).write_bytes(b"left behind")
    two_days_ago = (NOW - timedelta(days=2)).timestamp()
    os.utime(storage.root / old_orphan, (two_days_ago, two_days_ago))
    # A referenced file lost from storage (the row is old enough to judge).
    (storage.root / keys[0]).unlink()
    async with maker() as session, session.begin():
        await session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .values(created_at=NOW - timedelta(hours=1))
        )

    report = await retention.reconcile_storage(maker, storage, min_age=timedelta(hours=24))

    assert [orphan.key for orphan in report.orphans] == [old_orphan]
    assert report.too_recent == 1
    assert keys[0] in report.missing
    assert report.deleted == 0
    assert (storage.root / old_orphan).is_file()  # reported only

    cleaned = await retention.reconcile_storage(
        maker, storage, min_age=timedelta(hours=24), delete_orphans=True
    )
    assert cleaned.deleted == 1
    assert not (storage.root / old_orphan).exists()
    assert (storage.root / new_orphan).is_file()  # too recent: an upload may be committing
    assert stored(storage, keys[1:]) == [True, True]  # referenced previews are kept


async def test_cli_commands(
    worker_settings: Settings, storage: LocalStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    dry_run = argparse.Namespace(older_than_days=None, dry_run=True)
    assert await cli._purge_deleted(worker_settings, dry_run) == cli.EXIT_OK
    assert "would purge" in capsys.readouterr().out

    orphan = storage.root / "documents" / str(uuid.uuid4()) / "v1" / "original.pdf"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"left behind")
    report_only = argparse.Namespace(delete_orphans=False, min_age_hours=0.0)
    await cli._storage_reconcile(worker_settings, report_only)
    out = capsys.readouterr().out
    assert f"orphan   {orphan.relative_to(storage.root).as_posix()}" in out
    assert "--delete-orphans" in out
    assert orphan.is_file()
