"""Actual SAQ scheduling/delivery revisits retired-backend rows protected at boot."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from saq import Status, Worker
from saq.queue.postgres import PostgresQueue
from sqlalchemy import select, text

from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.services.pipeline.cloud import get_cloud_staging_candidates
from phaze.tasks import controller
from phaze.tasks.reap_orphaned_backend_cloud_jobs import reap_orphaned_backend_cloud_jobs
from phaze.tasks.submit_cloud_job import submit_cloud_job
from tests.analyze.services.backends.protocol._shared import _seed_staging_cloud_job
from tests.db_guard import integration_dsns


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration

# G0 containment bounds (phaze-47gba): every non-cleanup async transition settles within five
# seconds, and the whole finally settlement block shares ONE ten-second aggregate budget rather
# than a fresh ten seconds per resource. A bound that trips is a containment failure, never a
# semantic verdict on the revisit itself.
_TRANSITION_SEC = 5
_CLEANUP_SEC = 10


class _PreviousMinute(datetime):
    """Make the real croniter producer's next occurrence due without a wall-clock sleep."""

    tick = 0

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        cls.tick += 1
        return datetime.now(tz) - timedelta(minutes=10 - cls.tick)


async def _snapshot(factory: async_sessionmaker[AsyncSession]) -> dict[uuid.UUID, tuple[str, int]]:
    async with asyncio.timeout(_TRANSITION_SEC), factory() as session:
        return {
            fid: (status, attempts)
            for fid, status, attempts in (await session.execute(select(CloudJob.file_id, CloudJob.status, CloudJob.attempts))).all()
        }


async def _scheduled_tick(worker: Worker[Any], queue: PostgresQueue, *, expected_status: Status = Status.COMPLETE) -> Any:
    # No test-side enqueue of the reaper: production registration feeds the installed SAQ
    # scheduler, which writes the broker job, then Worker.process delivers the registered body.
    # The whole schedule/read/process/read transition shares one five-second bound.
    async with asyncio.timeout(_TRANSITION_SEC):
        await worker.schedule()
        job = await queue.job("cron:reap_orphaned_backend_cloud_jobs")
        assert job is not None, "controller scheduling must revisit deferred retired-backend staging rows"
        assert job.scheduled < datetime.now(UTC).timestamp()
        assert await worker.process()
        settled = await queue.job(job.key)
    assert settled is not None and settled.status == expected_status, settled
    return settled.result if expected_status == Status.COMPLETE else settled.error


@pytest.mark.parametrize("protected_by", ["age", "live-submit", "transient-broker"])
async def test_scheduled_cleanup_recovers_only_eligible_orphans(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
    protected_by: str,
) -> None:
    _, factory = committed_db
    # A configured backend stays outside this reaper, without requiring any remote cluster.
    backends_toml_env("""
        [[backends]]
        kind = "kueue"
        id = "current-cloud"
        rank = 20
        cap = 5
        buckets = ["staging-a"]
        [backends.kube]
        api_url = "https://kube.example.com"
        namespace = "phaze"
        local_queue = "phaze-lq"
        [[buckets]]
        id = "staging-a"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-staging-a"
    """)
    dsn, _ = integration_dsns()
    queue = PostgresQueue.from_url(dsn, name=f"orphan-revisit-{uuid.uuid4().hex}")
    submit_queue = PostgresQueue.from_url(dsn, name=f"orphan-submit-{uuid.uuid4().hex}")
    async with asyncio.timeout(_TRANSITION_SEC):
        await queue.connect()
        await submit_queue.connect()
    worker: Worker[Any] = Worker(
        queue,
        functions=controller.settings["functions"],
        cron_jobs=[cj for cj in controller.settings["cron_jobs"] if cj.function is reap_orphaned_backend_cloud_jobs],
        concurrency=1,
        dequeue_timeout=1,
    )
    worker.context["async_session"] = factory
    submit_worker: Worker[Any] = Worker(submit_queue, functions=[submit_cloud_job], concurrency=1, dequeue_timeout=1)
    submit_worker.context["async_session"] = factory
    _PreviousMinute.tick = 0
    monkeypatch.setattr("saq.worker.datetime", _PreviousMinute)
    try:
        async with asyncio.timeout(_TRANSITION_SEC), factory() as session:
            session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
            await session.commit()
            target = await _seed_staging_cloud_job(
                session, backend_id="retired-cloud", status=CloudJobStatus.UPLOADED, age_sec=3_600 if protected_by == "live-submit" else 60
            )
            young = await _seed_staging_cloud_job(session, backend_id="retired-cloud", status=CloudJobStatus.UPLOADED, age_sec=60)
            owned = await _seed_staging_cloud_job(session, backend_id="current-cloud", status=CloudJobStatus.UPLOADED, age_sec=90_000)
            submitted = await _seed_staging_cloud_job(session, backend_id="retired-cloud", status=CloudJobStatus.SUBMITTED, age_sec=90_000)
            running = await _seed_staging_cloud_job(session, backend_id="retired-cloud", status=CloudJobStatus.RUNNING, age_sec=90_000)
        live_job = None
        if protected_by == "live-submit":
            async with asyncio.timeout(_TRANSITION_SEC):
                live_job = await submit_queue.enqueue("submit_cloud_job", file_id=str(target), key=f"submit_cloud_job:{target}", retries=0)
            assert live_job is not None
        before = await _snapshot(factory)
        # Same real boot helper/body used by controller.startup; constructor/network startup
        # wiring remains independently pinned by the existing controller startup tests.
        async with asyncio.timeout(_TRANSITION_SEC):
            tally = await controller._run_boot_reconcile_with_retry("test orphan boot", lambda: reap_orphaned_backend_cloud_jobs(worker.context))
        assert tally == {"requeued": 0, "surfaced": 2}
        assert await _snapshot(factory) == before
        assert await _scheduled_tick(worker, queue) == {"requeued": 0, "surfaced": 2}
        assert await _snapshot(factory) == before
        if live_job is not None:
            # Real delivery fails resolution of the retired backend BEFORE any remote call.
            async with asyncio.timeout(_TRANSITION_SEC):
                assert await submit_worker.process()
                failed = await submit_queue.job(live_job.key)
            assert failed is not None and failed.status == Status.FAILED
            assert "no kueue backend resolves" in (failed.error or "")
        else:
            async with asyncio.timeout(_TRANSITION_SEC), factory() as session:
                await session.execute(text("UPDATE cloud_job SET updated_at = now() - interval '901 seconds' WHERE file_id = :fid"), {"fid": target})
                await session.commit()
        if protected_by == "transient-broker":
            import phaze.tasks.reap_orphaned_backend_cloud_jobs as orphan_module

            real_live_keys = orphan_module.get_live_job_keys

            async def unavailable(_session: AsyncSession) -> set[str]:
                raise RuntimeError("synthetic broker read unavailable")

            monkeypatch.setattr(orphan_module, "get_live_job_keys", unavailable)
            error = await _scheduled_tick(worker, queue, expected_status=Status.FAILED)
            assert "synthetic broker read unavailable" in error
            assert await _snapshot(factory) == before
            monkeypatch.setattr(orphan_module, "get_live_job_keys", real_live_keys)
        assert await _scheduled_tick(worker, queue) == {"requeued": 1, "surfaced": 2}
        expected = before | {target: (CloudJobStatus.AWAITING.value, 1)}
        assert await _snapshot(factory) == expected
        assert await _scheduled_tick(worker, queue) == {"requeued": 0, "surfaced": 2}
        assert await _snapshot(factory) == expected  # no second attempt consumed
        async with asyncio.timeout(_TRANSITION_SEC), factory() as session:
            candidates = await get_cloud_staging_candidates(session, limit=10)
            assert {file.id for file, _ in candidates} == {target}
        assert expected[young] == (CloudJobStatus.UPLOADED.value, 0)
        assert expected[owned] == (CloudJobStatus.UPLOADED.value, 0)
        assert expected[submitted] == (CloudJobStatus.SUBMITTED.value, 0)
        assert expected[running] == (CloudJobStatus.RUNNING.value, 0)
    finally:
        # One aggregate settlement budget for every owned resource, in dependency order: stop the
        # workers (their job tasks first), then delete only this test's unique queue rows, then
        # disconnect both pools.
        async with asyncio.timeout(_CLEANUP_SEC):
            await worker.stop()
            await submit_worker.stop()
            # SAQ tables are outside ORM metadata; clean only this test's unique queues.
            async with queue.pool.connection() as conn:
                await conn.execute("DELETE FROM saq_jobs WHERE queue IN (%s, %s)", (queue.name, submit_queue.name))
            await submit_queue.disconnect()
            await queue.disconnect()
        # Settlement receipt: no owned scheduler/worker task or in-flight job context survives.
        assert not worker.tasks and not worker.job_task_contexts
        assert not submit_worker.tasks and not submit_worker.job_task_contexts
