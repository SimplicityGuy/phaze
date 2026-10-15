"""Regression for the awaiting/legacy-ledger ownership deadlock (phaze-lwqz0)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import ProgrammingError

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.services.backends.kueue import KueueBackend
from phaze.tasks import awaiting_ownership
from phaze.tasks.awaiting_ownership import reconcile_awaiting_ownership, transfer_awaiting_ownership
from phaze.tasks.release_awaiting_cloud import stage_cloud_window
from tests._queue_fakes import DedupFakeTaskRouter, seed_active_agent
from tests.analyze.tasks.test_drain_head_of_line import _BASE_TIME, _CloudStub, _make_ctx, _make_file, _patch_backends, _seed
from tests.analyze.tasks.test_ledger_reaper import _saq_table


async def _conflict(session: Any, *, status: str = "awaiting", function: str = "process_file", age: int = 2) -> Any:
    file = _make_file(_BASE_TIME)
    await _seed(session, [file], attempts=1)
    await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(status=status))
    old = datetime.now(UTC) - timedelta(days=age)
    session.add(
        SchedulingLedger(
            key=f"{function}:{file.id}",
            function=function,
            routing="agent",
            payload={"file_id": str(file.id)},
            enqueued_at=old,
            updated_at=old,
        )
    )
    await session.commit()
    return file


async def _keys(session: Any) -> set[str]:
    return set((await session.scalars(select(SchedulingLedger.key))).all())


@pytest.mark.parametrize("first_lane", ["kueue-local", "kueue-remote"])
async def test_real_kubernetes_backends_make_continued_bounded_progress(session: Any, monkeypatch: Any, first_lane: str) -> None:
    await _saq_table(session)
    files = [await _conflict(session) for _ in range(4)]
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    other = "kueue-remote" if first_lane == "kueue-local" else "kueue-local"
    first = KueueBackend(id=first_lane, rank=10, cap=1, config=SimpleNamespace(buckets=["test-bucket"]))
    second = KueueBackend(id=other, rank=20, cap=2, config=SimpleNamespace(buckets=["test-bucket"]))
    _patch_backends(monkeypatch, [second, first])
    monkeypatch.setattr("phaze.services.backends.kueue.s3_staging.resolve_bucket_config", lambda *_args: SimpleNamespace(id="test-bucket"))

    async def available(self: Any, session: Any) -> bool:
        return True

    staged = []

    async def stage(session: Any, file: Any, router: Any, bucket: Any) -> None:
        staged.append(file.id)
        await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(status="uploading"))

    monkeypatch.setattr(KueueBackend, "is_available", available)
    monkeypatch.setattr("phaze.services.backends.kueue._stage_file_to_s3", stage)
    ctx = _make_ctx(DedupFakeTaskRouter())
    assert await stage_cloud_window(ctx) == {"staged": 3, "skipped": 0}
    assert await first.in_flight_count(session) == 1
    assert await second.in_flight_count(session) == 2
    assert await stage_cloud_window(ctx) == {"staged": 0, "skipped": 0}
    assert len(staged) == len(set(staged)) == 3
    # Finish one lane slot; the next normal tick takes exactly the remaining file.
    await session.execute(update(CloudJob).where(CloudJob.file_id == staged[0]).values(status="succeeded"))
    await session.commit()
    assert await stage_cloud_window(ctx) == {"staged": 1, "skipped": 0}
    assert set(staged) == {file.id for file in files}


@pytest.mark.parametrize("lane_order", [("kueue-local", "kueue-remote"), ("kueue-remote", "kueue-local")])
async def test_repair_then_real_drain_dispatches_once_in_rank_and_cap_order(session: Any, monkeypatch: Any, lane_order: tuple[str, str]) -> None:
    await _saq_table(session)
    files = [await _conflict(session) for _ in range(4)]
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    first = _CloudStub(id=lane_order[0], rank=10, cap=1)
    second = _CloudStub(id=lane_order[1], rank=20, cap=2)
    _patch_backends(monkeypatch, [second, first])
    ctx = _make_ctx(DedupFakeTaskRouter())
    assert await stage_cloud_window(ctx) == {"staged": 3, "skipped": 0}
    assert len(first.dispatched) == 1
    assert len(second.dispatched) == 2
    assert len(set(first.dispatched + second.dispatched)) == 3
    # Capacity here is a backend stub; temporarily fill it to inspect a repeated tick.
    first.cap = second.cap = 0
    assert await stage_cloud_window(ctx) == {"staged": 0, "skipped": 0}
    assert (await reconcile_awaiting_ownership(ctx))["transferred"] == 0
    rows = (await session.execute(select(CloudJob.status, CloudJob.attempts))).all()
    assert sum(status == "awaiting" for status, _ in rows) == 1
    assert all(attempts == 1 for _, attempts in rows)
    assert len(files) == 4
    assert not await _keys(session)


@pytest.mark.parametrize("broker_status", ["queued", "active"])
async def test_live_broker_protects_stale_ledger(session: Any, broker_status: str) -> None:
    await _saq_table(session)
    file = await _conflict(session)
    key = f"process_file:{file.id}"
    await session.execute(text("INSERT INTO saq_jobs (key,status) VALUES (:key,:status)"), {"key": key, "status": broker_status})
    await session.commit()
    assert (await transfer_awaiting_ownership(session))["transferred"] == 0
    assert key in await _keys(session)


@pytest.mark.parametrize("status", ["uploading", "uploaded", "submitted", "running", "failed", "succeeded"])
async def test_busy_or_terminal_cloud_owner_untouched(session: Any, status: str) -> None:
    await _saq_table(session)
    file = await _conflict(session, status=status)
    assert (await transfer_awaiting_ownership(session))["conflicts"] == 0
    assert f"process_file:{file.id}" in await _keys(session)


@pytest.mark.parametrize("protected", ["recent", "updated", "unrelated", "orphan", "done", "failed", "skipped"])
async def test_existing_handling_preserved(session: Any, protected: str) -> None:
    await _saq_table(session)
    file = await _conflict(
        session, function="extract_file_metadata" if protected == "unrelated" else "process_file", age=0 if protected == "recent" else 2
    )
    if protected == "updated":
        await session.execute(update(SchedulingLedger).values(updated_at=func.clock_timestamp()))
    elif protected == "orphan":
        await session.execute(text("DELETE FROM cloud_job WHERE file_id=:fid"), {"fid": file.id})
    elif protected in {"done", "failed"}:
        values = {"analysis_completed_at" if protected == "done" else "failed_at": datetime.now(UTC)}
        session.add(AnalysisResult(file_id=file.id, **values))
    elif protected == "skipped":
        session.add(StageSkip(file_id=file.id, stage="analyze", reason="synthetic test skip"))
    await session.commit()
    assert (await transfer_awaiting_ownership(session))["transferred"] == 0
    assert len(await _keys(session)) == 1


async def test_generation_refresh_between_snapshot_and_delete_wins(session: Any, monkeypatch: Any) -> None:
    await _saq_table(session)
    file = await _conflict(session)
    original = awaiting_ownership.try_lock_analysis_attempt

    async def refresh(session: Any, key: str) -> bool:
        await session.execute(update(SchedulingLedger).where(SchedulingLedger.key == key).values(enqueued_at=func.clock_timestamp()))
        return await original(session, key)

    monkeypatch.setattr(awaiting_ownership, "try_lock_analysis_attempt", refresh)
    result = await transfer_awaiting_ownership(session)
    assert result["transferred"] == 0
    assert result["held_changed"] == 1
    assert f"process_file:{file.id}" in await _keys(session)


async def test_rollback_restores_local_obligation_and_sidecar(session: Any) -> None:
    await _saq_table(session)
    file = await _conflict(session)
    async with session.begin_nested() as savepoint:
        assert (await transfer_awaiting_ownership(session))["transferred"] == 1
        await savepoint.rollback()
    assert f"process_file:{file.id}" in await _keys(session)
    assert await session.scalar(select(CloudJob.status).where(CloudJob.file_id == file.id)) == CloudJobStatus.AWAITING.value


async def test_missing_broker_fails_closed(session: Any) -> None:
    await _saq_table(session)
    file = await _conflict(session)
    await session.execute(text("DROP TABLE saq_jobs"))
    await session.commit()
    result = await reconcile_awaiting_ownership(_make_ctx(DedupFakeTaskRouter()))
    assert result["degraded"] == 1
    assert f"process_file:{file.id}" in await _keys(session)


async def test_bounded_batches_make_idempotent_progress(session: Any, monkeypatch: Any) -> None:
    await _saq_table(session)
    for _ in range(3):
        await _conflict(session)
    monkeypatch.setattr(awaiting_ownership, "_BATCH_SIZE", 2)
    ctx = _make_ctx(DedupFakeTaskRouter())
    first = await reconcile_awaiting_ownership(ctx)
    assert first["conflicts"] == 3
    assert first["transferred"] == 2
    assert (await reconcile_awaiting_ownership(ctx))["transferred"] == 1
    assert (await reconcile_awaiting_ownership(ctx))["transferred"] == 0
    assert (await session.scalars(select(CloudJob.attempts))).all() == [1, 1, 1]


async def test_cloud_transition_between_snapshot_and_lock_preserved(session: Any, monkeypatch: Any) -> None:
    await _saq_table(session)
    file = await _conflict(session)
    original = awaiting_ownership.try_lock_analysis_attempt

    async def move_to_busy(session: Any, key: str) -> bool:
        await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(status="running"))
        return await original(session, key)

    monkeypatch.setattr(awaiting_ownership, "try_lock_analysis_attempt", move_to_busy)
    result = await transfer_awaiting_ownership(session)
    assert result["held_changed"] == 1
    assert result["transferred"] == 0
    assert f"process_file:{file.id}" in await _keys(session)


async def test_existing_unreadable_broker_does_not_authorize_cloud_dispatch(session: Any) -> None:
    await _saq_table(session)
    file = _make_file(_BASE_TIME)
    await _seed(session, [file], attempts=0)
    await session.execute(text("ALTER TABLE saq_jobs DROP COLUMN status"))
    await session.commit()
    with pytest.raises(ProgrammingError):
        await awaiting_ownership.cloud_candidate_available(session, file.id)
    # The failed probe's SAVEPOINT alone is rolled back; admission remains usable.
    assert await session.scalar(select(CloudJob.status).where(CloudJob.file_id == file.id)) == "awaiting"
