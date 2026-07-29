"""phaze-lwqk: execute_tag_write must serialize concurrent writes/undos to the SAME file.

write_file_tags, undo_tag_write, and the bulk loop all funnel through
:func:`phaze.services.tag_writer.execute_tag_write` with no per-file mutual exclusion -- a
double-click, two tabs, or a per-file write racing the bulk loop's write of the same file could
run two concurrent mutagen full-file rewrites on one audio file (a real corruption risk for an
irreplaceable archive). The fix takes a ``pg_advisory_xact_lock`` keyed on the file id inside
``execute_tag_write`` itself.

Like the other advisory-lock regressions in this package, this needs REAL independent Postgres
connections -- the hermetic single-connection ``session``/``client`` fixtures cannot express two
overlapping transactions. See ``test_tag_bulk_write_advisory_lock.py``'s module docstring for the
full rationale.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.tag_writer import execute_tag_write
from tests.db_guard import require_test_database, resolve_test_dsn


pytestmark = pytest.mark.integration

SA_DSN = resolve_test_dsn()


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
    """Two concurrent ``execute_tag_write`` calls for the SAME file must not overlap.

    Request A takes the lock and holds its transaction open (uncommitted); request B, racing it on
    an INDEPENDENT connection, must block at the lock acquire until A commits -- proven with a
    bounded ``asyncio.wait_for`` timeout (a timeout on the FIX means B is correctly still blocked;
    the pre-fix code has no lock at all, so B would complete immediately instead).
    """
    require_test_database(SA_DSN, context="tag-write per-file lock regression")
    engine = create_async_engine(SA_DSN, pool_size=3, max_overflow=0)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as seed_session:
            if await seed_session.get(Agent, "test-fileserver") is None:
                seed_session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
                await seed_session.commit()
            file_id = await _seed_applied_file(seed_session, filename="lock-race.mp3")

        target_a = SimpleNamespace(id=file_id, current_path="/test/music/lock-race.mp3")
        target_b = SimpleNamespace(id=file_id, current_path="/test/music/lock-race.mp3")

        with (
            patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
            patch("phaze.services.tag_writer.write_tags"),
            patch("phaze.services.tag_writer.verify_write", return_value={}),
        ):
            async with session_factory() as session_a, session_factory() as session_b:
                await execute_tag_write(session_a, target_a, {"artist": "From A"}, source="proposal")
                # session_a's transaction is still OPEN (no commit yet) -- the advisory lock is held.

                task_b = asyncio.create_task(execute_tag_write(session_b, target_b, {"artist": "From B"}, source="proposal"))
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task_b), timeout=1.0)

                # Release A's lock; B must now proceed and complete promptly.
                await session_a.commit()
                log_b = await asyncio.wait_for(task_b, timeout=5.0)
                await session_b.commit()

        assert log_b.status == "completed"
        assert log_b.after_tags == {"artist": "From B"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_execute_tag_write_does_not_serialize_different_files(async_engine) -> None:  # type: ignore[no-untyped-def]
    """The per-file lock must be scoped to ONE file -- a concurrent write to a DIFFERENT file must
    not block behind it.
    """
    require_test_database(SA_DSN, context="tag-write per-file lock regression")
    engine = create_async_engine(SA_DSN, pool_size=3, max_overflow=0)
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

        with (
            patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
            patch("phaze.services.tag_writer.write_tags"),
            patch("phaze.services.tag_writer.verify_write", return_value={}),
        ):
            async with session_factory() as session_a, session_factory() as session_b:
                await execute_tag_write(session_a, target_a, {"artist": "From A"}, source="proposal")
                # session_a's transaction stays open -- if the lock were global, B (a DIFFERENT file)
                # would incorrectly block on it too.

                log_b = await asyncio.wait_for(
                    execute_tag_write(session_b, target_b, {"artist": "From B"}, source="proposal"),
                    timeout=2.0,
                )
                await session_a.commit()
                await session_b.commit()

        assert log_b.status == "completed"
    finally:
        await engine.dispose()
