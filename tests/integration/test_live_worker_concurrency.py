"""phaze-mvq8z.10: a running SAQ worker's concurrency changes live -- real SAQ, real Postgres broker.

``phaze.tasks._shared.live_worker`` hooks SAQ's private loop machinery, so a mocked ``Worker``
would prove nothing (CLAUDE.md, verification-fidelity rule 3). Everything here runs the INSTALLED
``saq.Worker.start()`` against a real ``PostgresQueue`` on the 5433 harness: real ``LISTEN``/
``NOTIFY`` wakeups, the real claim ``UPDATE ... LIMIT _waiting``, real done-callback relaunches.

Timing is gated, never slept on. Each job blocks on its own :class:`asyncio.Event` until the test
releases it, and "no more than N jobs ran" is asserted STRUCTURALLY -- the worker has exactly N job
loops, N jobs are running, and every other ``active`` row is a claim still sitting in the buffer
(see ``_assert_claims_are_running_or_buffered``) -- rather than by waiting a while to see whether
another job starts. The acceptance items, one test each:

* raising the target runs more jobs concurrently, immediately (nothing released first);
* lowering it lets the running jobs finish, then holds concurrency at the new target -- including
  the idle-loop case attrition alone gets wrong (an idle surplus loop would claim one more job);
* a reload through the real :class:`~phaze.runtime_config.RuntimeConfigStore` reaches a worker SAQ
  itself constructed, adopted from its startup hook as production does;
* SIGTERM still drains cleanly after a resize: running jobs finish inside the grace period, no row
  is left ``active``, no loop survives and none is relaunched.
"""

from __future__ import annotations

import asyncio
from collections import Counter
import contextlib
import itertools
import os
import signal
import time
from typing import TYPE_CHECKING, Any
import uuid

import pytest
import pytest_asyncio
from saq import Status, Worker
from saq.queue.postgres import DEQUEUE, PostgresQueue

from phaze.config import ControlSettings
from phaze.runtime_config import RUNTIME_TOML_NAME, RuntimeConfigStore
from phaze.tasks._shared.live_worker import APPLIER_NAME, LiveConcurrencyWorker, install_live_concurrency, lane_concurrency
from tests._async_settle import wait_until
from tests.db_guard import integration_dsns


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from pathlib import Path


pytestmark = pytest.mark.integration

BROKER_DSN, _ = integration_dsns()
#: A deadline for broker round-trips to settle, not a tuned margin: every wait below ends as soon as
#: its condition holds, and only a genuinely stuck worker ever reaches this.
SETTLE = 20.0


@pytest_asyncio.fixture
async def pg_queue() -> AsyncGenerator[PostgresQueue]:
    """A connected real ``PostgresQueue`` with a per-test-unique name; skipped if Postgres is down."""
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    queue = PostgresQueue.from_url(BROKER_DSN, name=f"itest-live-{uuid.uuid4().hex[:8]}")
    await queue.connect()
    try:
        yield queue
    finally:
        with contextlib.suppress(Exception):
            async with queue.pool.connection() as conn:
                await conn.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue.name,))
        await queue.disconnect()


class _GatedJobs:
    """The job body: record the start, block on this job's own gate, record the finish."""

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.running: set[str] = set()
        self.finished: list[str] = []
        #: Names whose outcome SAQ has WRITTEN to the broker: after_process runs after job.finish().
        self.settled: list[str] = []
        self.peak = 0

    def gate(self, name: str) -> asyncio.Event:
        return self.gates.setdefault(name, asyncio.Event())

    def reset_peak(self) -> None:
        self.peak = len(self.running)

    def release(self, *names: str) -> None:
        for name in names:
            self.gate(name).set()

    def release_all(self) -> None:
        for gate in self.gates.values():
            gate.set()

    async def run(self, _ctx: dict[str, Any], *, name: str) -> str:
        self.running.add(name)
        self.peak = max(self.peak, len(self.running))
        try:
            await self.gate(name).wait()
        finally:
            self.running.discard(name)
        self.finished.append(name)
        return name

    async def after_process(self, ctx: dict[str, Any]) -> None:
        self.settled.append(ctx["job"].kwargs["name"])


async def _enqueue(queue: PostgresQueue, *names: str) -> None:
    for name in names:
        # timeout=0: SAQ's default 10 s job timeout would cancel a gated job the test is holding.
        await queue.enqueue("gated", key=f"{queue.name}:{name}", name=name, timeout=0)


async def _statuses(queue: PostgresQueue) -> dict[str, int]:
    async with queue.pool.connection() as conn:
        cursor = await conn.execute("SELECT status, count(*) FROM saq_jobs WHERE queue = %s GROUP BY status", (queue.name,))
        return {str(status): int(count) for status, count in await cursor.fetchall()}


async def _assert_claims_are_running_or_buffered(queue: PostgresQueue, jobs: _GatedJobs, *, total: int) -> None:
    """Every ``active`` row is a job a loop is running, or one claimed into the buffer awaiting a loop.

    Not simply ``active == running``. Observed in a full-suite gate at concurrency 1: two rows
    ``active`` while one job ran. SAQ can do this on its own: the once-a-second schedule upkeep
    claims up to ``_waiting`` rows (``PostgresQueue.schedule`` -> ``_dequeue``), and a claim can land
    after a listening loop's claim but before that loop's ``dequeue()`` returns and stops being
    counted. The extra row goes to the buffer. That mechanism is read from the installed source,
    not caught in the act -- which is why this asserts the general property (every active row is
    running or buffered) rather than a count that assumes the mechanism never fires. A buffered row
    cannot start until a loop frees up, so the concurrency claims these tests make are unaffected.
    """
    statuses = await _statuses(queue)
    active = statuses.get("active", 0)
    assert active == len(jobs.running) + queue._job_queue.qsize(), (statuses, jobs.running, queue._job_queue.qsize())
    assert active + statuses.get("queued", 0) + statuses.get("complete", 0) == total, statuses


def _worker(queue: PostgresQueue, jobs: _GatedJobs, *, concurrency: int, **kwargs: Any) -> LiveConcurrencyWorker:
    return LiveConcurrencyWorker(queue, functions=[("gated", jobs.run)], concurrency=concurrency, after_process=jobs.after_process, **kwargs)


@contextlib.asynccontextmanager
async def _running(worker: Worker[Any], jobs: _GatedJobs) -> AsyncIterator[asyncio.Task[None]]:
    """Run ``worker.start()`` for the block; on the way out release every job and stop the worker."""
    task = asyncio.create_task(worker.start())
    try:
        yield task
    finally:
        jobs.release_all()
        worker.event.set()
        await asyncio.wait_for(task, SETTLE)


async def test_raising_the_target_runs_more_jobs_at_once_immediately(pg_queue: PostgresQueue) -> None:
    jobs = _GatedJobs()
    worker = _worker(pg_queue, jobs, concurrency=1)
    async with _running(worker, jobs):
        await _enqueue(pg_queue, "a", "b", "c")
        await wait_until(lambda: len(jobs.running) == 1, timeout=SETTLE, description="one job running at concurrency 1")
        assert worker.live_loops == 1
        await _assert_claims_are_running_or_buffered(pg_queue, jobs, total=3)

        worker.set_concurrency(3)

        # Nothing was released: the two extra jobs can only have started on the new loops.
        await wait_until(lambda: len(jobs.running) == 3, timeout=SETTLE, description="all three jobs running after the grow")
        assert worker.live_loops == 3
        assert jobs.finished == []
        assert await _statuses(pg_queue) == {"active": 3}


async def test_lowering_the_target_lets_running_jobs_finish_then_holds_it(pg_queue: PostgresQueue) -> None:
    jobs = _GatedJobs()
    worker = _worker(pg_queue, jobs, concurrency=3)
    async with _running(worker, jobs):
        await _enqueue(pg_queue, "r1", "r2", "r3")
        await wait_until(lambda: len(jobs.running) == 3, timeout=SETTLE, description="three jobs running at concurrency 3")

        worker.set_concurrency(1)
        await _enqueue(pg_queue, "n1", "n2", "n3")

        # The shrink cancelled nothing: all three are still running, and the new work waits.
        assert jobs.running == {"r1", "r2", "r3"}
        assert jobs.finished == []

        jobs.release("r1", "r2", "r3")
        await wait_until(lambda: {"r1", "r2", "r3"} <= set(jobs.settled), timeout=SETTLE, description="the running jobs finish")
        await wait_until(lambda: len(jobs.running) == 1, timeout=SETTLE, description="one new job running")
        jobs.reset_peak()

        # Held at the target, structurally: one loop exists, so one row can be active.
        for remaining in (3, 2, 1):
            assert worker.live_loops == 1
            await _assert_claims_are_running_or_buffered(pg_queue, jobs, total=6)
            (current,) = jobs.running
            jobs.release(current)
            await wait_until(lambda current=current: current in jobs.settled, timeout=SETTLE, description=f"{current} finishes")
            if remaining > 1:
                await wait_until(lambda: len(jobs.running) == 1, timeout=SETTLE, description="the next job starts")

        assert jobs.peak == 1
        assert sorted(jobs.finished) == ["n1", "n2", "n3", "r1", "r2", "r3"]
        assert await _statuses(pg_queue) == {"complete": 6}


async def test_lowering_the_target_trims_idle_loops_before_they_claim_work(pg_queue: PostgresQueue) -> None:
    """The half attrition misses: an idle surplus loop is a counted dequeue waiter, so it WOULD claim a job."""
    jobs = _GatedJobs()
    # SAQ's schedule upkeep also claims work, once a second (PostgresQueue.schedule -> _dequeue). Pushed
    # out of the test's reach so the only wakeup left is the LISTEN one the trim must not lose.
    worker = _worker(pg_queue, jobs, concurrency=4, timers={"schedule": 3600})
    async with _running(worker, jobs):
        await wait_until(lambda: len(worker._dequeuing) == 4, timeout=SETTLE, description="four idle loops waiting in dequeue")

        worker.set_concurrency(1)
        await wait_until(lambda: worker.live_loops == 1 and len(worker._dequeuing) == 1, timeout=SETTLE, description="one idle loop left waiting")
        assert pg_queue._waiting == 1  # what the next claim's LIMIT will be

        await _enqueue(pg_queue, "a", "b", "c")
        await wait_until(lambda: len(jobs.running) == 1, timeout=SETTLE, description="one job running")
        assert worker.live_loops == 1
        await _assert_claims_are_running_or_buffered(pg_queue, jobs, total=3)

        for _ in range(3):
            (current,) = jobs.running
            jobs.release(current)
            await wait_until(lambda current=current: current in jobs.settled, timeout=SETTLE, description=f"{current} finishes")
            if len(jobs.finished) < 3:
                await wait_until(lambda: len(jobs.running) == 1, timeout=SETTLE, description="the next job starts")
                assert worker.live_loops == 1

        assert jobs.peak == 1
        assert await _statuses(pg_queue) == {"complete": 3}


async def test_a_reload_resizes_a_worker_saq_constructed_and_the_startup_hook_adopted(pg_queue: PostgresQueue, tmp_path: Path) -> None:
    """Production's path end to end: a plain ``saq.Worker``, adopted in startup, resized by a real reload."""
    jobs = _GatedJobs()
    store = RuntimeConfigStore(ControlSettings(), runtime_toml=tmp_path / RUNTIME_TOML_NAME, env={}, physical_cores=lambda: 64)
    installed: list[LiveConcurrencyWorker | None] = []

    async def startup(ctx: dict[str, Any]) -> None:
        installed.append(install_live_concurrency(ctx, store, lambda config: lane_concurrency(config, "meta")))

    (tmp_path / RUNTIME_TOML_NAME).write_text("worker_max_jobs = 8\nlane_meta_concurrency = 1\n", encoding="utf-8")
    await store.reload("startup")

    worker: Worker[Any] = Worker(pg_queue, functions=[("gated", jobs.run)], concurrency=5, startup=startup, after_process=jobs.after_process)
    async with _running(worker, jobs):
        await _enqueue(pg_queue, "a", "b", "c")
        await wait_until(lambda: len(jobs.running) == 1, timeout=SETTLE, description="one job running")
        assert installed == [worker]
        assert isinstance(worker, LiveConcurrencyWorker)
        # The startup-time snapshot, not SAQ's constructor argument, sized the first spawn.
        assert worker.live_loops == 1

        (tmp_path / RUNTIME_TOML_NAME).write_text("worker_max_jobs = 8\nlane_meta_concurrency = 3\n", encoding="utf-8")
        result = await store.reload("file")
        assert result.outcome == "applied", result
        assert APPLIER_NAME not in result.applier_errors

        await wait_until(lambda: len(jobs.running) == 3, timeout=SETTLE, description="three jobs running after the reload")

        # worker_max_jobs is still the ceiling: min(lane knob, worker_max_jobs).
        (tmp_path / RUNTIME_TOML_NAME).write_text("worker_max_jobs = 2\nlane_meta_concurrency = 3\n", encoding="utf-8")
        assert (await store.reload("file")).outcome == "applied"
        assert worker.concurrency == 2


#: Every upkeep poll sleeps its own timer between checks of the stop event, and ``stop()`` waits
#: for them too -- the sweep's default 60 s would outlast any sane grace period. One second each
#: keeps shutdown's wait about the JOBS, which is what these tests are measuring.
_FAST_UPKEEP = {"schedule": 1, "worker_info": 1, "sweep": 1, "abort": 1}


async def _grow_shrink_then_sigterm(worker: LiveConcurrencyWorker, jobs: _GatedJobs, queue: PostgresQueue) -> asyncio.Task[None]:
    """Start the worker, resize it both ways with jobs in flight, then deliver a real SIGTERM."""
    task = asyncio.create_task(worker.start())
    await _enqueue(queue, "a", "b", "c")
    await wait_until(lambda: len(jobs.running) == 1, timeout=SETTLE, description="one job running")
    worker.set_concurrency(3)
    await wait_until(lambda: len(jobs.running) == 3, timeout=SETTLE, description="three jobs running after the grow")
    worker.set_concurrency(2)
    # The real signal, through the handler Worker.start() installed (saq/worker.py:191-192).
    os.kill(os.getpid(), signal.SIGTERM)
    await wait_until(worker.event.is_set, timeout=SETTLE, description="SIGTERM reached the worker")
    return task


async def test_sigterm_after_a_resize_drains_running_jobs_within_the_grace_period(pg_queue: PostgresQueue) -> None:
    jobs = _GatedJobs()
    worker = _worker(pg_queue, jobs, concurrency=1, shutdown_grace_period_s=int(SETTLE), timers=_FAST_UPKEEP)
    task = await _grow_shrink_then_sigterm(worker, jobs, pg_queue)
    try:
        # Shutdown cancels nothing inside the grace period: released now, every job completes.
        jobs.release_all()
        await asyncio.wait_for(task, 2 * SETTLE)
    finally:
        jobs.release_all()
        worker.event.set()
        await asyncio.wait_for(task, 2 * SETTLE)

    assert sorted(jobs.finished) == ["a", "b", "c"]
    assert await _statuses(pg_queue) == {"complete": 3}
    assert worker.live_loops == 0
    assert worker.tasks == set()


async def test_sigterm_after_a_resize_with_no_grace_requeues_running_jobs(pg_queue: PostgresQueue) -> None:
    """Production's shape: the workers set no grace, so SAQ cancels in-flight jobs and retries them.

    What must still hold after a resize: every cancelled job goes back to ``queued`` for the next
    worker (none stranded ``active``, the phaze-o0n6 failure), and no loop is relaunched.
    """
    jobs = _GatedJobs()
    worker = _worker(pg_queue, jobs, concurrency=1, timers=_FAST_UPKEEP)
    task = await _grow_shrink_then_sigterm(worker, jobs, pg_queue)
    try:
        await asyncio.wait_for(task, 2 * SETTLE)
    finally:
        jobs.release_all()
        worker.event.set()
        await asyncio.wait_for(task, 2 * SETTLE)

    assert jobs.finished == []
    assert await _statuses(pg_queue) == {"queued": 3}
    assert worker.live_loops == 0
    assert worker.tasks == set()


async def test_no_job_is_lost_or_run_twice_across_repeated_resizes(pg_queue: PostgresQueue) -> None:
    """The idle trim cancels dequeue waiters; a bad cancel strands a claimed row or double-runs one.

    Three rounds. In each, a batch drains while EVERY finished job resizes the worker to the next
    target in a cycle that grows and shrinks (so resizes land mid-drain, with loops busy, idle and
    freshly claimed), then -- with the queue empty and every loop parked in dequeue -- the worker is
    grown, shrunk to one and grown again. Every job must run exactly once and reach ``complete``;
    no row may be left ``active`` or ``queued``.
    """
    runs: Counter[str] = Counter()
    settled: list[str] = []
    targets = itertools.cycle((1, 5, 2, 7, 1, 3, 6, 2))
    worker: LiveConcurrencyWorker

    async def quick(_ctx: dict[str, Any], *, name: str) -> str:
        runs[name] += 1
        for _ in range(3):  # yield, so other loops' claims and resizes interleave with this one
            await asyncio.sleep(0)
        return name

    async def after_process(ctx: dict[str, Any]) -> None:
        settled.append(ctx["job"].kwargs["name"])
        worker.set_concurrency(next(targets))

    worker = LiveConcurrencyWorker(pg_queue, functions=[("quick", quick)], concurrency=4, after_process=after_process, timers=_FAST_UPKEEP)
    names: list[str] = []
    task = asyncio.create_task(worker.start())
    try:
        for round_ in range(3):
            batch = [f"r{round_}-{i:02d}" for i in range(15)]
            names += batch
            for name in batch:
                await pg_queue.enqueue("quick", key=f"{pg_queue.name}:{name}", name=name, timeout=0)
            await wait_until(lambda batch=batch: set(batch) <= set(settled), timeout=SETTLE, description=f"round {round_} drains")

            # The queue is empty: every loop is an idle dequeue waiter.
            worker.set_concurrency(6)
            await wait_until(lambda: len(worker._dequeuing) == 6, timeout=SETTLE, description="six idle loops")
            worker.set_concurrency(1)
            await wait_until(lambda: worker.live_loops == 1 and len(worker._dequeuing) == 1, timeout=SETTLE, description="trimmed to one")
            worker.set_concurrency(3)
            await wait_until(lambda: len(worker._dequeuing) == 3, timeout=SETTLE, description="three idle loops")
    finally:
        worker.event.set()
        await asyncio.wait_for(task, 2 * SETTLE)

    assert runs == Counter(dict.fromkeys(names, 1))
    assert sorted(settled) == sorted(names)
    assert await _statuses(pg_queue) == {"complete": len(names)}


async def test_the_trim_never_cancels_the_waiter_a_buffered_claim_belongs_to(pg_queue: PostgresQueue) -> None:
    """The ``_job_queue.empty()`` guard: a row claimed into the buffer must reach a loop, not be stranded.

    ``PostgresQueue._dequeue`` marks rows ``active`` in the database, puts them in the in-memory
    buffer and then NOTIFYs ``DEQUEUE`` so the listening waiter takes them. That is reproduced here
    by hand, at the one instant it matters: a shrink's trim is already scheduled when the claimed
    row lands. The two busy loops cover the new target, so nothing would relaunch a cancelled
    waiter -- a trim that cancelled it anyway would leave the row ``active`` with no loop coming for
    it, which is the phaze-o0n6 stranded row. With the guard, the waiter takes it; production's
    zero-grace shutdown then retries all three, and nothing is left ``active``.
    """
    jobs = _GatedJobs()
    worker = _worker(pg_queue, jobs, concurrency=3, timers=_FAST_UPKEEP)
    task = asyncio.create_task(worker.start())
    try:
        await _enqueue(pg_queue, "a", "b")
        await wait_until(lambda: len(jobs.running) == 2 and len(worker._dequeuing) == 1, timeout=SETTLE, description="two busy, one idle")

        # A row claimed the way _dequeue claims it: ineligible to be claimed again (future
        # `scheduled`), marked active in the database, then handed to the buffer.
        await pg_queue.enqueue("gated", key=f"{pg_queue.name}:c", name="c", timeout=0, scheduled=int(time.time()) + 3600)
        claimed = await pg_queue.job(f"{pg_queue.name}:c")
        assert claimed is not None
        await claimed.update(status=Status.ACTIVE)

        worker.set_concurrency(1)  # schedules the trim ...
        pg_queue._job_queue.put_nowait(claimed)  # ... and the claim lands before it runs
        await pg_queue._notify(DEQUEUE)

        await wait_until(lambda: "c" in jobs.running, timeout=SETTLE, description="the buffered claim reaches a loop")
        assert await _statuses(pg_queue) == {"active": 3}
    finally:
        worker.event.set()
        await asyncio.wait_for(task, 2 * SETTLE)
        jobs.release_all()

    assert await _statuses(pg_queue) == {"queued": 3}
