"""Programmatic access to Alembic (tests, readiness checks, CLI)."""

from __future__ import annotations

from functools import lru_cache

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

SCRIPT_LOCATION = "docintel.db:migrations"


def make_alembic_config(database_url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    if database_url is not None:
        # Passed via attributes, not set_main_option, so '%' in passwords needs no escaping.
        config.attributes["database_url"] = database_url
    return config


@lru_cache(maxsize=1)
def head_revisions() -> frozenset[str]:
    """Revision ids of the migration heads shipped with this build."""
    return frozenset(ScriptDirectory.from_config(make_alembic_config()).get_heads())


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(make_alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str) -> None:
    command.downgrade(make_alembic_config(database_url), revision)
