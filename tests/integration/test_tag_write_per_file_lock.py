"""phaze-lwqk: execute_tag_write must serialize concurrent writes/undos to the SAME file.

write_file_tags, undo_tag_write, and the bulk loop all funnel through
:func:`phaze.services.tag_writer.execute_tag_write` with no per-file mutual exclusion -- a
double-click, two tabs, or a per-file write racing the bulk loop's write of the same file could
run two concurrent mutagen full-file rewrites on one audio file (a real corruption risk for an
irreplaceable archive). The fix takes a session-scoped advisory lock (``pg_advisory_lock`` /
``pg_advisory_unlock``) keyed on the file id, held on a connection DEDICATED for the whole
``execute_tag_write`` call (phaze-ysnp's write-ahead commit happens mid-call, so an xact-scoped
lock on the caller's own session would release too early -- see
``_dedicated_lock_connection``'s docstring).

Because the lock now wraps the ENTIRE call (including its own internal write-ahead commit) and is
released again before the function returns, "hold the caller's transaction open" no longer
expresses contention (phaze-ysnp: there is no caller-visible open transaction to hold -- the
function commits and completes its own lock cycle internally). Contention is proven instead by
gating the disk-write phase itself with a real ``threading.Event`` (``write_tags`` runs inside
``asyncio.to_thread``, a worker OS thread, so an ``asyncio.Event`` cannot pause it): request A
blocks mid-write holding the lock; request B, racing it on an INDEPENDENT connection, must stay
blocked at the lock acquire until A's gate is released and A completes.

Like the other advisory-lock regressions in this package, this needs REAL independent Postgres
connections -- the hermetic single-connection ``session``/``client`` fixtures cannot express two
overlapping transactions. See ``test_tag_bulk_write_advisory_lock.py``'s module docstring for the
full rationale.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog
from phaze.services.tag_writer import execute_tag_write
from tests.db_guard import require_test_database, resolve_test_dsn


pytestmark = pytest.mark.integration

SA_DSN = resolve_test_dsn()


async def _delete_seeded_files(engine, file_ids: list[uuid.UUID]) -> None:  # type: ignore[no-untyped-def]
    """Delete seeded ``FileRecord``s (and their children) for real, in FK-safe order.

    This module's tests commit through a REAL engine, independent of the suite's hermetic
    per-test savepoint sandbox (see the module docstring for why). A row left behind here
    persists in the shared test database for the rest of the pytest session and pollutes any
    LATER test that counts rows globally (e.g. ``_get_tag_stats``'s ``TagWriteLog`` tallies).
    """
    if not file_ids:
        return
    async with engine.begin() as conn:
        await conn.execute(delete(TagWriteLog).where(TagWriteLog.file_id.in_(file_ids)))
        await conn.execute(delete(RenameProposal).where(RenameProposal.file_id.in_(file_ids)))
        await conn.execute(delete(FileRecord).where(FileRecord.id.in_(file_ids)))


async def _seed_applied_file(session: AsyncSession, *, filename: str) -> uuid.UUID:
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/test/music/{uuid.uuid4().hex}/{filename}",
            original_filename=filename,
            current_path=f"/test/music/{filename}",
            file_type="mp3",
            file_size=1024,
        )
    )
    session.add(RenameProposal(id=uuid.uuid4(), file_id=file_id, proposed_filename=filename, status=ProposalStatus.EXECUTED.value))
    await session.commit()
    return file_id


@pytest.mark.asyncio
async def test_execute_tag_write_serializes_concurrent_writes_to_the_same_file(async_engine) -> None:  # type: ignore[no-untyped-def]
    """Two concurrent ``execute_tag_write`` calls for the SAME file must not overlap on disk.

    Request A is gated mid-write (holding the per-file lock); request B, racing it on an
    INDEPENDENT connection, must stay blocked at the lock acquire until A's gate is released and A
    completes -- proven with a bounded ``asyncio.wait_for`` timeout (a timeout on the FIX means B
    is correctly still blocked; the pre-fix code has no lock at all, so B would complete
    immediately instead, interleaving its write with A's).
    """
    require_test_database(SA_DSN, context="tag-write per-file lock regression")
    engine = create_async_engine(SA_DSN, pool_size=4, max_overflow=0)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as seed_session:
            if await seed_session.get(Agent, "test-fileserver") is None:
                seed_session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
                await seed_session.commit()
            file_id = await _seed_applied_file(seed_session, filename="lock-race.mp3")

        target_a = SimpleNamespace(id=file_id, current_path="/test/music/lock-race.mp3")
        target_b = SimpleNamespace(id=file_id, current_path="/test/music/lock-race.mp3")

        gate_reached = threading.Event()
        gate_release = threading.Event()
        call_count = {"n": 0}
        call_lock = threading.Lock()

        def _gated_write_tags(_file_path: str, _tags: dict) -> None:
            # Gate ONLY the FIRST call (whichever request reaches it first -- A, if the lock is
            # doing its job). A second, genuinely-concurrent call (the lock NOT serializing, the
            # pre-fix shape) must NOT also block here, or the test would time out for the wrong
            # reason (both requests waiting on this same gate) regardless of whether the lock works.
            with call_lock:
                call_count["n"] += 1
                is_first = call_count["n"] == 1
            if is_first:
                gate_reached.set()
                gate_release.wait(timeout=10.0)

        with (
            patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
            patch("phaze.services.tag_writer.write_tags", side_effect=_gated_write_tags),
            patch("phaze.services.tag_writer.verify_write", return_value={}),
        ):
            async with session_factory() as session_a, session_factory() as session_b:
                task_a = asyncio.create_task(execute_tag_write(session_a, target_a, {"artist": "From A"}, source="proposal"))
                # Wait (off-loop) for A to be parked inside its gated write_tags -- it now holds the
                # per-file lock on its dedicated connection.
                await asyncio.to_thread(gate_reached.wait, 5.0)
                assert gate_reached.is_set(), "request A never reached its gated write -- test setup is broken"

                task_b = asyncio.create_task(execute_tag_write(session_b, target_b, {"artist": "From B"}, source="proposal"))
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task_b), timeout=1.0)

                # Release A: it finishes its write, releases the lock; B can now proceed.
                gate_release.set()
                log_a = await asyncio.wait_for(task_a, timeout=5.0)
                log_b = await asyncio.wait_for(task_b, timeout=5.0)
                await session_a.commit()
                await session_b.commit()

        assert log_a.status == "completed"
        assert log_b.status == "completed"
        assert log_b.after_tags == {"artist": "From B"}
    finally:
        await _delete_seeded_files(engine, [file_id])
        await engine.dispose()


@pytest.mark.asyncio
async def test_execute_tag_write_does_not_serialize_different_files(async_engine) -> None:  # type: ignore[no-untyped-def]
    """The per-file lock must be scoped to ONE file -- a concurrent write to a DIFFERENT file must
    not block behind it, even while the first is gated mid-write.
    """
    require_test_database(SA_DSN, context="tag-write per-file lock regression")
    engine = create_async_engine(SA_DSN, pool_size=4, max_overflow=0)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as seed_session:
            if await seed_session.get(Agent, "test-fileserver") is None:
                seed_session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
                await seed_session.commit()
            file_id_a = await _seed_applied_file(seed_session, filename="lock-independent-a.mp3")
            file_id_b = await _seed_applied_file(seed_session, filename="lock-independent-b.mp3")

        target_a = SimpleNamespace(id=file_id_a, current_path="/test/music/lock-independent-a.mp3")
        target_b = SimpleNamespace(id=file_id_b, current_path="/test/music/lock-independent-b.mp3")

        gate_reached = threading.Event()
        gate_release = threading.Event()

        def _gated_write_tags(file_path: str, _tags: dict) -> None:
            if file_path == target_a.current_path:
                gate_reached.set()
                gate_release.wait(timeout=10.0)

        with (
            patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
            patch("phaze.services.tag_writer.write_tags", side_effect=_gated_write_tags),
            patch("phaze.services.tag_writer.verify_write", return_value={}),
        ):
            async with session_factory() as session_a, session_factory() as session_b:
                task_a = asyncio.create_task(execute_tag_write(session_a, target_a, {"artist": "From A"}, source="proposal"))
                await asyncio.to_thread(gate_reached.wait, 5.0)
                assert gate_reached.is_set(), "request A never reached its gated write -- test setup is broken"

                # A DIFFERENT file must proceed without waiting for A's gate.
                log_b = await asyncio.wait_for(
                    execute_tag_write(session_b, target_b, {"artist": "From B"}, source="proposal"),
                    timeout=2.0,
                )
                await session_b.commit()

                gate_release.set()
                log_a = await asyncio.wait_for(task_a, timeout=5.0)
                await session_a.commit()

        assert log_a.status == "completed"
        assert log_b.status == "completed"
    finally:
        await _delete_seeded_files(engine, [file_id_a, file_id_b])
        await engine.dispose()
