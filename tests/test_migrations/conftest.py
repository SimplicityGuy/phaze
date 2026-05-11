"""Fixtures for tests that actually run Alembic migrations against a real Postgres DB.

Pre-condition: the database ``phaze_migrations_test`` must exist on
``localhost:5432`` with the same credentials as ``phaze_test`` (operator-created,
matching the standing requirement for integration tests -- see
``tests/conftest.py``).

Unlike the parent ``tests/conftest.py``'s ``async_engine`` fixture (which uses
SQLAlchemy's metadata-driven table creation and so never exercises migration
files), the ``migrated_engine`` fixture exported here runs
``alembic.command.upgrade(cfg, 'head')`` against the dedicated migrations test
DB, so the actual migration revisions on disk are validated.

Note: ``alembic/env.py`` overrides ``sqlalchemy.url`` with
``settings.database_url`` on every run, so this conftest also patches the
in-memory ``settings.database_url`` for the duration of upgrade/downgrade
calls. ``_patched_settings_database_url`` is the small helper that does that.
"""

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic.config import Config
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from phaze.config import settings


MIGRATIONS_TEST_DATABASE_URL = "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_migrations_test"
ALEMBIC_INI_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"


@contextmanager
def _patched_settings_database_url(database_url: str) -> Iterator[None]:
    """Patch ``settings.database_url`` to ``database_url`` for the contextmanager body.

    ``alembic/env.py`` calls ``config.set_main_option('sqlalchemy.url', settings.database_url)``
    at import time, which overwrites anything ``_build_alembic_config`` sets on the cfg.
    Patching the singleton's attribute is the smallest-blast-radius way to make alembic
    point at the migrations test DB without modifying production env.py.
    """
    original = settings.database_url
    settings.database_url = database_url
    try:
        yield
    finally:
        settings.database_url = original


def _build_alembic_config(database_url: str) -> Config:
    """Build an in-memory Alembic Config pointing at ``database_url``.

    The returned cfg has ``sqlalchemy.url`` set so callers that read the cfg
    directly see the test URL. Callers that invoke ``command.upgrade`` /
    ``command.downgrade`` must wrap the call in ``_patched_settings_database_url``
    (or use ``upgrade_to`` / ``downgrade_to``), because ``alembic/env.py``
    re-overrides ``sqlalchemy.url`` from ``settings.database_url``.
    """
    cfg = Config(str(ALEMBIC_INI_PATH))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def upgrade_to(cfg: Config, revision: str) -> None:
    """Upgrade to ``revision`` using the test database URL on cfg.

    Thin wrapper around ``alembic.command.upgrade`` that also patches
    ``settings.database_url`` so ``alembic/env.py`` does not overwrite the cfg URL.
    """
    database_url = cfg.get_main_option("sqlalchemy.url") or MIGRATIONS_TEST_DATABASE_URL
    with _patched_settings_database_url(database_url):
        command.upgrade(cfg, revision)


def downgrade_to(cfg: Config, revision: str) -> None:
    """Downgrade to ``revision`` using the test database URL on cfg.

    Thin wrapper around ``alembic.command.downgrade`` that also patches
    ``settings.database_url`` so ``alembic/env.py`` does not overwrite the cfg URL.
    """
    database_url = cfg.get_main_option("sqlalchemy.url") or MIGRATIONS_TEST_DATABASE_URL
    with _patched_settings_database_url(database_url):
        command.downgrade(cfg, revision)


async def _reset_schema(database_url: str) -> None:
    """Drop and recreate the ``public`` schema on ``database_url``.

    Used as fixture teardown instead of ``downgrade_to('base')`` because tests
    that deliberately insert duplicate ``original_path`` rows under different
    ``agent_id`` values (validating the composite UQ from migration 013) would
    otherwise trip the D-16 guard during the 013->012 downgrade step, leaving
    the DB stuck mid-chain. A bare DROP/CREATE bypasses the migration chain
    entirely and is faster than walking 13 downgrade steps.
    """
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def migrated_engine() -> AsyncGenerator:
    """Upgrade to head, yield an async engine bound to the migrations test DB, reset schema on teardown.

    ``upgrade_to`` / ``downgrade_to`` are sync helpers that internally trigger
    ``alembic/env.py``, which calls ``asyncio.run(run_async_migrations())``.
    When invoked directly from this async fixture, the nested ``asyncio.run``
    crashes with "cannot be called from a running event loop". Running the
    sync alembic commands in a worker thread sidesteps the conflict.

    Teardown resets the schema rather than calling ``downgrade_to('base')`` so
    tests that exercise the D-16 guard by inserting duplicate paths under
    different agents do not strand the DB at revision 013 between tests.
    """
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "head")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        yield engine
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


__all__ = [
    "ALEMBIC_INI_PATH",
    "MIGRATIONS_TEST_DATABASE_URL",
    "_build_alembic_config",
    "downgrade_to",
    "migrated_engine",
    "upgrade_to",
]
