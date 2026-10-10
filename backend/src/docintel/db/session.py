"""Async engine and session factory (SQLAlchemy 2.x + psycopg 3)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from docintel.core.config import Settings
from docintel.core.metrics import instrument_engine


def create_engine(settings: Settings) -> AsyncEngine:
    engine = create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        echo=settings.db_echo,
        connect_args={
            # Server-side guard against runaway queries; applies to every connection.
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
            "application_name": "docintel",
        },
    )
    instrument_engine(engine)  # statement latency for /metrics
    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: response models are built after commit without extra queries.
    return async_sessionmaker(engine, expire_on_commit=False)
