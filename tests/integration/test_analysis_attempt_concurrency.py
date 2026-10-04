"""Actual broker winners, stale acknowledgements, and tiny ledger pools agree."""

import asyncio
from datetime import datetime
from typing import Any
import uuid

import pytest
from saq import Status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.scheduling_ledger import mark_analysis_attempt_terminal
from phaze.tasks._shared.attempt_context import ATTEMPT_META_KEY, ledger_enqueue_session
from phaze.tasks.recovery_backfill import backfill_ledger_from_saq_jobs


@pytest.mark.parametrize("pool_size", [1, 2])
async def test_concurrent_producers_keep_winning_attempt_and_release_on_failure(
    committed_db: Any, stage_env: Any, pool_size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-key serialization must not deadlock by borrowing a second ledger connection."""
    _, default_maker = committed_db
    queue, _ = stage_env
    engine = create_async_engine(default_maker.kw["bind"].url, pool_size=pool_size, max_overflow=0, pool_timeout=1)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    queue.ledger_sessionmaker = maker
    broker_insert = queue._enqueue

    async def assert_ledger_visible_before_broker_insert(candidate: Any) -> Any:
        # Independent connection models an immediate control callback after broker visibility.
        async with default_maker() as observer:
            ledger = await observer.get(SchedulingLedger, candidate.key)
            assert ledger is not None, "ledger must commit before broker insertion"
            assert ledger.enqueued_at == datetime.fromisoformat(candidate.meta[ATTEMPT_META_KEY])
        return await broker_insert(candidate)

    monkeypatch.setattr(queue, "_enqueue", assert_ledger_visible_before_broker_insert)
    file_ids = [str(uuid.uuid4()) for _ in range(3)]
    try:
        # Four simultaneous producers: same-key dedup and independent-key progress.
        jobs = await asyncio.wait_for(
            asyncio.gather(*(queue.enqueue("process_file", file_id=fid) for fid in [file_ids[0], file_ids[0], *file_ids[1:]])), 10
        )
        assert sum(job is not None for job in jobs) == 3
        key = f"process_file:{file_ids[0]}"
        winning = await queue.job(key)
        assert winning is not None
        original_epoch = datetime.fromisoformat(winning.meta[ATTEMPT_META_KEY])
        async with maker() as session:
            row = await session.get(SchedulingLedger, key)
            assert row is not None and row.enqueued_at == original_epoch
            await mark_analysis_attempt_terminal(session, key, original_epoch)
            await session.commit()
        # Sequential dedup must preserve both winning epoch and terminal acknowledgement.
        assert await queue.enqueue("process_file", file_id=file_ids[0]) is None
        async with maker() as session:
            row = await session.get(SchedulingLedger, key)
            assert row is not None and row.enqueued_at == original_epoch and row.terminal_at is not None
        # A genuine new enqueue resets the outcome, and a late ACK cannot terminate it.
        await queue.finish(winning, Status.FAILED, error="synthetic failure")
        new_job = await queue.enqueue("process_file", file_id=file_ids[0])
        assert new_job is not None
        new_epoch = datetime.fromisoformat(new_job.meta[ATTEMPT_META_KEY])
        assert new_epoch > original_epoch
        async with maker() as session:
            await mark_analysis_attempt_terminal(session, key, original_epoch)
            await session.commit()
            row = await session.get(SchedulingLedger, key)
            assert row is not None and row.enqueued_at == new_epoch and row.terminal_at is None
            # Startup reconstruction must retain the actual broker identity after ledger loss.
            await session.execute(delete(SchedulingLedger).where(SchedulingLedger.key == key))
            await session.commit()
            await backfill_ledger_from_saq_jobs(session)
            await session.commit()
            session.expire_all()
            row = await session.get(SchedulingLedger, key)
            assert row is not None and row.enqueued_at == new_epoch
        original_enqueue = queue._enqueue

        async def fail_insert(_job: Any) -> Any:
            raise RuntimeError("synthetic broker insert failure")

        monkeypatch.setattr(queue, "_enqueue", fail_insert)
        failing_id = str(uuid.uuid4())
        with pytest.raises(RuntimeError, match="synthetic broker insert failure"):
            await queue.enqueue("process_file", file_id=failing_id)
        assert ledger_enqueue_session.get() is None
        monkeypatch.setattr(queue, "_enqueue", original_enqueue)
        assert await asyncio.wait_for(queue.enqueue("process_file", file_id=failing_id), 5) is not None
        async with maker() as session:
            assert len((await session.scalars(select(SchedulingLedger))).all()) == 4
    finally:
        await engine.dispose()


@pytest.mark.parametrize("pool_size", [1, 2])
@pytest.mark.parametrize("broker_fails", [False, True])
async def test_unlock_failure_invalidates_real_connection_and_preserves_broker_error(
    committed_db: Any, stage_env: Any, pool_size: int, broker_fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uncertain unlock closes its real backend; the original insert error wins."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncConnection

    _, default_maker = committed_db
    engine = create_async_engine(default_maker.kw["bind"].url, pool_size=pool_size, max_overflow=0, pool_timeout=1)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    queue, _ = stage_env
    queue.ledger_sessionmaker = maker
    fid = str(uuid.uuid4())
    pids: list[int] = []
    invalidated: list[bool] = []
    actual_execute = AsyncConnection.execute
    actual_invalidate = AsyncConnection.invalidate
    actual_insert = queue._enqueue

    async def failed_unlock(connection: Any, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if connection.engine is engine and str(statement).startswith("SELECT pg_advisory_unlock("):
            pids.append((await actual_execute(connection, text("SELECT pg_backend_pid()"))).scalar_one())
            raise RuntimeError("synthetic uncertain unlock")
        return await actual_execute(connection, statement, *args, **kwargs)

    async def observed_invalidation(connection: Any, *args: Any, **kwargs: Any) -> None:
        await actual_invalidate(connection, *args, **kwargs)
        if connection.engine is engine:
            invalidated.append(connection.invalidated)

    async def failed_insert(_job: Any) -> Any:
        raise RuntimeError("original broker failure")

    monkeypatch.setattr(AsyncConnection, "execute", failed_unlock)
    monkeypatch.setattr(AsyncConnection, "invalidate", observed_invalidation)
    if broker_fails:
        monkeypatch.setattr(queue, "_enqueue", failed_insert)
    try:
        expected = "original broker failure" if broker_fails else "synthetic uncertain unlock"
        with pytest.raises(RuntimeError, match=expected):
            await asyncio.wait_for(queue.enqueue("process_file", file_id=fid), 5)
        assert ledger_enqueue_session.get() is None
        assert invalidated == [True] and len(pids) == 1
        async with default_maker() as observer:
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=:pid "
                        "AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
                    ),
                    {"pid": pids[0]},
                )
                == 0
            )
        monkeypatch.setattr(AsyncConnection, "execute", actual_execute)
        monkeypatch.setattr(queue, "_enqueue", actual_insert)
        if not broker_fails:
            accepted = await queue.job(f"process_file:{fid}")
            assert accepted is not None
            await queue.finish(accepted, Status.FAILED)
        assert await asyncio.wait_for(queue.enqueue("process_file", file_id=fid), 5) is not None
    finally:
        await engine.dispose()


async def test_analysis_queue_rejects_foreign_registration_and_survives_lock_outage(
    committed_db: Any, stage_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong queues are rejected before writes; lock bookkeeping failure still enqueues."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from saq import Job

    from phaze.services import analysis_attempt

    _, maker = committed_db
    queue, _ = stage_env
    queue.ledger_sessionmaker = maker
    fid = str(uuid.uuid4())
    foreign = Job(function="process_file", kwargs={"file_id": fid}, queue=SimpleNamespace(name="foreign"))
    with pytest.raises(ValueError, match="different queue"):
        await queue.enqueue(foreign)
    async with maker() as session:
        assert await session.get(SchedulingLedger, f"process_file:{fid}") is None

    @asynccontextmanager
    async def unavailable_lock(*_args: Any) -> Any:
        raise RuntimeError("synthetic lock acquisition outage")
        yield

    monkeypatch.setattr(analysis_attempt, "serialize_analysis_enqueue", unavailable_lock)
    accepted = await queue.enqueue("process_file", file_id=fid)
    assert accepted is not None and ledger_enqueue_session.get() is None
    assert await queue.job(accepted.key) is not None
    async with maker() as session:
        row = await session.get(SchedulingLedger, accepted.key)
        assert row is not None and accepted.meta[ATTEMPT_META_KEY] == row.enqueued_at.isoformat()


def test_backfill_rejects_naive_attempt_epoch_in_broker_payload() -> None:
    """A timezone-less broker token cannot be mistaken for an attempt identity."""
    import json

    from phaze.tasks.recovery_backfill import _classify_saq_job_row

    fid = str(uuid.uuid4())
    key = f"process_file:{fid}"
    blob = json.dumps({"function": "process_file", "kwargs": {"file_id": fid}, "meta": {ATTEMPT_META_KEY: "2026-10-03T12:00:00"}})
    candidate = _classify_saq_job_row((blob, key))
    assert candidate is not None and candidate["enqueued_at"] is None


@pytest.mark.parametrize("terminal", ["finish", "abort", "delete"])
async def test_terminal_transition_after_live_read_remains_a_dedup(
    committed_db: Any, stage_env: Any, terminal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finish between the observed-live read and return cannot accept a fresh old-epoch job."""
    _, maker = committed_db
    queue, _ = stage_env
    queue.ledger_sessionmaker = maker
    fid = str(uuid.uuid4())
    job = await queue.enqueue("process_file", file_id=fid)
    assert job is not None
    entered = asyncio.Event()
    release = asyncio.Event()
    original_job = queue.job

    async def paused_live_read(key: str) -> Any:
        snapshot = await original_job(key)
        entered.set()
        await release.wait()
        return snapshot

    monkeypatch.setattr(queue, "job", paused_live_read)
    producer = asyncio.create_task(queue.enqueue("process_file", file_id=fid))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        if terminal == "delete":
            job.ttl = -1
        if terminal == "abort":
            await queue.abort(job, "synthetic abort")
        else:
            await queue.finish(job, Status.FAILED)
    finally:
        release.set()
    assert await producer is None
    stored = await original_job(job.key)
    assert stored is None if terminal == "delete" else stored.status in {Status.FAILED, Status.ABORTED}
    async with maker() as session:
        row = await session.get(SchedulingLedger, job.key)
        assert row is not None and row.enqueued_at == datetime.fromisoformat(job.meta[ATTEMPT_META_KEY])


async def test_job_instance_overrides_serialize_the_effective_task_key(committed_db: Any, stage_env: Any) -> None:
    """SAQ's accepted Job-instance and explicit-kwargs forms share the same attempt key."""
    from saq import Job

    _, maker = committed_db
    queue, _ = stage_env
    queue.ledger_sessionmaker = maker
    original_id, effective_id = str(uuid.uuid4()), str(uuid.uuid4())
    job = await queue.enqueue(Job(function="process_file", kwargs={"file_id": original_id}), file_id=effective_id)
    assert job is not None and job.key == f"process_file:{effective_id}"
    assert await queue.enqueue("process_file", kwargs={"file_id": effective_id}) is None
    async with maker() as session:
        assert await session.get(SchedulingLedger, f"process_file:{original_id}") is None
        row = await session.get(SchedulingLedger, job.key)
        assert row is not None and row.enqueued_at == datetime.fromisoformat(job.meta[ATTEMPT_META_KEY])


async def test_failure_callback_serializes_with_new_enqueue_without_nested_pool_use(
    committed_db: Any, stage_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ACK holds its attempt through finalize; the later lost attempt remains recoverable."""
    from datetime import UTC, timedelta
    from types import SimpleNamespace

    from sqlalchemy import text

    from phaze.config import get_settings
    from phaze.config_backends import LocalBackend
    from phaze.models.agent import Agent
    from phaze.models.analysis import AnalysisResult
    from phaze.models.file import FileRecord
    from phaze.routers.agent_analysis import report_analysis_failed
    from phaze.schemas.agent_analysis import AnalysisFailurePayload
    from phaze.services import analysis_attempt
    from phaze.services.analysis_enqueue import enqueue_process_file
    from phaze.tasks.reenqueue import recover_orphaned_work
    from tests._queue_fakes import make_agent_live
    from tests.db_guard import BLOCKED_WAITER_SQL

    _, default_maker = committed_db
    engine = create_async_engine(default_maker.kw["bind"].url, pool_size=2, max_overflow=0, pool_timeout=1)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    queue, _ = stage_env
    queue.ledger_sessionmaker = maker
    monkeypatch.setattr(get_settings(), "backends", [LocalBackend(kind="local", id="local", rank=99, cap=1)])
    file = FileRecord(
        sha256_hash=uuid.uuid4().hex * 2,
        original_path="/test/song.mp3",
        original_filename="song.mp3",
        current_path="/test/song.mp3",
        file_type="mp3",
        file_size=1024,
        agent_id="test-fileserver",
    )
    try:
        async with maker() as session:
            session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
            await session.flush()
            session.add(file)
            await session.flush()
            session.add(AnalysisResult(file_id=file.id, analysis_completed_at=datetime.now(UTC) - timedelta(days=1)))
            await make_agent_live(session)
            await session.commit()
        old_job = await enqueue_process_file(queue, file, "test-fileserver", "/models")
        assert old_job is not None
        await queue.finish(old_job, Status.FAILED)
        entered, release = asyncio.Event(), asyncio.Event()
        actual_guard = analysis_attempt.analysis_failure_ack_matches

        async def paused_ack_guard(session: Any, key: str, epoch: Any) -> bool:
            accepted = await actual_guard(session, key, epoch)
            entered.set()
            await release.wait()
            return accepted

        monkeypatch.setattr(analysis_attempt, "analysis_failure_ack_matches", paused_ack_guard)

        async def ack() -> None:
            async with maker() as session:
                agent = await session.get(Agent, "test-fileserver")
                await report_analysis_failed(
                    file.id, AnalysisFailurePayload(reason="error", attempt_enqueued_at=old_job.meta[ATTEMPT_META_KEY]), agent, session
                )

        callback = asyncio.create_task(ack())
        await asyncio.wait_for(entered.wait(), 5)
        producer = asyncio.create_task(enqueue_process_file(queue, file, "test-fileserver", "/models"))
        try:
            # Observe the producer queued on the callback's key lock from an independent connection.
            async with default_maker() as observer:
                async with asyncio.timeout(3):
                    while not await observer.scalar(text(BLOCKED_WAITER_SQL)):
                        await asyncio.sleep(0.01)
            assert not producer.done()
        finally:
            release.set()
        _, new_job = await asyncio.wait_for(asyncio.gather(callback, producer), 5)
        assert new_job is not None and new_job.meta[ATTEMPT_META_KEY] != old_job.meta[ATTEMPT_META_KEY]
        async with queue.pool.connection() as connection:
            await connection.execute("DELETE FROM saq_jobs WHERE key = %s", (new_job.key,))
        async with maker() as session:
            row = await session.get(SchedulingLedger, new_job.key)
            assert row is not None and row.terminal_at is None
            analysis = await session.scalar(select(AnalysisResult).where(AnalysisResult.file_id == file.id))
            assert analysis is not None and analysis.failed_at is None
        router = SimpleNamespace(queue_for=lambda *_args: queue)
        result = await recover_orphaned_work({"async_session": maker, "queue": queue, "task_router": router}, force=True)
        assert result["stages"]["process_file"]["reenqueued"] == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("pool_size", [1, 2])
@pytest.mark.parametrize("cancel_at", ["blocked_acquisition", "acquisition_completed", "broker_insert"])
async def test_cancelled_enqueue_releases_session_lock_and_context(
    committed_db: Any, stage_env: Any, pool_size: int, cancel_at: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled producers cannot return an advisory-locked connection to a tiny pool."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncConnection

    from phaze.services.analysis_attempt import _LOCK_KEY
    from tests.db_guard import BLOCKED_WAITER_SQL

    _, default_maker = committed_db
    engine = create_async_engine(default_maker.kw["bind"].url, pool_size=pool_size, max_overflow=0, pool_timeout=1)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    queue, _ = stage_env
    queue.ledger_sessionmaker = maker
    fid = str(uuid.uuid4())
    key = f"process_file:{fid}"
    entered = asyncio.Event()
    pids: set[int] = set()
    context_after: list[Any] = []
    driver_execute = AsyncConnection.execute
    driver_invalidations: list[bool] = []
    injected = False

    async def instrument_execute(connection: Any, statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        if connection.engine is engine and str(statement).startswith("SELECT pg_advisory_lock("):
            pids.add((await driver_execute(connection, text("SELECT pg_backend_pid()"))).scalar_one())
            try:
                result = await driver_execute(connection, statement, *args, **kwargs)
            except asyncio.CancelledError:
                driver_invalidations.append(connection.invalidated)
                raise
            if cancel_at == "acquisition_completed" and not injected:
                injected = True
                # The server really acquired the lock, but cancellation arrives before
                # the acquisition await returns to the owner. The driver remains valid.
                raise asyncio.CancelledError
            return result
        return await driver_execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", instrument_execute)
    actual_insert = queue._enqueue

    async def paused_insert(_job: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    if cancel_at == "broker_insert":
        monkeypatch.setattr(queue, "_enqueue", paused_insert)

    async def producer() -> Any:
        try:
            return await queue.enqueue("process_file", file_id=fid)
        finally:
            context_after.append(ledger_enqueue_session.get())

    holder = None
    try:
        if cancel_at == "blocked_acquisition":
            # Hold the key using a different pool, so even pool_size=1 reaches a real
            # blocked advisory-lock query rather than waiting for a pool checkout.
            holder = await default_maker.kw["bind"].connect()
            await holder.execute(text(f"SELECT pg_advisory_lock({_LOCK_KEY})"), {"key": key})
            await holder.commit()
        task = asyncio.create_task(producer())
        if cancel_at == "blocked_acquisition":
            async with default_maker() as observer:
                async with asyncio.timeout(3):
                    while not await observer.scalar(text(BLOCKED_WAITER_SQL)):
                        await asyncio.sleep(0.01)
            task.cancel()
        elif cancel_at == "broker_insert":
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert context_after == [None]
        if cancel_at == "blocked_acquisition":
            assert driver_invalidations == [True], "record the actual asyncpg/SQLAlchemy cancellation behavior"
        assert pids
        if holder is not None:
            await holder.execute(text(f"SELECT pg_advisory_unlock({_LOCK_KEY})"), {"key": key})
            await holder.commit()
            await holder.close()
            holder = None
        async with default_maker() as observer:
            for pid in pids:
                count = await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = :pid "
                        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                    ),
                    {"pid": pid},
                )
                assert count == 0, "a cancelled producer leaked its session advisory lock"
        monkeypatch.setattr(queue, "_enqueue", actual_insert)
        outcomes = await asyncio.wait_for(asyncio.gather(*(queue.enqueue("process_file", file_id=fid) for _ in range(2))), 5)
        assert sum(job is not None for job in outcomes) == 1
    finally:
        if holder is not None:
            await holder.execute(text(f"SELECT pg_advisory_unlock({_LOCK_KEY})"), {"key": key})
            await holder.commit()
            await holder.close()
        await engine.dispose()
