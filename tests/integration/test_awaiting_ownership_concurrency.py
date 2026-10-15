"""Independent connections prove both orders of local/cloud admission."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.analysis_attempt import serialize_analysis_enqueue
from phaze.tasks import awaiting_ownership, release_awaiting_cloud
from phaze.tasks.awaiting_ownership import reconcile_awaiting_ownership
from phaze.tasks.release_awaiting_cloud import stage_cloud_window
from tests._queue_fakes import DedupFakeTaskRouter
from tests.analyze.tasks.test_awaiting_ownership import _conflict
from tests.analyze.tasks.test_drain_head_of_line import _CloudStub, _patch_backends
from tests.db_guard import BLOCKED_WAITER_SQL


async def _seed_conflict(maker: Any) -> Any:
    async with maker() as session:
        session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[], last_seen_at=datetime.now(UTC)))
        await session.commit()
        return await _conflict(session)


async def test_producer_lock_protects_even_old_ledger_before_broker_insert(committed_db: Any, stage_env: Any) -> None:
    _, maker = committed_db
    file = await _seed_conflict(maker)
    key = f"process_file:{file.id}"
    async with serialize_analysis_enqueue(maker, key):
        counts = await reconcile_awaiting_ownership({"async_session": maker})
        assert counts["transferred"] == 0
        assert counts["held_enqueue"] == 1
    assert (await reconcile_awaiting_ownership({"async_session": maker}))["transferred"] == 1
    async with maker() as session:
        assert await session.scalar(select(CloudJob.status).where(CloudJob.file_id == file.id)) == "awaiting"


async def test_committed_generation_refresh_after_snapshot_wins(committed_db: Any, stage_env: Any, monkeypatch: Any) -> None:
    _, maker = committed_db
    await _seed_conflict(maker)
    original = awaiting_ownership.try_lock_analysis_attempt

    async def refresh(session: Any, key: str) -> bool:
        async with maker() as writer:
            # Keep it OLD: freshness alone cannot protect a changed generation.
            old = datetime.now(UTC) - timedelta(days=3)
            await writer.execute(update(SchedulingLedger).where(SchedulingLedger.key == key).values(enqueued_at=old, updated_at=old))
            await writer.commit()
        return await original(session, key)

    monkeypatch.setattr(awaiting_ownership, "try_lock_analysis_attempt", refresh)
    counts = await reconcile_awaiting_ownership({"async_session": maker})
    assert counts["transferred"] == 0
    assert counts["held_changed"] == 1


async def test_delete_rechecks_generation_after_waiting_for_row_update(committed_db: Any, stage_env: Any, monkeypatch: Any) -> None:
    _, maker = committed_db
    file = await _seed_conflict(maker)
    original = awaiting_ownership.try_lock_analysis_attempt
    async with maker() as writer:

        async def update_while_locked(session: Any, key: str) -> bool:
            locked = await original(session, key)
            old = datetime.now(UTC) - timedelta(days=3)
            await writer.execute(update(SchedulingLedger).where(SchedulingLedger.key == key).values(enqueued_at=old, updated_at=old))
            return locked

        monkeypatch.setattr(awaiting_ownership, "try_lock_analysis_attempt", update_while_locked)
        reaper = asyncio.create_task(reconcile_awaiting_ownership({"async_session": maker}))
        try:
            async with asyncio.timeout(5):
                async with maker() as observer:
                    while not await observer.scalar(text(BLOCKED_WAITER_SQL)):
                        await asyncio.sleep(0.01)
            await writer.commit()
            assert (await asyncio.wait_for(reaper, 5))["transferred"] == 0
            async with maker() as observer:
                assert await observer.get(SchedulingLedger, f"process_file:{file.id}") is not None
        finally:
            await writer.rollback()
            if not reaper.done():
                reaper.cancel()
                await asyncio.gather(reaper, return_exceptions=True)


@pytest.mark.parametrize("winner", ["producer", "cloud"])
async def test_candidate_selection_race_has_one_owner(committed_db: Any, stage_env: Any, monkeypatch: Any, winner: str) -> None:
    _, maker = committed_db
    queue, _ = stage_env
    queue.ledger_sessionmaker = maker
    file = await _seed_conflict(maker)
    backend = _CloudStub(id="kueue-local", rank=10, cap=1)
    _patch_backends(monkeypatch, [backend])
    ctx = {"async_session": maker, "task_router": DedupFakeTaskRouter()}
    producer_task = None
    original_budget = release_awaiting_cloud._cloud_budget_for
    original_dispatch = backend.dispatch

    async def enqueue_after_select(session: Any, fid: Any) -> Any:
        assert await queue.enqueue("process_file", file_id=str(fid)) is not None
        return await original_budget(session, fid)

    async def enqueue_during_dispatch(file: Any, session: Any, task_router: Any) -> bool:
        nonlocal producer_task
        started = asyncio.Event()
        from phaze.services import analysis_attempt

        original_serialize = analysis_attempt.serialize_analysis_enqueue
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def observe_wait(maker: Any, key: str) -> Any:
            started.set()
            async with original_serialize(maker, key):
                yield

        monkeypatch.setattr(analysis_attempt, "serialize_analysis_enqueue", observe_wait)
        producer_task = asyncio.create_task(queue.enqueue("process_file", file_id=str(file.id)))
        await asyncio.wait_for(started.wait(), 5)
        # The attempt lock is held by the drain across the cloud write+commit.
        assert not producer_task.done()
        return await original_dispatch(file, session, task_router)

    if winner == "producer":
        monkeypatch.setattr(release_awaiting_cloud, "_cloud_budget_for", enqueue_after_select)
    else:
        monkeypatch.setattr(backend, "dispatch", enqueue_during_dispatch)
    try:
        outcome = await asyncio.wait_for(stage_cloud_window(ctx), 10)
        if winner == "producer":
            assert outcome == {"staged": 0, "skipped": 1}
            assert backend.dispatched == []
            assert await queue.job(f"process_file:{file.id}") is not None
        else:
            assert outcome == {"staged": 1, "skipped": 0}
            assert producer_task is not None
            assert await asyncio.wait_for(producer_task, 5) is None
            assert await queue.job(f"process_file:{file.id}") is None
            # The compute handoff from an already-submitted cloud job stays valid.
            assert await queue.enqueue("process_file", file_id=str(file.id), scratch_path="/scratch/synthetic.flac") is not None
    finally:
        if producer_task is not None and not producer_task.done():
            producer_task.cancel()
            await asyncio.gather(producer_task, return_exceptions=True)


@pytest.mark.parametrize("status", ["awaiting", "uploading", "uploaded", "submitted", "running", "failed", "succeeded"])
async def test_lock_outage_preserves_cloud_owner_and_ordinary_local_availability(
    committed_db: Any, stage_env: Any, monkeypatch: Any, status: str
) -> None:
    _, maker = committed_db
    queue, _ = stage_env
    queue.ledger_sessionmaker = maker
    file = await _seed_conflict(maker)
    async with maker() as session:
        await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(status=status))
        await session.commit()

    @asynccontextmanager
    async def unavailable_lock(*_args: Any) -> Any:
        raise RuntimeError("synthetic lock acquisition outage")
        yield

    monkeypatch.setattr("phaze.services.analysis_attempt.serialize_analysis_enqueue", unavailable_lock)
    job = await queue.enqueue("process_file", file_id=str(file.id))
    if status in {"failed", "succeeded"}:
        assert job is not None, "ordinary local work retains the existing availability fallback"
    else:
        assert job is None
        assert await queue.job(f"process_file:{file.id}") is None
    async with maker() as observer:
        row = await observer.get(SchedulingLedger, f"process_file:{file.id}")
        assert row is not None
        assert row.enqueued_at < datetime.now(UTC) - timedelta(days=1), "untrusted fallback must not refresh ledger authority"
    # Scratch-path handoff is the cloud owner's legitimate analysis job.
    if status == "running":
        assert await queue.enqueue("process_file", file_id=str(file.id), scratch_path="/scratch/synthetic.flac") is not None


async def test_exhausted_ledger_pool_cannot_bypass_cloud_ownership(committed_db: Any, stage_env: Any, monkeypatch: Any) -> None:
    _, maker = committed_db
    queue, _ = stage_env
    file = await _seed_conflict(maker)
    engine = create_async_engine(maker.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=0.05)
    queue.ledger_sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    inserts = []
    original_insert = queue._enqueue

    async def record_insert(job: Any) -> Any:
        inserts.append(job)
        return await original_insert(job)

    monkeypatch.setattr(queue, "_enqueue", record_insert)
    try:
        async with engine.connect():
            # The SAQ broker's independent psycopg pool is usable throughout.
            assert await queue.job(f"process_file:{file.id}") is None
            with pytest.raises(SQLAlchemyTimeoutError):
                await queue.enqueue("process_file", file_id=str(file.id))
            assert inserts == []
            assert await queue.job(f"process_file:{file.id}") is None
        # Once the pool recovers, normal serialization may enqueue awaiting work locally.
        assert await queue.enqueue("process_file", file_id=str(file.id)) is not None
    finally:
        await engine.dispose()
