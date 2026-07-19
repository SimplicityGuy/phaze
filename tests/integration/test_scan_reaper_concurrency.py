"""phaze-5dfj: the stall reaper must never clobber a concurrently-completed/progressed scan (real PG).

The hermetic single-connection ``create_savepoint`` ``session`` fixture (92-03) cannot express this: it
binds every session in a test to ONE outer-transaction connection, so a "concurrent" write on that same
connection is not a real race -- there is no second transaction whose COMMIT can land in the window
between the reaper's read and its write. This lives on the real-PG ``committed_db`` fixture (92-04)
where the reaper and the "owning agent" each open their OWN pool connection and race for real.

Connection A (``_completer``) holds ``SELECT ... FOR UPDATE`` on the stale RUNNING batch from the START,
simulating the owning agent's in-flight terminal ``patch_scan_batch`` transaction. The reaper is launched
concurrently (``asyncio.gather``): its own write to the SAME row -- whether the pre-fix blind
``UPDATE ... WHERE id = :id`` or the fixed guarded ``UPDATE ... WHERE status='running' AND
heartbeat<cutoff`` -- needs that row's write lock and therefore BLOCKS until A commits. A does not
guess a sleep duration to land in this window; it polls ``pg_locks`` for a genuine queued waiter on the
``scan_batches`` relation, so the ordering is deterministic rather than timing-dependent. Once a waiter
is observed, A finalizes: flips the row to COMPLETED with a fresh heartbeat and commits, releasing the
lock. The reaper's blocked write then resumes, and its outcome is exactly what distinguishes the two
implementations:

  * pre-fix: the blind UPDATE has no status/heartbeat guard, so it proceeds unconditionally and
    clobbers the just-committed COMPLETED row back to FAILED -- a lost update.
  * fixed (this test's expectation): the guarded UPDATE re-evaluates its WHERE clause against A's
    newly committed row (PostgreSQL EvalPlanQual) -- no longer matches, so 0 rows are affected and the
    COMPLETED row survives untouched.

Run with real PG via ``just integration-test``. Package auto-marked ``integration`` by
``tests/conftest.py``; the explicit ``pytestmark`` is the Plan 37-03 artifact contract.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.tasks.scan_reaper import reap_stalled_scans


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


pytestmark = pytest.mark.integration


class _StubCfg:
    """Minimal stand-in for the settings object the reaper reads (mirrors the hermetic suite's helper)."""

    def __init__(self, scan_stall_seconds: int) -> None:
        self.scan_stall_seconds = scan_stall_seconds


async def _seed_agent(session: AsyncSession) -> None:
    """Seed the ``test-fileserver`` FK-parent agent (committed_db never seeds it for us at setup)."""
    session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
    await session.commit()


async def _seed_stale_running_batch(session: AsyncSession, *, last_progress_at: datetime) -> uuid.UUID:
    """Seed a RUNNING ScanBatch whose heartbeat already predates the reaper's cutoff; return its id."""
    batch_id = uuid.uuid4()
    session.add(
        ScanBatch(
            id=batch_id,
            agent_id="test-fileserver",
            scan_path="/music/scan",
            status=ScanStatus.RUNNING.value,
            total_files=0,
            processed_files=0,
            last_progress_at=last_progress_at,
        )
    )
    await session.commit()
    return batch_id


async def _wait_for_blocked_waiter(session_factory: async_sessionmaker[AsyncSession], *, timeout: float = 5.0) -> None:
    """Poll ``pg_locks`` until some OTHER backend is genuinely queued waiting on a lock.

    Proves the reaper's write has reached Postgres and is blocked behind the completer's held row lock --
    deterministic, not a guessed sleep-and-hope duration. A backend blocked on an in-use ROW (as opposed
    to the whole relation) waits on a ``transactionid`` lock keyed to the lock-holder's xid -- NOT a
    ``relation``-scoped lock row -- so this deliberately does not filter by relation; on this dedicated,
    single-purpose ephemeral test database the only contender in this window is the reaper's write.
    """

    async def _poll() -> None:
        while True:
            async with session_factory() as probe:
                waiting = (await probe.execute(text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted)"))).scalar()
            if waiting:
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def test_lost_update_race_does_not_clobber_concurrently_completed_batch(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-5dfj: a batch that completes between the reaper's read and its write must survive as COMPLETED."""
    engine, session_factory = committed_db
    monkeypatch.setattr("phaze.tasks.scan_reaper.get_settings", lambda: _StubCfg(scan_stall_seconds=600))

    async with session_factory() as seed_session:
        await _seed_agent(seed_session)
        batch_id = await _seed_stale_running_batch(seed_session, last_progress_at=datetime.now(UTC) - timedelta(seconds=700))

    reaper_ctx = {"async_session": async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)}

    # Hold the row's write lock BEFORE the reaper is even started (awaited synchronously, outside the
    # gather below) -- mirrors the owning agent's open terminal PATCH transaction. Starting the reaper
    # only after this completes fully removes any ambiguity about which side reaches the row first;
    # the reaper's own write is GUARANTEED to queue behind this already-held lock.
    completer_session = session_factory()
    await completer_session.execute(select(ScanBatch).where(ScanBatch.id == batch_id).with_for_update())

    async def _finalize_completer() -> None:
        await _wait_for_blocked_waiter(session_factory)
        batch = await completer_session.get(ScanBatch, batch_id)
        assert batch is not None
        batch.status = ScanStatus.COMPLETED.value
        batch.total_files = 42
        batch.processed_files = 42
        batch.completed_at = datetime.now(UTC)
        batch.last_progress_at = datetime.now(UTC)
        await completer_session.commit()

    try:
        _finalize_result, reaper_result = await asyncio.gather(_finalize_completer(), reap_stalled_scans(reaper_ctx))
    finally:
        await completer_session.close()

    async with session_factory() as session:
        final = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()

    assert final.status == ScanStatus.COMPLETED.value, (
        f"lost update: a batch that completed while the reaper was mid-flight must survive as COMPLETED, not {final.status!r}"
    )
    assert final.total_files == 42
    assert final.error_message is None
    assert reaper_result == {"reaped": 0}, "the reaper must not count a batch it lost the race on as reaped"
