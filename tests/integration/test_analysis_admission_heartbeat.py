"""Real SAQ dispatch/sweep with synthetic extraction and a bounded synthetic clock."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock
import uuid

from pydantic import SecretStr
import pytest
from saq import Status, Worker

from phaze.config import AgentSettings
from phaze.runtime_config import RuntimeConfig
from phaze.schemas.agent_tasks import ProcessFilePayload
from phaze.services.resizable_limiter import ResizableLimiter
from phaze.services.video_audio import AudioSource
from phaze.tasks import functions


async def _until(predicate: Any, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.01)


@pytest.mark.integration
async def test_active_capacity_waiter_survives_real_sweep_and_completes_once(stage_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _ = stage_env
    limiter = ResizableLimiter(1)
    first_entered, release_first = asyncio.Event(), asyncio.Event()
    callbacks: list[Any] = []
    clock = [0]
    import saq.job
    import saq.queue.base

    # Immutable origin recorded BEFORE the clock seams are patched; never reset after dispatch.
    clock_origin = saq.job.now()
    clock[0] = clock_origin
    monkeypatch.setattr(saq.job, "now", lambda: clock[0])
    monkeypatch.setattr(saq.queue.base, "now", lambda: clock[0])
    # Accelerate only heartbeat cadence; the stored deadline stays the real default 3600s.
    monkeypatch.setattr(functions, "_HEARTBEAT_TOUCHES_PER_DEADLINE", 360000)
    cfg = AgentSettings(
        agent_api_url="http://synthetic",
        agent_token=SecretStr("synthetic"),
        kind="compute",
        lane_analyze_concurrency=2,
        worker_max_jobs=2,
        worker_process_pool_size=1,
    )

    runtime = RuntimeConfig(
        **{
            **functions.current_runtime_config().model_dump(),
            "lane_analyze_concurrency": 2,
            "worker_max_jobs": 2,
            "worker_process_pool_size": 1,
            "analysis_stall_timeout_sec": 1800,
        }
    )
    assert runtime.analysis_job_heartbeat_sec == 3600
    monkeypatch.setattr(functions, "current_runtime_config", lambda: runtime)

    async def extraction(path: str, *, heartbeat_cb: Any, **kwargs: Any) -> AudioSource:
        if path == "first.mp3":
            callbacks.append(heartbeat_cb)
            first_entered.set()
            await release_first.wait()
        return AudioSource(path, None)

    monkeypatch.setattr(functions, "extract_audio_track", extraction)
    monkeypatch.setattr(functions, "_run_analysis_with_progress", AsyncMock(return_value={}))

    async def task(ctx: dict[str, Any], path: str) -> str:
        payload = ProcessFilePayload(file_id=uuid.uuid4(), original_path=path, file_type="mp3", agent_id="synthetic", models_path="models")
        await functions._extract_and_analyze(AsyncMock(), cfg, ctx, payload, path, {"extracted_audio_path": None})
        return path

    worker = Worker(queue, functions=[("analyze", task)], concurrency=2, dequeue_timeout=0.1)
    worker.context["analysis_semaphore"] = limiter
    async with asyncio.timeout(5):
        first = await queue.enqueue("analyze", path="first.mp3", heartbeat=3600, timeout=0, retries=2)
    assert first is not None
    first_run = asyncio.create_task(worker.process())
    second_run = None
    try:
        await asyncio.wait_for(first_entered.wait(), 5)
        async with asyncio.timeout(5):
            second = await queue.enqueue("analyze", path="second.mp3", heartbeat=3600, timeout=0, retries=2)
        assert second is not None
        second_run = asyncio.create_task(worker.process())

        async def waiting() -> bool:
            stored = await queue.job(second.key)
            return limiter.waiting == 1 and stored is not None and stored.status == Status.ACTIVE

        await _until(waiting)
        async with asyncio.timeout(5):
            original_waiter = await queue.job(second.key)
        assert original_waiter is not None
        assert original_waiter.status == Status.ACTIVE and original_waiter.attempts == 1 and original_waiter.error is None
        assert original_waiter.heartbeat == 3600
        assert limiter.borrowed_tokens == 1 and limiter.waiting == 1
        original_touch = original_waiter.touched
        # Worker dispatch stamps touched from the patched queue clock, so the waiter starts at age 0.
        assert original_touch == clock_origin
        # Exactly three synthetic advances, 1200 + 1200 + 1201 = 3601 s, so only the LAST one takes a
        # relay-less waiter past SAQ's strict `age > heartbeat` (3600). Keep the synthetic first
        # extraction healthy at every step. Reads use independently committed broker connections.
        observations: list[tuple[int, bool, Any, Any, Any, Any]] = []
        for advance_sec in (1200, 1200, 1201):
            clock[0] += advance_sec * 1000
            async with asyncio.timeout(5):
                await callbacks[0]()

            async def waiter_touched() -> bool:
                stored = await queue.job(second.key)
                return stored is not None and stored.touched == clock[0]

            observed = True
            try:
                await _until(waiter_touched, 0.5)
            except TimeoutError:
                # Recorded, not asserted yet: the real sweep below is the first semantic oracle.
                observed = False
            async with asyncio.timeout(5):
                seen = await queue.job(second.key)
            assert seen is not None
            observations.append((clock[0], observed, seen.touched, seen.heartbeat, seen.status, seen.attempts))
        assert clock[0] == clock_origin + 3601 * 1000
        assert clock[0] > original_touch + 3600 * 1000
        async with asyncio.timeout(5):
            swept = await queue.sweep(abort=0.01)
        # First semantic oracle, captured before any cleanup can rewrite the broker row.
        assert swept == [], f"real sweep expired the capacity waiter: swept={swept} observations={observations}"
        # Positive proof AFTER the real sweep: each of the three steps observed a committed touch at
        # exactly that step's clock, so a starved relay cannot pass on its final touch alone.
        assert [(step, True, step, 3600, Status.ACTIVE, 1) for step, *_ in observations] == observations
        async with asyncio.timeout(5):
            stored = await queue.job(second.key)
        assert stored is not None and stored.status == Status.ACTIVE and stored.attempts == 1 and stored.error is None
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first_run, second_run), 5)
        for job in (first, second):
            async with asyncio.timeout(5):
                stored = await queue.job(job.key)
            assert stored is not None and stored.status == Status.COMPLETE and stored.attempts == 1 and stored.error is None
        assert limiter.borrowed_tokens == 0 and limiter.waiting == 0
    finally:
        # One aggregate ten-second settlement budget: release the held extraction, settle both
        # direct process tasks (cancelling any survivor), then stop the worker. stage_env deletes
        # the owned rows only after this, so no row is deleted under a live task.
        release_first.set()
        runs = [run for run in (first_run, second_run) if run is not None]
        async with asyncio.timeout(10):
            try:
                await asyncio.wait_for(asyncio.gather(*runs, return_exceptions=True), 5)
            finally:
                for run in runs:
                    if not run.done():
                        run.cancel()
                await asyncio.gather(*runs, return_exceptions=True)
                await worker.stop()
        assert all(run.done() for run in runs)
        assert not worker.tasks and not worker.job_task_contexts


async def test_cancelled_admission_waiter_settles_relay_and_keeps_limiter_capacity() -> None:
    limiter = ResizableLimiter(1)
    await limiter.acquire()
    job = AsyncMock()
    touch_started, touch_cancelled = asyncio.Event(), asyncio.Event()

    async def touch(**kwargs: Any) -> None:
        touch_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            touch_cancelled.set()

    job.update.side_effect = touch

    async def wait() -> None:
        async with functions._analysis_capacity(limiter, job, 0):
            pytest.fail("held permit must not be acquired")

    waiter = asyncio.create_task(wait())
    await asyncio.wait_for(touch_started.wait(), 5)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert touch_cancelled.is_set()
    assert limiter.waiting == 0 and limiter.borrowed_tokens == 1
    limiter.release()


async def test_admitted_capacity_stops_relay_before_work(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = ResizableLimiter(1)
    job = AsyncMock()
    monkeypatch.setattr(functions, "_HEARTBEAT_TOUCHES_PER_DEADLINE", 360000)
    async with functions._analysis_capacity(limiter, job, 3600):
        await asyncio.sleep(0.04)
        job.update.assert_not_awaited()
    assert limiter.borrowed_tokens == 0


@pytest.mark.integration
async def test_admitted_stall_remains_sweepable_with_real_worker_abort(stage_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _ = stage_env
    limiter = ResizableLimiter(1)
    admitted = asyncio.Event()
    cancelled = asyncio.Event()
    import saq.job
    import saq.queue.base

    clock = [saq.job.now()]
    monkeypatch.setattr(saq.job, "now", lambda: clock[0])
    monkeypatch.setattr(saq.queue.base, "now", lambda: clock[0])
    monkeypatch.setattr(functions, "_HEARTBEAT_TOUCHES_PER_DEADLINE", 360000)

    async def stalled(ctx: dict[str, Any]) -> None:
        async with functions._analysis_capacity(limiter, ctx["job"], 3600):
            admitted.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    worker = Worker(queue, functions=[("stalled", stalled)], concurrency=1, dequeue_timeout=0.1)
    job = await queue.enqueue("stalled", heartbeat=3600, timeout=0, retries=1)
    assert job is not None
    await job.update(retries=1)  # Explicit exhausted-retry case after production enqueue defaults.
    run = asyncio.create_task(worker.process())
    try:
        await asyncio.wait_for(admitted.wait(), 5)
        clock[0] += 3601 * 1000
        await asyncio.sleep(0.04)
        stored = await queue.job(job.key)
        assert stored is not None and stored.stuck
        sweep = asyncio.create_task(queue.sweep(abort=0.1))

        async def aborting() -> bool:
            stored = await queue.job(job.key)
            return stored is not None and stored.status == Status.ABORTING

        await _until(aborting)
        await worker.abort(0)
        assert await sweep == [job.key]
        await asyncio.wait_for(run, 5)
        stored = await queue.job(job.key)
        assert stored is not None and stored.status == Status.ABORTED and stored.attempts == 1 and stored.error == queue.swept_error_message
        assert cancelled.is_set() and limiter.borrowed_tokens == 0
    finally:
        if not run.done():
            run.cancel()
        await asyncio.wait_for(run, 5)


@pytest.mark.parametrize("cancel_requests", [1, 2])
async def test_parent_cancel_during_granted_permit_relay_cleanup_never_admits_work(cancel_requests: int) -> None:
    """Actual limiter grant races cancellation at pre-yield settlement, not while waiting."""
    limiter = ResizableLimiter(1)
    await limiter.acquire()
    touch_started, cleanup_started, release_cleanup, relay_finished = (asyncio.Event() for _ in range(4))
    work_started = asyncio.Event()
    job = AsyncMock()

    async def touch(**kwargs: Any) -> None:
        touch_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            finally:
                relay_finished.set()

    job.update.side_effect = touch

    async def analysis() -> None:
        async with functions._analysis_capacity(limiter, job, 0):
            work_started.set()

    parent = asyncio.create_task(analysis())
    try:
        await asyncio.wait_for(touch_started.wait(), 5)
        assert limiter.waiting == 1
        limiter.release()  # Actual FIFO grant; cancellation below happens AFTER acquisition.
        await asyncio.wait_for(cleanup_started.wait(), 5)
        assert limiter.borrowed_tokens == 1 and limiter.waiting == 0
        for _ in range(cancel_requests):
            parent.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)  # Let both relay and parent cancellation continuations run.
            assert not work_started.is_set()
            assert not relay_finished.is_set()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(parent, 5)
        assert relay_finished.is_set()
        assert not work_started.is_set()
        assert limiter.borrowed_tokens == 0 and limiter.waiting == 0
    finally:
        release_cleanup.set()
        if not parent.done():
            parent.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(parent, 5)
        if limiter.borrowed_tokens:
            limiter.release()
