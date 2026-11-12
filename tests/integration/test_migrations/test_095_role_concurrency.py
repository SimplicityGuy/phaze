"""Migration 095's cluster-wide role DDL is safe when two databases migrate at once (phaze-2al7q.3, finding 2).

``analytics_ro`` is a ROLE, so it lives in the cluster, while every seat's database is its own: two seats migrating at
the same moment on the shared 5433 harness run 095's role statements against the same catalog row. Check-then-create
used to race (the second ``CREATE ROLE`` fails on the unique index once the first commits), and a downgrade's
``DROP ROLE`` could land under a concurrent upgrade that had already seen the role and skipped creating it.

These tests replay 095's OWN role statements (``ROLE_UPGRADE`` / ``ROLE_DOWNGRADE``, read from the migration module)
from two connections to two different databases, interleaved so the second really waits on the first. Each run uses a
throwaway role name, never ``analytics_ro`` itself: dropping or creating the real role here would act on every other
seat's databases.
"""

import asyncio
from collections.abc import AsyncGenerator, Sequence
import importlib.util
import os
from pathlib import Path
from types import ModuleType
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from tests.db_guard import require_test_database

from .conftest import MIGRATIONS_TEST_DATABASE_URL


pytestmark = pytest.mark.asyncio

_OTHER_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test")
require_test_database(_OTHER_DATABASE_URL, context="095 role concurrency tests")


def _migration() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "095_analytics_ro_role.py"
    spec = importlib.util.spec_from_file_location("migration_095", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _migration()


@pytest_asyncio.fixture
async def engines() -> AsyncGenerator[tuple[AsyncEngine, AsyncEngine, str]]:
    """Two engines on two different databases of the same cluster, and a throwaway role name dropped afterwards."""
    assert MIGRATIONS_TEST_DATABASE_URL.rsplit("/", 1)[1] != _OTHER_DATABASE_URL.rsplit("/", 1)[1], "the race needs two databases"
    role = f"analytics_ro_race_{uuid.uuid4().hex[:8]}"
    first, second = create_async_engine(MIGRATIONS_TEST_DATABASE_URL), create_async_engine(_OTHER_DATABASE_URL)
    try:
        yield first, second, role
    finally:
        async with first.begin() as conn:
            await conn.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")
        await first.dispose()
        await second.dispose()


def _as(role: str, statements: Sequence[str]) -> list[str]:
    """095's statements with the throwaway role in place of ``analytics_ro``."""
    return [statement.replace(MIGRATION.ROLE, role) for statement in statements]


async def _wait_until_blocked(probe: AsyncConnection, pid: int) -> None:
    """Return once backend ``pid`` is waiting on a lock another backend holds (bounded: fails rather than hangs)."""
    for _ in range(200):
        if (await probe.execute(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid})).scalar_one():
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"backend {pid} never waited on the first migration: the interleaving this test needs did not happen")


async def _run_second(engine: AsyncEngine, statements: Sequence[str], pid_ready: asyncio.Future[int]) -> None:
    async with engine.begin() as conn:
        pid_ready.set_result((await conn.exec_driver_sql("SELECT pg_backend_pid()")).scalar_one())
        for statement in statements:
            await conn.exec_driver_sql(statement)


async def _interleave(first: AsyncEngine, second: AsyncEngine, first_statements: Sequence[str], second_statements: Sequence[str]) -> None:
    """Run ``first_statements`` uncommitted, start ``second_statements`` in another database, let it block, then commit both."""
    pid_ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    async with first.connect() as probe, first.begin() as conn:
        for statement in first_statements:
            await conn.exec_driver_sql(statement)
        task = asyncio.create_task(_run_second(second, second_statements, pid_ready))
        await _wait_until_blocked(probe, await asyncio.wait_for(pid_ready, 10))
    await asyncio.wait_for(task, 30)  # raises here if the second migration's role DDL failed


async def _role(engine: AsyncEngine, role: str) -> tuple[bool, list[str]]:
    async with engine.connect() as conn:
        exists = (await conn.execute(text("SELECT count(*) FROM pg_roles WHERE rolname = :r"), {"r": role})).scalar_one() == 1
        settings = (
            await conn.execute(
                text(
                    "SELECT coalesce(array_agg(c), '{}') FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole, unnest(s.setconfig) c "
                    "WHERE r.rolname = :r AND s.setdatabase = 0"
                ),
                {"r": role},
            )
        ).scalar_one()
    return exists, list(settings)


async def test_two_databases_upgrading_at_once_both_succeed_and_the_role_is_created_once(engines: tuple[AsyncEngine, AsyncEngine, str]) -> None:
    first, second, role = engines
    upgrade = _as(role, MIGRATION.ROLE_UPGRADE)

    await _interleave(first, second, upgrade, upgrade)

    assert await _role(first, role) == (True, ["default_transaction_read_only=on"])


async def test_a_downgrade_racing_an_upgrade_leaves_the_upgraded_database_its_role(engines: tuple[AsyncEngine, AsyncEngine, str]) -> None:
    """The downgrading database drops the role it alone used; the database upgrading meanwhile must still end with it."""
    first, second, role = engines
    async with first.begin() as conn:
        for statement in _as(role, MIGRATION.ROLE_UPGRADE):
            await conn.exec_driver_sql(statement)

    await _interleave(first, second, _as(role, MIGRATION.ROLE_DOWNGRADE), _as(role, MIGRATION.ROLE_UPGRADE))

    assert await _role(first, role) == (True, ["default_transaction_read_only=on"])


async def test_a_downgrade_keeps_the_role_while_another_database_holds_a_grant_to_it(engines: tuple[AsyncEngine, AsyncEngine, str]) -> None:
    first, second, role = engines
    async with first.begin() as conn:
        for statement in _as(role, MIGRATION.ROLE_UPGRADE):
            await conn.exec_driver_sql(statement)
    async with second.begin() as conn:
        await conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
    try:
        async with first.begin() as conn:
            for statement in _as(role, MIGRATION.ROLE_DOWNGRADE):
                await conn.exec_driver_sql(statement)
        assert (await _role(first, role))[0], "another database still grants to the role, so it stays"
    finally:
        async with second.begin() as conn:
            await conn.exec_driver_sql(f"REVOKE USAGE ON SCHEMA public FROM {role}")
