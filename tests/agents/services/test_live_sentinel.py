"""Tests for the shared LIVE-sentinel helper (phaze-tvdu1).

``ensure_live_sentinel`` is the one place that creates a LIVE sentinel `ScanBatch`, shared by
the dev seed (``services/agent_bootstrap.py``), the ``phaze agents add`` CLI, and the
``upsert_files`` self-heal path (``routers/agent_files.py``). These tests exercise the helper
directly: idempotency (a second call reuses the first sentinel) and the concurrency guarantee
the partial unique index `uq_scan_batches_agent_id_live` exists to enforce (two concurrent
first-callers for the same agent must not collide, and must agree on exactly one sentinel).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import NullPool, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.services.live_sentinel import ensure_live_sentinel
from tests.conftest import TEST_DATABASE_URL


if TYPE_CHECKING:
    import uuid


_AGENT_ID = "live-sentinel-test-agent"


@pytest.fixture
def _cleanup_committed_rows(async_engine: object) -> object:  # type: ignore[misc]
    """Delete the agent + scan batches this module COMMITS via its own out-of-band engine.

    Mirrors ``tests/agents/cli/test_agents_add.py``'s ``_cleanup_committed_agents``: the
    concurrency test below deliberately bypasses the hermetic ``session`` fixture (real
    concurrent transactions need real, independently-committing connections), so it must clean
    up after itself instead of relying on the per-test rollback the rest of the suite gets for
    free. Depends on ``async_engine`` so the schema is guaranteed to exist.
    """
    yield

    async def _clean() -> None:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(delete(ScanBatch).where(ScanBatch.agent_id == _AGENT_ID))
                await conn.execute(delete(Agent).where(Agent.id == _AGENT_ID))
        finally:
            await engine.dispose()

    asyncio.run(_clean())


@pytest.mark.asyncio
async def test_ensure_live_sentinel_creates_when_missing(session: AsyncSession) -> None:
    session.add(Agent(id=_AGENT_ID, name=_AGENT_ID, scan_roots=["/data/music"]))
    await session.flush()

    sentinel_id = await ensure_live_sentinel(session, _AGENT_ID)

    row = (await session.execute(select(ScanBatch).where(ScanBatch.id == sentinel_id))).scalar_one()
    assert row.agent_id == _AGENT_ID
    assert row.status == ScanStatus.LIVE.value
    assert row.scan_path == "<watcher>"
    assert row.configured_root == "<watcher>"


@pytest.mark.asyncio
async def test_ensure_live_sentinel_second_call_reuses_first(session: AsyncSession) -> None:
    session.add(Agent(id=_AGENT_ID, name=_AGENT_ID, scan_roots=["/data/music"]))
    await session.flush()

    first_id = await ensure_live_sentinel(session, _AGENT_ID)
    second_id = await ensure_live_sentinel(session, _AGENT_ID)

    assert first_id == second_id

    count = (
        await session.execute(select(func.count()).select_from(ScanBatch).where(ScanBatch.agent_id == _AGENT_ID, ScanBatch.status == "live"))
    ).scalar_one()
    assert count == 1, f"expected exactly one LIVE sentinel after two calls, got {count}"


@pytest.mark.asyncio
async def test_ensure_live_sentinel_does_not_disturb_non_live_batches(session: AsyncSession) -> None:
    """A completed/failed batch for the same agent must not block or be replaced by the sentinel."""
    session.add(Agent(id=_AGENT_ID, name=_AGENT_ID, scan_roots=["/data/music"]))
    await session.flush()
    session.add(
        ScanBatch(
            agent_id=_AGENT_ID,
            scan_path="/data/music",
            status="completed",
            total_files=10,
            processed_files=10,
        )
    )
    await session.flush()

    sentinel_id = await ensure_live_sentinel(session, _AGENT_ID)

    rows = (await session.execute(select(ScanBatch).where(ScanBatch.agent_id == _AGENT_ID))).scalars().all()
    statuses = sorted(r.status for r in rows)
    assert statuses == ["completed", "live"]
    live_row = next(r for r in rows if r.status == "live")
    assert live_row.id == sentinel_id


@pytest.mark.asyncio
async def test_ensure_live_sentinel_concurrent_first_calls_create_exactly_one(
    _cleanup_committed_rows: object,
) -> None:
    """Two concurrent first-time callers for the same agent must not collide.

    `ON CONFLICT (agent_id) WHERE status = 'live' DO NOTHING` mirrors the partial unique index
    `uq_scan_batches_agent_id_live` exactly, so Postgres serializes the two racing INSERTs via
    the index's speculative-insertion lock: exactly one wins, the other becomes a no-op, and the
    trailing re-SELECT in both callers returns the SAME row.

    This needs two REAL, independently-committing connections -- the hermetic `session` fixture
    is a single connection wrapped in savepoints that never truly commits, so it cannot exhibit
    the race this test targets. Mirrors the `NullPool` direct-engine pattern used by
    `tests/agents/cli/test_agents_add.py`'s `test_main_success_inserts_and_prints`.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as setup_session:
            setup_session.add(Agent(id=_AGENT_ID, name=_AGENT_ID, scan_roots=["/data/music"]))
            await setup_session.commit()

        async def _call() -> uuid.UUID:
            async with factory() as call_session:
                sentinel_id = await ensure_live_sentinel(call_session, _AGENT_ID)
                await call_session.commit()
                return sentinel_id

        first_id, second_id = await asyncio.gather(_call(), _call())

        assert first_id == second_id, "both concurrent callers must agree on the SAME sentinel id"

        async with factory() as verify_session:
            count = (
                await verify_session.execute(
                    select(func.count()).select_from(ScanBatch).where(ScanBatch.agent_id == _AGENT_ID, ScanBatch.status == "live")
                )
            ).scalar_one()
        assert count == 1, f"expected exactly one committed LIVE sentinel after the race, got {count}"
    finally:
        await engine.dispose()
