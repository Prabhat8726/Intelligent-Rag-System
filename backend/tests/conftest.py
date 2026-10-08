"""Shared fixtures.

Integration tests run against a real PostgreSQL server (TEST_DATABASE_URL). A uniquely named
database is created and migrated once per test session and dropped afterwards. Each test runs
inside an outer transaction that is rolled back, while service-level commits become savepoints
(`join_transaction_mode="create_savepoint"`), so tests are isolated without truncation.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from docintel.api.app import create_app
from docintel.api.deps import get_session
from docintel.auth.passwords import hash_password
from docintel.core.config import Settings
from docintel.db import migrations_runner
from docintel.db.models import Department, Role, User

TEST_JWT_SECRET = "test-only-jwt-signing-key-0123456789abcdefghijklmnop"
TEST_PASSWORD = "correct horse battery staple"
# Strong, random secret for production-mode settings tests (generated, never a literal).
PRODUCTION_SECRET = secrets.token_urlsafe(32)
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://docintel:docintel@localhost:5432/docintel"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": DEFAULT_TEST_DATABASE_URL,
        "jwt_secret_key": TEST_JWT_SECRET,
        "log_level": "WARNING",
        "gemini_api_key": None,
        "seed_user_password": None,
        "cors_allowed_origins": [],
        "auth_max_failed_logins": 3,
        "auth_lockout_minutes": 15,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ------------------------------------------------------------------------------ database
def _server_dsn(url: str) -> str:
    """libpq DSN for the maintenance connection used to create/drop test databases."""
    parsed = make_url(url)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def create_database(base_url: str) -> str:
    """Create a uniquely named database on the server of `base_url`; return its URL."""
    name = f"docintel_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_server_dsn(base_url), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return make_url(base_url).set(database=name).render_as_string(hide_password=False)


def drop_database(url: str, base_url: str) -> None:
    name = make_url(url).database
    assert name, url
    with psycopg.connect(_server_dsn(base_url), autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def base_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    base_url = base_database_url()
    url = create_database(base_url)
    try:
        migrations_runner.upgrade(url)
        yield url
    finally:
        drop_database(url, base_url)


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


# ------------------------------------------------------------------------------ application
@pytest.fixture
def settings(database_url: str) -> Settings:
    return make_settings(database_url=database_url)


@pytest.fixture
async def app(settings: Settings, db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    application = create_app(settings)

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _test_session
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 51000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


# ------------------------------------------------------------------------------ factories
@pytest.fixture
async def department(db_session: AsyncSession) -> Department:
    dept = Department(id=uuid.uuid4(), name=f"Finance-{uuid.uuid4().hex[:6]}")
    db_session.add(dept)
    await db_session.flush()
    return dept


async def make_user(
    session: AsyncSession,
    *,
    role: Role = Role.ANALYST,
    department: Department | None = None,
    email: str | None = None,
    password: str = TEST_PASSWORD,
    is_active: bool = True,
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email or f"{role.value.lower()}-{uuid.uuid4().hex[:8]}@example.test",
        full_name=f"Test {role.value.title()}",
        password_hash=hash_password(password),
        role=role,
        department_id=department.id if department else None,
        is_active=is_active,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def login(client: httpx.AsyncClient, email: str, password: str = TEST_PASSWORD) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token
