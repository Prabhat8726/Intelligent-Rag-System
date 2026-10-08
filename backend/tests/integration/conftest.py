"""Shared integration fixtures: an API client plus a worker over the session database.

The worker commits in its own transactions, so these tests use committed data; users and
departments get unique names per test.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from docintel.api.app import create_app
from docintel.core.config import Settings
from docintel.db.models import Department, ProcessingJob, Role, User
from docintel.processing.services import ProcessingServices
from docintel.storage import LocalStorage
from docintel.workers.runner import Worker
from tests.conftest import auth_headers, make_settings


@dataclass
class Env:
    settings: Settings
    maker: async_sessionmaker[AsyncSession]
    storage: LocalStorage
    client: httpx.AsyncClient
    analyst: User
    reviewer: User
    viewer: User
    outsider: User

    def worker(self, services: ProcessingServices | None = None) -> Worker:
        return Worker(
            settings=self.settings,
            sessionmaker=self.maker,
            storage=self.storage,
            services=services,
        )

    async def upload(
        self,
        content: bytes,
        filename: str = "doc.pdf",
        content_type: str = "application/pdf",
        sensitivity: str = "INTERNAL",
    ) -> str:
        response = await self.client.post(
            "/api/v1/documents",
            headers=auth_headers(self.analyst),
            files={"file": (filename, content, content_type)},
            data={"sensitivity": sensitivity},
        )
        assert response.status_code == 201, response.text
        document_id: str = response.json()["id"]
        return document_id

    async def detail(self, document_id: str, user: User | None = None) -> dict[str, Any]:
        response = await self.client.get(
            f"/api/v1/documents/{document_id}", headers=auth_headers(user or self.analyst)
        )
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()
        return data


@pytest.fixture
async def env(engine: AsyncEngine, database_url: str, tmp_path: Path) -> AsyncIterator[Env]:
    storage_root = tmp_path / "storage"
    settings = make_settings(
        database_url=database_url,
        storage_local_root=storage_root,
        job_retry_base_seconds=0,
        page_preview_width=300,
    )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(delete(ProcessingJob))  # nothing left over from other tests
        finance = Department(id=uuid.uuid4(), name=f"Finance-{uuid.uuid4().hex[:6]}")
        legal = Department(id=uuid.uuid4(), name=f"Legal-{uuid.uuid4().hex[:6]}")
        users = {
            role_name: User(
                id=uuid.uuid4(),
                email=f"{role_name}-{uuid.uuid4().hex[:8]}@example.test",
                full_name=f"Test {role_name}",
                password_hash="not-used",
                role=role,
                department_id=(legal if role_name == "outsider" else finance).id,
            )
            for role_name, role in (
                ("analyst", Role.ANALYST),
                ("reviewer", Role.REVIEWER),
                ("viewer", Role.VIEWER),
                ("outsider", Role.REVIEWER),
            )
        }
        session.add_all([finance, legal, *users.values()])
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield Env(
                settings,
                maker,
                LocalStorage(storage_root),
                client,
                users["analyst"],
                users["reviewer"],
                users["viewer"],
                users["outsider"],
            )
