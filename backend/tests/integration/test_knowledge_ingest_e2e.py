"""`docintel knowledge-ingest` end to end: REST client -> API -> queue -> worker."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from docintel import cli
from docintel.api.app import create_app
from docintel.audit.service import SYSTEM_REQUEST
from docintel.auth.service import UserService
from docintel.db.models import ProcessingJob, Role
from docintel.storage import LocalStorage
from docintel.tools import knowledge_ingest as knowledge_tool
from docintel.tools.knowledge_ingest import ALREADY_PRESENT, KnowledgeItem, ingest_knowledge
from docintel.workers.runner import Worker
from tests.conftest import TEST_PASSWORD, make_settings

pytestmark = pytest.mark.integration

KNOWLEDGE_BASE = Path(__file__).resolve().parents[3] / "knowledge_base"


async def test_seed_knowledge_base_through_the_api(
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
    email = f"kb-admin-{uuid.uuid4().hex[:8]}@example.test"
    async with sessions() as session:
        await session.execute(delete(ProcessingJob))
        service = UserService(session)
        await service.get_or_create_department("Legal", meta=SYSTEM_REQUEST)  # the playbook's
        await service.create_user(
            email=email,
            full_name="Knowledge Admin",
            password=TEST_PASSWORD,
            role=Role.ADMIN,
            department=None,
            meta=SYSTEM_REQUEST,
        )
        await session.commit()

    app = create_app(settings)
    worker = Worker(settings=settings, sessionmaker=sessions, storage=LocalStorage(storage_root))
    stop = asyncio.Event()
    async with app.router.lifespan_context(app):
        worker_task = asyncio.create_task(worker.run(stop))
        try:
            runs = []
            for _ in range(2):  # the second run finds everything already there
                runs.append(
                    await ingest_knowledge(
                        KNOWLEDGE_BASE,
                        api_url="http://testserver",
                        email=email,
                        password=TEST_PASSWORD,
                        timeout_seconds=60,
                        poll_interval_seconds=0.2,
                        transport=httpx.ASGITransport(app=app),
                    )
                )
        finally:
            stop.set()
            await asyncio.wait_for(worker_task, timeout=10)

    first, second = runs
    files = sorted(path.name for path in KNOWLEDGE_BASE.glob("*.md"))
    assert sorted(item.file for item in first) == files
    statuses = {item.file: item.status for item in first}
    assert statuses.pop("procurement-policy-2025.md") == "SUPERSEDED"
    assert set(statuses.values()) == {"ACTIVE"}
    assert all(item.chunks for item in first)
    assert {item.status for item in second} == {ALREADY_PRESENT}


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        (
            [KnowledgeItem("a.md", status="ACTIVE"), KnowledgeItem("b.md", status=ALREADY_PRESENT)],
            0,
        ),
        ([KnowledgeItem("a.md", status="FAILED", note="Text recognition failed.")], 1),
        ([KnowledgeItem("a.md", http_status=422, note="category is required")], 1),
    ],
)
def test_knowledge_ingest_exit_code(
    items: list[KnowledgeItem],
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake(*_args: object, **_kwargs: object) -> list[KnowledgeItem]:
        return items

    monkeypatch.setenv("SEED_USER_PASSWORD", "unused-in-this-test")
    monkeypatch.setattr(knowledge_tool, "ingest_knowledge", fake)
    assert cli.main(["knowledge-ingest", str(tmp_path)]) == expected
    output = capsys.readouterr().out
    for item in items:
        assert item.file in output
