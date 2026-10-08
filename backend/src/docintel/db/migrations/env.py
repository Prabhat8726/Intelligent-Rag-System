"""Alembic environment.

URL resolution order: `config.attributes["database_url"]` (programmatic use, tests) then the
DATABASE_URL environment variable (CLI / migrate container). The full application settings are
deliberately not required here, so migrations can run with only database credentials.
"""

from __future__ import annotations

import logging
import os

from alembic import context
from sqlalchemy import create_engine, pool

import docintel.db.models  # noqa: F401  (registers all tables on Base.metadata)
from docintel.core.config import LogFormat
from docintel.core.logging import configure_logging
from docintel.db.base import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url") or os.environ.get("DATABASE_URL")
    if not url:
        msg = "DATABASE_URL is not set; cannot run migrations"
        raise RuntimeError(msg)
    return str(url)


if not logging.getLogger().handlers:
    configure_logging(level="INFO", log_format=LogFormat.CONSOLE)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is not None:
        # A caller (e.g. tests) supplied an open connection.
        context.configure(
            connection=connectable,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
