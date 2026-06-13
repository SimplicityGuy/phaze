"""Shared real-Postgres harness for the Phase 37 per-stage control-plane integration tests.

The four ``test_stage_*.py`` modules all need the same scaffolding:

* a real :class:`saq.queue.postgres.PostgresQueue` built through the production
  :func:`phaze.tasks._shared.queue_factory.build_pipeline_queue` seam (so the
  ``apply_stage_control`` before-enqueue hook is wired EXACTLY as it is in prod and
  stamps each new stage job with its live priority);
* the ``saq_jobs`` table SAQ auto-creates on ``connect()`` -> ``init_db()``;
* a seeded ``pipeline_stage_control`` table the hook reads through the queue's psycopg3
  pool (created here idempotently so the harness does not depend on migration 020 having
  been applied to the ephemeral broker DB);
* a SQLAlchemy ``AsyncSession`` bound to the SAME database, because the service helpers
  under test (:mod:`phaze.services.stage_control`) issue their raw ``saq_jobs`` UPDATEs
  through an ``AsyncSession`` and expect a caller-owned transaction.

Phase 36's ``test_pg_*`` modules duplicated their ``pg_queue`` fixture per file; the
Phase 37 suite shares one fixture here instead (idiomatic pytest, one harness to maintain).
The whole ``tests/integration/`` package is auto-marked ``integration`` by
``tests/conftest.py`` (path rule), and every Phase 37 file ALSO declares an explicit
``pytestmark = pytest.mark.integration`` (belt-and-suspenders, and the documented artifact
contract for Plan 37-03).

Connectivity is probed first; if Postgres is not up the fixture ``pytest.skip``s, so a bare
``uv run pytest`` (no ``just test-db``) skips rather than errors. Run the suite with real PG
via ``just integration-test`` (ephemeral Postgres + Redis on host ports 5433 / 6380).
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.tasks._shared import stage_control as stage_control_module
from phaze.tasks._shared.queue_factory import build_pipeline_queue


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from saq.queue.postgres import PostgresQueue


# Raw libpq broker DSN (NOT the ``+asyncpg`` dialect form psycopg3 cannot parse). Derived
# the same way the Phase 36 live-broker tests derive it: prefer PHAZE_QUEUE_URL, else the
# integration harness' TEST_DATABASE_URL with the SQLAlchemy dialect suffix stripped.
BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
# SQLAlchemy async (asyncpg) DSN for the service-helper session, pointing at the SAME DB as
# the broker. ``"postgresql+asyncpg://"`` does not contain the ``"postgresql://"`` substring,
# so the replace is a no-op when TEST_DATABASE_URL is already in dialect form.
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")
# Cache-handle Redis DSN the factory attaches; the enqueued-counter hook is best-effort, so
# this never blocks an enqueue even when Redis is down.
CACHE_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")


# Idempotent mirror of migration 020 -- only the columns the hook reads (paused, priority)
# plus the range CHECK. CREATE ... IF NOT EXISTS is a no-op when the real migrated table is
# already present, so the harness works whether or not migrations ran on the broker DB.
_CONTROL_DDL = text(
    """
    CREATE TABLE IF NOT EXISTS pipeline_stage_control (
        stage VARCHAR(32) PRIMARY KEY,
        paused BOOLEAN NOT NULL DEFAULT false,
        priority SMALLINT NOT NULL DEFAULT 50,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_pipeline_stage_control_priority_range CHECK (priority BETWEEN 0 AND 100)
    )
    """
)
# Seed (or reset) the three agent stages to the unpaused / priority-50 baseline so the hook
# stamps a deterministic priority and parks nothing at enqueue. ON CONFLICT makes it
# idempotent across reruns and against a pre-seeded migrated table.
_SEED_CONTROL_SQL = text(
    """
    INSERT INTO pipeline_stage_control (stage, paused, priority)
    VALUES ('metadata', false, 50), ('analyze', false, 50), ('fingerprint', false, 50)
    ON CONFLICT (stage) DO UPDATE SET paused = EXCLUDED.paused, priority = EXCLUDED.priority, updated_at = now()
    """
)


def _reset_hook_cache() -> None:
    """Drop the ``apply_stage_control`` module-level TTL cache so the next enqueue reads fresh.

    The hook caches ``(paused, priority)`` per stage for 5s in module globals; across tests
    in one process a stale window could otherwise serve a prior test's seeded priority. Each
    test seeds its own control rows, so we clear the cache at setup and teardown.
    """
    stage_control_module._cache.clear()
    stage_control_module._cache_expires_at = 0.0


@pytest_asyncio.fixture
async def stage_env() -> AsyncGenerator[tuple[PostgresQueue, async_sessionmaker[AsyncSession]]]:
    """Yield ``(queue, session_factory)`` against a real Postgres ``saq_jobs`` broker.

    ``queue`` is a connected :class:`PostgresQueue` built via ``build_pipeline_queue`` (hooks
    wired as in prod) with a per-test-unique name for row isolation. ``session_factory`` makes
    SQLAlchemy ``AsyncSession``s bound to the same DB for the service-helper UPDATEs. Setup
    seeds ``pipeline_stage_control`` (3 unpaused/priority-50 rows) and resets the hook cache;
    teardown deletes this queue's ``saq_jobs`` rows, disconnects the pool, disposes the engine,
    and clears the hook cache again.
    """
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    import uuid

    queue = build_pipeline_queue(f"itest-stage-{uuid.uuid4().hex[:8]}", BROKER_DSN, cache_redis_url=CACHE_REDIS_URL)
    await queue.connect()  # opens the psycopg3 pool + init_db() (creates saq_jobs)

    engine = create_async_engine(SA_DSN)
    async with engine.begin() as conn:
        await conn.execute(_CONTROL_DDL)
        await conn.execute(_SEED_CONTROL_SQL)
    _reset_hook_cache()

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield queue, session_factory
    finally:
        with contextlib.suppress(Exception):
            async with queue.pool.connection() as conn:
                # queue.name is bound, never interpolated.
                await conn.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue.name,))
        await queue.disconnect()
        await engine.dispose()
        _reset_hook_cache()
