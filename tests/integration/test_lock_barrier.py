"""phaze-lz69g: ``tests/_lock_barrier`` waits on the CONDITION, and every way it fails says it was the barrier (real PG).

The barrier is test infrastructure that three concurrency modules' verdicts rest on, so its own
contract is pinned here rather than inferred from their green runs:

* it observes a contender that connects and queues only AFTER polling has started -- the property the
  single AUTOCOMMIT probe connection exists for (``pg_stat_activity`` is snapshotted per transaction);
* a contender that finishes without queueing, or raises first, fails fast with a ``LOCK BARRIER``
  message rather than waiting out any budget;
* the wedged-contender ceiling is reachable and says it is hang protection;
* ``run_contender_behind_held_lock`` releases the held lock and settles the contender on failure.

Package auto-marked ``integration`` by ``tests/conftest.py``; the explicit ``pytestmark`` is the Plan
37-03 artifact contract.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from phaze.models.agent import Agent
from tests._lock_barrier import LockBarrierError, run_contender_behind_held_lock, wait_for_blocked_waiter


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration

_AGENT_ID = "test-fileserver"


async def _seed_agent(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        session.add(Agent(id=_AGENT_ID, name=_AGENT_ID, kind="fileserver", scan_roots=[]))
        await session.commit()


async def _hold_agent_row(session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    holder = session_factory()
    await holder.execute(select(Agent).where(Agent.id == _AGENT_ID).with_for_update())
    return holder


@contextlib.asynccontextmanager
async def _waiter_in_another_database(engine: AsyncEngine) -> AsyncIterator[None]:
    """Hold an ungranted lock in a SIBLING database for the duration -- another seat's waiter, in miniature.

    ``pg_locks`` is cluster-wide, so this row is visible to the barrier's probe and forces its join to
    scan ``pg_stat_activity`` on the very first poll; ``BLOCKED_WAITER_SQL``'s ``current_database()``
    filter then rejects it. Without it ``pg_locks`` has no ungranted row until the contender queues, the
    join never scans ``pg_stat_activity`` early, and a stale per-transaction snapshot cannot show itself
    -- which is why a quiet machine could not catch the regression this exists to discriminate.
    """
    own_db = engine.url.database
    assert own_db is not None
    foreign_db = f"{own_db}_lockbarrier_foreign"
    foreign_url = engine.url.set(database=foreign_db)
    async with engine.connect() as admin_raw:
        admin = await admin_raw.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f'DROP DATABASE IF EXISTS "{foreign_db}" WITH (FORCE)'))
        await admin.execute(text(f'CREATE DATABASE "{foreign_db}"'))
        foreign = create_async_engine(foreign_url, poolclass=NullPool)
        try:
            async with foreign.connect() as holder_raw, foreign.connect() as waiter_raw:
                holder = await holder_raw.execution_options(isolation_level="AUTOCOMMIT")
                waiter = await waiter_raw.execution_options(isolation_level="AUTOCOMMIT")
                await holder.execute(text("SELECT pg_advisory_lock(7302)"))
                blocked = asyncio.create_task(waiter.execute(text("SELECT pg_advisory_lock(7302)")))
                while not (
                    await holder.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted AND database = (SELECT oid FROM pg_database WHERE datname = current_database()))"
                        )
                    )
                ).scalar():
                    assert not blocked.done(), "the sibling-database waiter never queued"
                    await asyncio.sleep(0.02)
                try:
                    yield
                finally:
                    await holder.execute(text("SELECT pg_advisory_unlock(7302)"))
                    await blocked
                    await waiter.execute(text("SELECT pg_advisory_unlock(7302)"))
        finally:
            await foreign.dispose()
            await admin.execute(text(f'DROP DATABASE IF EXISTS "{foreign_db}" WITH (FORCE)'))


async def test_observes_a_contender_that_queues_only_after_polling_has_started(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A contender that CONNECTS after the first poll must still be seen, while another database has a waiter.

    ``pg_stat_activity`` is snapshotted per TRANSACTION. A probe that held one transaction across polls
    would, once anything forced an early scan of it, keep a snapshot that predates the contender's
    backend, and the join in ``BLOCKED_WAITER_SQL`` would never match again. Multi-seat load is exactly
    what forces that early scan -- a sibling seat's waiter -- so it is reproduced here. The small ceiling
    turns that regression into a prompt ``LockBarrierError`` instead of a 300 s wait. Verified
    discriminating: with the probe's AUTOCOMMIT removed this test fails at the ceiling.
    """
    engine, session_factory = committed_db
    await _seed_agent(session_factory)

    async def _late_contender() -> str:
        await asyncio.sleep(0.5)  # ~25 polls happen before this backend exists
        async with session_factory() as session:
            await session.execute(update(Agent).where(Agent.id == _AGENT_ID).values(name="renamed"))
            await session.commit()
        return "done"

    async with _waiter_in_another_database(engine):
        holder = await _hold_agent_row(session_factory)
        result = await run_contender_behind_held_lock(session_factory, holder, _late_contender(), release=holder.commit, ceiling=30)
    assert result == "done"


async def test_a_contender_that_never_blocks_fails_fast_as_a_barrier_failure(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The regression shape of a lock-serialization test: the contender completes without contending."""
    _engine, session_factory = committed_db
    await _seed_agent(session_factory)
    holder = await _hold_agent_row(session_factory)

    async def _never_contends() -> str:
        return "finished"

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(LockBarrierError, match=r"^LOCK BARRIER: the contender finished without ever being observed") as info:
        await run_contender_behind_held_lock(session_factory, holder, _never_contends(), release=holder.commit)
    assert "it returned 'finished'" in str(info.value)
    assert "not the race assertion under test" in str(info.value)
    assert loop.time() - started < 30, "a contender that finished must be reported at once, not after a budget"


async def test_a_contender_that_raises_is_reported_with_its_own_exception(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _engine, session_factory = committed_db
    await _seed_agent(session_factory)
    holder = await _hold_agent_row(session_factory)

    async def _explodes() -> None:
        raise RuntimeError("contender blew up before reaching the lock")

    with pytest.raises(LockBarrierError, match=r"RuntimeError\('contender blew up before reaching the lock'\)"):
        await run_contender_behind_held_lock(session_factory, holder, _explodes(), release=holder.commit)


async def test_the_wedged_contender_ceiling_says_it_is_hang_protection(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _engine, session_factory = committed_db
    contender = asyncio.create_task(asyncio.sleep(60))
    try:
        with pytest.raises(LockBarrierError, match=r"^LOCK BARRIER: after 0s the contender is still running") as info:
            await wait_for_blocked_waiter(session_factory, contender, ceiling=0.2)
        assert "hang protection" in str(info.value)
    finally:
        contender.cancel()
        await asyncio.gather(contender, return_exceptions=True)


async def test_a_barrier_failure_releases_the_held_lock_and_settles_the_contender(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """On failure the holder is rolled back (lock released) and the contender task is not left pending."""
    _engine, session_factory = committed_db
    await _seed_agent(session_factory)
    holder = await _hold_agent_row(session_factory)
    tasks_before = asyncio.all_tasks()

    async def _never_contends() -> None:
        return None

    with pytest.raises(LockBarrierError):
        await run_contender_behind_held_lock(session_factory, holder, _never_contends(), release=holder.commit)

    assert not (asyncio.all_tasks() - tasks_before), "the contender task was left pending"
    # The row lock is gone: a NOWAIT lock attempt from a fresh connection succeeds immediately.
    async with session_factory() as session:
        await session.execute(select(Agent).where(Agent.id == _AGENT_ID).with_for_update(nowait=True))
