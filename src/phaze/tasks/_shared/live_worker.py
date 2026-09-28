"""A SAQ ``Worker`` whose concurrency can change while it runs (``phaze-mvq8z.10``).

The design and the operator decision behind it are
``docs/design/0019-runtime-config-hot-reload.md`` §8. SAQ 0.26.4 has no supported way to change a
running worker's concurrency: ``Worker.start()`` spawns ``concurrency`` self-perpetuating
``_process()`` loops ONCE (``saq/worker.py:200-201``), each loop's ``process()`` task relaunches
itself from its own done-callback (``saq/worker.py:440-453``), and nothing reads ``concurrency``
again. :class:`LiveConcurrencyWorker` treats ``concurrency`` as a live TARGET instead:

* **Grow** spawns the missing loops at once, so the new capacity dequeues immediately.
* **Shrink** never cancels a running job. A loop whose job has just finished stops relaunching
  while the other loops already cover the target -- attrition, the same shape as
  :mod:`phaze.services.resizable_limiter`'s shrink.
* **Shrink also cancels the IDLE loops**, the half that attrition alone gets wrong. An idle loop
  is parked in ``PostgresQueue.dequeue()`` and counted in its ``_waiting``, which is the ``LIMIT``
  of the claim ``UPDATE`` (``saq/queue/postgres.py:636-690``); left alone, every surplus idle loop
  would claim and run one more job, so a lowered target would not hold. Cancelling one is safe
  only while nobody is inside ``_dequeue`` (the claim transaction, which puts rows into the
  in-memory ``_job_queue`` one by one) and no claimed row is buffered. :meth:`_trim_idle` holds
  the queue's own ``_dequeue_lock`` and checks ``_job_queue.empty()`` before cancelling, with no
  ``await`` between the check and the cancels; ``dequeue()`` swallows the cancellation and
  returns ``None`` (``saq/queue/postgres.py:581-634``), so the loop ends without a job. ALL idle
  loops are cancelled, not just the surplus: one of them is the queue's single ``LISTEN`` waiter,
  which cannot be told apart from the others, and the rest wait on the buffer only it fills on an
  ``ENQUEUE`` notification. Cancel it alone and a new job waits for SAQ's once-a-second schedule
  upkeep to claim it (``PostgresQueue.schedule`` -> ``_dequeue``, ``saq/queue/postgres.py:353-355``)
  -- measured: with that upkeep pushed out, a job enqueued after cancelling only the listener was
  never picked up. The retire rule then relaunches exactly as many loops as the target needs, and
  a relaunched loop takes over the listen.

How the subclass gets in: SAQ's CLI (``saq <module>.settings``, which every compose file and the
arm64 image run) constructs a plain ``saq.Worker(**settings)`` and has no worker-class hook
(``saq/worker.py:541``). :func:`install_live_concurrency` therefore ADOPTS that instance from the
SAQ startup hook -- ``ctx["worker"]`` is the worker itself (``saq/worker.py:132``), and startup runs
before the loops are spawned (``saq/worker.py:194-201``) -- rather than changing every entry point
and every operator's ``PHAZE_CLOUD_AGENT_CMD`` override. Implementer decision (phaze-mvq8z.10).

Every SAQ internal named here is private and unversioned. SAQ is pinned to its minor line in
``pyproject.toml`` and ``tests/shared/tasks/test_saq_worker_contract.py`` fails on any installed
version or internal shape this module was not verified against: a SAQ bump is a compatibility
review of this module, not a routine dependency update (ADR §8).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Final, cast

from saq import Worker
from saq.queue.postgres import PostgresQueue
import structlog

from phaze.services.enqueue_router import LANE_CONCURRENCY_SETTING


if TYPE_CHECKING:
    from collections.abc import Callable

    from saq import Job, Queue

    from phaze.runtime_config import RuntimeConfig, RuntimeConfigStore


logger = structlog.get_logger(__name__)

#: The name SAQ gives every job loop's task (``saq/worker.py:451``) and itself counts loops by
#: (``saq/worker.py:445``). Upkeep tasks are unnamed, so this is what tells a loop apart.
PROCESS_TASK_NAME: Final = "process"
#: The runtime-config applier this module registers.
APPLIER_NAME: Final = "saq_worker_concurrency"
_TRIM_TASK_NAME: Final = "live-concurrency-trim"
#: How long a trim waits for a claimed-but-buffered row to be picked up before looking again.
_TRIM_RETRY_SECONDS: Final = 1.0


def lane_concurrency(config: RuntimeConfig, lane: str | None) -> int:
    """The SAQ concurrency a worker for ``lane`` runs at under ``config``.

    ``min(lane knob, worker_max_jobs)``: the quick-260707-g84 memory ceiling, where an explicit
    lower ``worker_max_jobs`` is authoritative over the per-lane knob. An unlaned (all-mode) or
    unrecognised lane runs at ``worker_max_jobs``. One definition, read by the worker that sizes
    itself and by :func:`phaze.services.queue_introspection.concurrency_for_queue`, whose
    stranded-row guard is only as honest as its idea of this number (phaze-o0n6).
    """
    attr = LANE_CONCURRENCY_SETTING.get(lane) if lane is not None else None
    if attr is None:
        return config.worker_max_jobs
    return min(int(getattr(config, attr)), config.worker_max_jobs)


class _DequeueTracking:
    """This worker's view of its queue: every attribute delegated, ``dequeue`` also recorded.

    A task is in ``dequeuing`` exactly while it is suspended inside ``queue.dequeue()`` -- the one
    place a loop holds no job. It leaves the set in the same step ``dequeue()`` returns in, so no
    loop is ever in the set while it holds a claimed job. Scoped to this worker: the queue object
    itself, which the module that built it may share, is untouched.
    """

    def __init__(self, queue: Queue, dequeuing: set[asyncio.Task[Any]]) -> None:
        self._queue = queue
        self._dequeuing = dequeuing

    @property
    def wrapped(self) -> Queue:
        return self._queue

    def __getattr__(self, name: str) -> Any:
        return getattr(self._queue, name)

    def __repr__(self) -> str:
        return repr(self._queue)

    async def dequeue(self, timeout: float = 0.0, poll_interval: float = 0.0) -> Job | None:
        # Always inside a task: Worker.process() awaits this from the loop's own process() task.
        task = cast("asyncio.Task[Any]", asyncio.current_task())
        self._dequeuing.add(task)
        try:
            return await self._queue.dequeue(timeout=timeout, poll_interval=poll_interval)
        finally:
            self._dequeuing.discard(task)


class LiveConcurrencyWorker(Worker[Any]):
    """A SAQ worker whose ``concurrency`` is a live target -- see the module docstring."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._init_live_concurrency()

    @classmethod
    def adopt(cls, worker: Worker[Any]) -> LiveConcurrencyWorker:
        """Turn a plain ``saq.Worker`` that has not started dispatching into this class, in place.

        Only an exact ``saq.Worker`` is accepted: its instance layout is this class's by
        construction, which no other subclass can promise. Adopting after the loops exist would
        leave them uncounted by nothing and unbounded by the target, so that is refused too.
        """
        if isinstance(worker, cls):
            return worker
        if type(worker) is not Worker:
            raise TypeError(f"only a plain saq.Worker can be adopted, not {type(worker).__qualname__}")
        if any(task.get_name() == PROCESS_TASK_NAME for task in worker.tasks):
            raise RuntimeError("the worker is already dispatching; adopt it from the SAQ startup hook")
        worker.__class__ = cls
        adopted = cast("LiveConcurrencyWorker", worker)
        adopted._init_live_concurrency()
        return adopted

    def _init_live_concurrency(self) -> None:
        self._dispatching = False
        self._dequeuing: set[asyncio.Task[Any]] = set()
        self._tracked_queue = _DequeueTracking(self.queue, self._dequeuing)
        self.queue = cast("Queue", self._tracked_queue)

    @property
    def live_loops(self) -> int:
        """Job loops that exist right now: running a job, waiting in dequeue, or about to relaunch."""
        return sum(1 for task in self.tasks if task.get_name() == PROCESS_TASK_NAME)

    def set_concurrency(self, target: int) -> None:
        """Make ``target`` the number of job loops: grow now, shrink without cancelling a job."""
        if target < 1:
            raise ValueError(f"worker concurrency must be at least 1, got {target}")
        previous = self.concurrency
        self.concurrency = target
        if target != previous:
            logger.info("phaze.saq_worker concurrency target changed", previous=previous, target=target, live_loops=self.live_loops)
        # Before start() spawns its loops (the startup hook, where adopt runs), the target is all
        # there is: start() reads it. After stop(), nothing may be spawned. Burst mode has its own
        # stop rule keyed on the loop count, which a live target would fight.
        if not self._dispatching or self.event.is_set() or self.burst:
            return
        while self.live_loops < self.concurrency:
            super()._process()
        if self.live_loops > self.concurrency:
            self._schedule_trim()

    def _process(self, previous_task: asyncio.Task[Any] | None = None) -> None:
        if previous_task is None:
            # start()'s spawn loop -- which runs only after every startup hook -- is the only
            # caller without a previous task, so from here on a grow may spawn directly.
            self._dispatching = True
            if self.live_loops >= self.concurrency:
                return
        elif self._retire(previous_task):
            return
        super()._process(previous_task)

    def _retire(self, previous_task: asyncio.Task[Any]) -> bool:
        """Stop ``previous_task``'s loop instead of relaunching it, if the others cover the target."""
        if self.burst or self.event.is_set():
            return False
        others = sum(1 for task in self.tasks if task is not previous_task and task.get_name() == PROCESS_TASK_NAME)
        if others < self.concurrency:
            return False
        self.tasks.discard(previous_task)
        return True

    def _schedule_trim(self) -> None:
        trim = asyncio.create_task(self._trim_idle(), name=_TRIM_TASK_NAME)
        # In self.tasks so stop() waits for or cancels it like any upkeep task; it is cancel-safe
        # everywhere it can be suspended (the lock acquire and the buffer wait).
        self.tasks.add(trim)
        trim.add_done_callback(self.tasks.discard)

    async def _trim_idle(self) -> None:
        """Cancel every idle loop, once no claim is in flight and no claimed row is buffered."""
        queue = self._tracked_queue.wrapped
        if not isinstance(queue, PostgresQueue):
            logger.warning("phaze.saq_worker idle loops cannot be trimmed on this queue; they retire after their next job", queue=repr(queue))
            return
        while not self.event.is_set() and self.live_loops > self.concurrency:
            async with queue._dequeue_lock:
                if queue._job_queue.empty():
                    idle = [task for task in self._dequeuing if task in self.tasks]
                    for task in idle:
                        task.cancel()
                    logger.info("phaze.saq_worker cancelled idle loops", cancelled=len(idle), target=self.concurrency)
                    return
            # A claimed row is buffered; the waiter it was claimed for takes it on its next step.
            # Wait for that rather than cancel the waiter out from under it, then look again.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(queue._job_queue.join(), timeout=_TRIM_RETRY_SECONDS)


def install_live_concurrency(
    ctx: dict[str, Any],
    store: RuntimeConfigStore,
    resolve: Callable[[RuntimeConfig], int],
) -> LiveConcurrencyWorker | None:
    """Adopt ``ctx["worker"]``, size it from the current snapshot, and keep it sized on reloads.

    Call from a SAQ startup hook, after the store's startup reload. ``resolve`` maps a snapshot
    to the concurrency this worker should run at (:func:`lane_concurrency` for an agent lane,
    ``worker_max_jobs`` for the control worker). Returns ``None`` -- concurrency stays whatever
    SAQ was constructed with -- when there is no SAQ worker in ``ctx``, as when a startup hook is
    driven directly rather than by ``Worker.start()``.
    """
    worker = ctx.get("worker")
    if not isinstance(worker, Worker):
        logger.warning("phaze.saq_worker no SAQ worker in ctx; concurrency is fixed for this process")
        return None
    live = LiveConcurrencyWorker.adopt(worker)
    live.set_concurrency(resolve(store.current()))
    store.register_applier(APPLIER_NAME, lambda _old, new: live.set_concurrency(resolve(new)))
    logger.info("phaze.saq_worker live concurrency installed", concurrency=live.concurrency)
    return live


__all__ = ["APPLIER_NAME", "PROCESS_TASK_NAME", "LiveConcurrencyWorker", "install_live_concurrency", "lane_concurrency"]
