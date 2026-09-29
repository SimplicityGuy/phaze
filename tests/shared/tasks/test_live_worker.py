"""``phaze.tasks._shared.live_worker`` edges that need no broker (phaze-mvq8z.10).

The grow / shrink / idle-trim / shutdown behaviour is proven on a real SAQ worker and a real
Postgres broker in ``tests/integration/test_live_worker_concurrency.py``; the SAQ internals it
leans on are pinned in ``tests/shared/tasks/test_saq_worker_contract.py``. This module covers the
guard rails around them: what adoption refuses, what a resize does before dispatch or in burst
mode, the no-worker startup path, and the trim's wait for a buffered claim.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from saq import Worker
from saq.queue.postgres import PostgresQueue

from phaze.runtime_config import RuntimeConfig
from phaze.tasks._shared import live_worker
from phaze.tasks._shared.live_worker import APPLIER_NAME, PROCESS_TASK_NAME, LiveConcurrencyWorker, install_live_concurrency, lane_concurrency


def _queue() -> PostgresQueue:
    # from_url opens no connection, so nothing here touches a database.
    return PostgresQueue.from_url("postgresql://unit@localhost:1/unit", name="unit")


def _config(**overrides: Any) -> RuntimeConfig:
    values: dict[str, Any] = {
        "log_level": "INFO",
        "worker_max_jobs": 4,
        "lane_analyze_concurrency": 2,
        "lane_meta_concurrency": 6,
        "lane_io_concurrency": 3,
        "worker_process_pool_size": 2,
        "analysis_intra_op_threads": 1,
        "analysis_omp_threads": 1,
        "analysis_stall_timeout_sec": 1800,
        "cloud_route_threshold_sec": 3600,
    }
    return RuntimeConfig.model_validate(values | overrides)


class _Store:
    """The two things install_live_concurrency uses from a RuntimeConfigStore."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.appliers: dict[str, Any] = {}

    def current(self) -> RuntimeConfig:
        return self.config

    def register_applier(self, name: str, applier: Any) -> None:
        self.appliers[name] = applier


def _parked_loop(worker: LiveConcurrencyWorker, release: asyncio.Event, *, idle: bool) -> asyncio.Task[None]:
    """A stand-in job loop: named like SAQ's, counted in ``worker.tasks``, optionally 'in dequeue'."""
    task = asyncio.create_task(release.wait(), name=PROCESS_TASK_NAME)
    worker.tasks.add(task)
    if idle:
        worker._dequeuing.add(task)
    return task


@pytest.mark.parametrize(
    ("lane", "expected"),
    [
        (None, 4),  # all-mode: worker_max_jobs
        ("nope", 4),  # an unrecognised suffix: worker_max_jobs
        ("analyze", 2),  # the lane knob, under the ceiling
        ("meta", 4),  # the lane knob, clamped by worker_max_jobs
    ],
)
def test_lane_concurrency_is_the_lane_knob_clamped_by_worker_max_jobs(lane: str | None, expected: int) -> None:
    assert lane_concurrency(_config(), lane) == expected


def test_adopting_an_adopted_worker_returns_it_unchanged() -> None:
    worker = LiveConcurrencyWorker(_queue(), functions=[])
    assert LiveConcurrencyWorker.adopt(worker) is worker


def test_only_a_plain_saq_worker_can_be_adopted() -> None:
    class _Other(Worker[Any]):
        pass

    with pytest.raises(TypeError, match=r"only a plain saq\.Worker"):
        LiveConcurrencyWorker.adopt(_Other(_queue(), functions=[]))


async def test_a_worker_that_is_already_dispatching_is_not_adopted() -> None:
    worker: Worker[Any] = Worker(_queue(), functions=[])
    loop = asyncio.create_task(asyncio.sleep(0), name=PROCESS_TASK_NAME)
    worker.tasks.add(loop)
    with pytest.raises(RuntimeError, match="already dispatching"):
        LiveConcurrencyWorker.adopt(worker)
    assert type(worker) is Worker
    await loop


def test_a_target_below_one_is_refused() -> None:
    worker = LiveConcurrencyWorker(_queue(), functions=[], concurrency=2)
    with pytest.raises(ValueError, match="at least 1"):
        worker.set_concurrency(0)
    assert worker.concurrency == 2


def test_before_dispatch_a_resize_only_sets_the_target_start_will_read() -> None:
    worker = LiveConcurrencyWorker(_queue(), functions=[], concurrency=2)
    worker.set_concurrency(5)
    worker.set_concurrency(5)  # unchanged: nothing to report, nothing to do
    assert worker.concurrency == 5
    assert worker.live_loops == 0


async def test_burst_mode_keeps_its_own_loop_rules() -> None:
    """Burst mode stops the worker when its loops run out, so a live target stays out of its way."""
    worker = LiveConcurrencyWorker(_queue(), functions=[], concurrency=1, burst=True, dequeue_timeout=1.0)
    worker._dispatching = True
    release = asyncio.Event()
    loop = _parked_loop(worker, release, idle=False)

    worker.set_concurrency(3)
    assert worker.live_loops == 1  # no loop spawned
    worker.concurrency = 0  # even a surplus is not retired: SAQ's burst rule decides
    assert worker._retire(loop) is False

    release.set()
    await loop


async def test_a_loop_is_never_retired_once_the_worker_is_stopping() -> None:
    worker = LiveConcurrencyWorker(_queue(), functions=[], concurrency=1)
    release = asyncio.Event()
    loops = [_parked_loop(worker, release, idle=False) for _ in range(2)]
    worker.event.set()
    assert worker._retire(loops[0]) is False  # SAQ's own _process then discards it without a relaunch
    release.set()
    await asyncio.gather(*loops)


async def test_start_and_grow_never_spawn_past_the_target() -> None:
    worker = LiveConcurrencyWorker(_queue(), functions=[], concurrency=1)
    release = asyncio.Event()
    loop = _parked_loop(worker, release, idle=False)
    worker._process()  # what start()'s spawn loop calls: the target is already covered
    assert worker._dispatching is True
    assert worker.live_loops == 1
    release.set()
    await loop


def test_no_worker_in_ctx_leaves_concurrency_fixed() -> None:
    store = _Store(_config())
    assert install_live_concurrency({}, store, lambda config: config.worker_max_jobs) is None
    assert store.appliers == {}


def test_install_adopts_the_ctx_worker_sizes_it_and_registers_the_applier() -> None:
    worker: Worker[Any] = Worker(_queue(), functions=[], concurrency=9)
    store = _Store(_config())
    live = install_live_concurrency(worker.context, store, lambda config: lane_concurrency(config, "analyze"))

    assert live is worker
    assert isinstance(worker, LiveConcurrencyWorker)
    assert worker.concurrency == 2  # the snapshot's value, not SAQ's constructor argument
    store.appliers[APPLIER_NAME](store.config, _config(lane_analyze_concurrency=3))
    assert worker.concurrency == 3


async def test_the_trim_leaves_a_queue_it_cannot_inspect_alone() -> None:
    class _NotPostgres:
        pass

    worker = LiveConcurrencyWorker(_NotPostgres(), functions=[], concurrency=1)
    release = asyncio.Event()
    loops = [_parked_loop(worker, release, idle=True) for _ in range(2)]
    await worker._trim_idle()
    assert not any(loop.cancelled() or loop.cancelling() for loop in loops)
    release.set()
    await asyncio.gather(*loops)


async def test_the_trim_waits_for_a_buffered_claim_before_cancelling_idle_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """A claimed row in the buffer belongs to a waiter: cancel nothing until it has been taken."""
    queue = _queue()
    worker = LiveConcurrencyWorker(queue, functions=[], concurrency=1)
    release = asyncio.Event()
    loops = [_parked_loop(worker, release, idle=True) for _ in range(3)]
    queue._job_queue.put_nowait(object())
    buffered_waits: list[None] = []
    real_join = queue._job_queue.join

    async def observed_join() -> None:
        buffered_waits.append(None)
        await real_join()

    monkeypatch.setattr(queue._job_queue, "join", observed_join)
    trim = asyncio.create_task(worker._trim_idle())
    while not buffered_waits:
        await asyncio.sleep(0)
    assert not any(loop.cancelling() for loop in loops)

    queue._job_queue.get_nowait()  # a waiter takes its row ...
    queue._job_queue.task_done()  # ... as dequeue() does
    await trim
    assert all(loop.cancelling() for loop in loops)
    await asyncio.gather(*loops, return_exceptions=True)


async def test_the_trim_does_nothing_once_the_surplus_is_gone() -> None:
    worker = LiveConcurrencyWorker(_queue(), functions=[], concurrency=2)
    release = asyncio.Event()
    loop = _parked_loop(worker, release, idle=True)
    await worker._trim_idle()
    assert not loop.cancelling()
    release.set()
    await loop


def test_the_module_exports_its_public_names() -> None:
    assert set(live_worker.__all__) == {"APPLIER_NAME", "PROCESS_TASK_NAME", "LiveConcurrencyWorker", "install_live_concurrency", "lane_concurrency"}
