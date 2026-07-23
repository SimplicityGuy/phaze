"""phaze-q1ow: delete_scan_cascade must not FK-violate against an in-flight pipeline writer (real PG).

The hermetic single-connection ``create_savepoint`` ``session`` fixture (92-03) cannot express this: it
binds every session in a test to ONE outer-transaction connection, so a "concurrent" child-row insert on
that same connection is not a real race -- there is no second, independent transaction whose commit can
land between the cascade's child-delete steps and its ``DELETE FROM files``. This lives on the real-PG
``committed_db`` fixture (92-04) where the cascade and the "still-running pipeline worker" each open
their OWN pool connection and race for real.

The scenario mirrors the bug report exactly: a worker session has ALREADY sent (flushed, not yet
committed) an INSERT of a new ``fingerprint_results`` row for one of the batch's files -- an in-flight
insert that, per Postgres FK enforcement, holds an implicit ``FOR KEY SHARE`` lock on the referenced file
row for the lifetime of the worker's transaction. ``delete_scan_cascade`` is launched concurrently on its
own connection.

* Pre-fix (no upfront lock): the cascade runs its first 14 child-delete steps unobstructed (none of them
  touch the file row itself), then blocks at its OWN LAST statement -- ``DELETE FROM files`` -- which
  needs a lock incompatible with the worker's held ``FOR KEY SHARE``. When the worker finally commits,
  the blocked ``DELETE FROM files`` wakes up, Postgres re-validates the FK and discovers the row the
  worker just committed, and raises ``ForeignKeyViolation`` -- the whole cascade aborts. This is the exact
  failure scenario from the bug report ("cascade step 15's DELETE FROM files hits the FK from that new
  row").

* Fixed (this test's expectation): the cascade's FIRST statement is ``SELECT ... FOR UPDATE`` on the
  batch's file rows -- incompatible with the worker's ``FOR KEY SHARE`` -- so the cascade blocks
  IMMEDIATELY, before running any child-delete step. Once the worker commits its insert, the cascade's
  lock acquisition succeeds and it proceeds through all 16 steps INCLUDING the ``fingerprint_results``
  delete, which now runs AFTER the worker's row was committed and therefore sweeps it up along with
  everything else -- the whole cascade commits cleanly, with zero ForeignKeyViolation.

A blocked waiter is observed via ``pg_locks`` (deterministic), not guessed via a sleep duration -- the
same style as ``test_scan_reaper_concurrency.py`` / ``test_stage_pause_resume_lock.py``.

Run with real PG via ``just integration-test``. Package auto-marked ``integration`` by
``tests/conftest.py``; the explicit ``pytestmark`` is the Plan 37-03 artifact contract.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.services.scan_deletion import delete_scan_cascade


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


pytestmark = pytest.mark.integration


async def _seed_agent(session: AsyncSession) -> None:
    """Seed the ``test-fileserver`` FK-parent agent (``committed_db`` never seeds it for us at setup)."""
    session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
    await session.commit()


async def _seed_batch_with_one_file(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one COMPLETED ScanBatch with one FileRecord; return (batch_id, file_id), committed."""
    batch_id = uuid.uuid4()
    session.add(
        ScanBatch(
            id=batch_id,
            agent_id="test-fileserver",
            scan_path="/data/music",
            status=ScanStatus.COMPLETED.value,
            total_files=1,
            processed_files=1,
        )
    )
    await session.flush()
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id="test-fileserver",
            batch_id=batch_id,
            sha256_hash="a" * 64,
            original_path=f"/data/music/{file_id}.flac",
            original_filename=f"{file_id}.flac",
            current_path=f"/data/music/{file_id}.flac",
            file_type="flac",
            file_size=4096,
        )
    )
    await session.commit()
    return batch_id, file_id


async def _wait_for_blocked_waiter(session_factory: async_sessionmaker[AsyncSession], *, timeout: float = 5.0) -> None:
    """Poll ``pg_locks`` until some OTHER backend is genuinely queued waiting on a lock.

    Mirrors ``test_scan_reaper_concurrency.py``'s helper: proves the cascade's own lock acquisition has
    reached Postgres and is blocked behind the worker's held ``FOR KEY SHARE`` -- deterministic, not a
    guessed sleep duration.
    """

    async def _poll() -> None:
        while True:
            async with session_factory() as probe:
                waiting = (await probe.execute(text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted)"))).scalar()
            if waiting:
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def test_cascade_blocks_on_in_flight_worker_write_then_sweeps_it_instead_of_fk_violating(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A worker's uncommitted-but-flushed child insert must serialize the cascade, not FK-violate it.

    Reproduces the bug's own root mechanism: a still-running pipeline worker has already sent (but not
    yet committed) an INSERT of a new ``fingerprint_results`` row for one of the batch's files -- which,
    per Postgres FK enforcement, holds an implicit ``FOR KEY SHARE`` lock on the file row for the
    lifetime of the worker's open transaction. ``delete_scan_cascade`` must block on that lock (via its
    own upfront ``FOR UPDATE``) rather than racing past it and discovering the conflict only at its own
    final ``DELETE FROM files`` step.
    """
    _engine, session_factory = committed_db

    async with session_factory() as seed_session:
        await _seed_agent(seed_session)
        batch_id, file_id = await _seed_batch_with_one_file(seed_session)

    # The "in-flight pipeline writer": an uncommitted transaction that has ALREADY flushed (sent, not
    # yet committed) an INSERT referencing the batch's file -- exactly the fingerprint-result write the
    # bug describes landing mid-cascade. The flush alone is enough to acquire the implicit FOR KEY SHARE
    # lock; committing only happens once the cascade is observed blocked on it below.
    holder_session = session_factory()
    holder_session.add(FingerprintResult(id=uuid.uuid4(), file_id=file_id, engine="chromaprint", status="completed"))
    await holder_session.flush()

    async def _release_after_waiter() -> None:
        await _wait_for_blocked_waiter(session_factory)
        await holder_session.commit()

    async def _run_cascade() -> dict[str, int]:
        async with session_factory() as cascade_session:
            counts = await delete_scan_cascade(cascade_session, batch_id)
            await cascade_session.commit()
            return counts

    try:
        _release_result, counts = await asyncio.gather(_release_after_waiter(), _run_cascade())
    finally:
        await holder_session.close()

    # The cascade's OWN fingerprint_results delete step -- which runs AFTER the lock is granted, i.e.
    # AFTER the worker's row is committed -- sweeps it up too: the row is never orphaned, and the
    # files DELETE never sees a dangling reference.
    assert counts["fingerprint_results"] == 1

    async with session_factory() as session:
        assert await session.get(ScanBatch, batch_id) is None
        assert await session.get(FileRecord, file_id) is None
        remaining = (await session.execute(select(FingerprintResult).where(FingerprintResult.file_id == file_id))).scalars().all()
        assert remaining == []
