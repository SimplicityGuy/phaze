"""Contract: the SAQ internals ``phaze.tasks._shared.live_worker`` relies on (phaze-mvq8z.10).

``LiveConcurrencyWorker`` reaches into SAQ's PRIVATE worker loop and Postgres queue: none of it is
public API, and a release can change it without calling that breaking
(``docs/design/0019-runtime-config-hot-reload.md`` §8). Each test below pins ONE internal the subclass
depends on and names what breaks without it, so a SAQ bump fails HERE, loudly and specifically,
instead of as a worker that quietly ignores a resize or strands claimed rows. The behaviour on a
real broker is proven in ``tests/integration/test_live_worker_concurrency.py``; this module is
the tripwire, and is deliberately database-free so it runs on every gate.

Where behaviour can be observed without a broker it is; where the property IS an ordering or a
shape inside a method body (start()'s startup-before-spawn, the claim's ``LIMIT``) the method's
source is read, because there is no other way to see it.

**On a failure:** do not update the expectation to match. Re-read the new SAQ source against the
module docstring of ``live_worker``, fix the subclass if the mechanism moved, re-run the
integration tests, and only then add the version to ``VERIFIED_SAQ_VERSIONS``.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Any

import saq
from saq import Worker
from saq.queue.postgres import PostgresQueue
import saq.worker


#: SAQ versions the subclass was verified against, on a real broker. pyproject.toml pins the minor
#: line; this pins the exact release, so a patch bump is a review too (ADR §8).
VERIFIED_SAQ_VERSIONS = frozenset({"0.26.4"})


def _source(obj: Any) -> str:
    return inspect.getsource(obj)


def _queue() -> PostgresQueue:
    # from_url opens no connection (the pool is built with open=False), so this is DB-free.
    return PostgresQueue.from_url("postgresql://contract@localhost:1/contract", name="contract")


def test_the_installed_saq_is_a_verified_version() -> None:
    assert saq.__version__ in VERIFIED_SAQ_VERSIONS, (
        f"saq {saq.__version__} is installed but live_worker was verified only against {sorted(VERIFIED_SAQ_VERSIONS)}"
    )


def test_the_worker_is_its_own_ctx_entry() -> None:
    """install_live_concurrency finds the worker to adopt at ``ctx["worker"]`` (saq/worker.py:132)."""
    worker: Worker[Any] = Worker(_queue(), functions=[])
    assert worker.context["worker"] is worker


def test_start_runs_startup_hooks_before_it_spawns_the_loops() -> None:
    """Adoption happens in a startup hook, so every hook must run before the first ``_process()``.

    ``start()`` must also spawn exactly ``self.concurrency`` loops through ``self._process()`` with no
    previous task (saq/worker.py:194-201): that call is how the subclass learns dispatch has begun,
    and ``concurrency`` read at that point is what makes the startup-time target the initial one.
    """
    source = _source(Worker.start)
    startup = source.index("await s(self.context)")
    spawn = re.search(r"for _ in range\(self\.concurrency\):\s*\n\s*self\._process\(\)", source)
    assert spawn is not None, "start() no longer spawns concurrency loops via self._process()"
    assert startup < spawn.start()


def test_each_loop_relaunches_from_its_own_done_callback() -> None:
    """The retire rule overrides ``_process(previous_task)``, SAQ's relaunch point (saq/worker.py:440-453).

    It depends on: the parameter's name and default; the previous task being discarded from
    ``self.tasks`` there; the new task being named ``"process"`` (how loops are counted apart from
    upkeep tasks) and added to ``self.tasks``; and ``self._process`` being the done-callback.
    """
    signature = inspect.signature(Worker._process)
    assert list(signature.parameters) == ["self", "previous_task"]
    assert signature.parameters["previous_task"].default is None
    source = _source(Worker._process)
    assert "self.tasks.discard(previous_task)" in source
    assert 'asyncio.create_task(self.process(), name="process")' in source
    assert "self.tasks.add(new_task)" in source
    assert "new_task.add_done_callback(self._process)" in source
    assert "if not self.event.is_set():" in source


def test_the_saq_cli_constructs_a_plain_worker_with_no_class_hook() -> None:
    """Why the subclass is adopted rather than constructed (saq/worker.py:541).

    If SAQ grows a worker-class setting, adoption becomes unnecessary -- this failing is the
    prompt to switch to it rather than keep the ``__class__`` swap.
    """
    assert "worker = Worker(**settings_obj)" in _source(saq.worker.start)


def test_a_plain_worker_can_change_class_in_place() -> None:
    """``LiveConcurrencyWorker.adopt`` assigns ``__class__``; SAQ must keep Worker slot-free for that."""
    assert "__slots__" not in vars(Worker)
    worker: Worker[Any] = Worker(_queue(), functions=[])

    class _Sub(Worker[Any]):
        pass

    worker.__class__ = _Sub
    assert isinstance(worker, _Sub)


def test_process_dequeues_through_self_queue() -> None:
    """The subclass sees which loops are idle by wrapping ``self.queue.dequeue`` (saq/worker.py:347-350)."""
    source = _source(Worker.process)
    assert "await self.queue.dequeue(" in source
    assert source.index("self.queue.dequeue(") < source.index("await job.update(status=Status.ACTIVE)")


def test_the_postgres_queue_carries_the_lock_buffer_and_waiter_count() -> None:
    """The idle trim holds ``_dequeue_lock``, reads ``_job_queue`` and relies on ``_waiting`` (postgres.py:144-148)."""
    queue = _queue()
    assert isinstance(queue._dequeue_lock, asyncio.Lock)
    assert isinstance(queue._job_queue, asyncio.Queue)
    assert queue._waiting == 0


def test_the_claim_runs_under_the_dequeue_lock_and_is_sized_by_the_waiters() -> None:
    """Why idle loops must be trimmed, and why holding the lock makes cancelling one safe (postgres.py:636-690).

    Every waiter counts toward the claim's ``LIMIT``, so an idle surplus loop claims work. The claim,
    which moves rows into the in-memory buffer one by one, runs only under ``_dequeue_lock``, and a
    second caller returns at once rather than waiting -- so with the lock held and the buffer empty,
    no waiter is inside a claim.
    """
    source = _source(PostgresQueue._dequeue)
    assert re.search(r"if self\._dequeue_lock\.locked\(\):\s*\n\s*return", source)
    assert "async with self._dequeue_lock:" in source
    assert '"limit": self._waiting' in source
    assert "self._job_queue.put_nowait(job)" in source


def test_dequeue_swallows_a_cancellation_and_returns_no_job() -> None:
    """A cancelled idle loop must end as a loop that got no job, not as an error (postgres.py:581-634)."""
    source = _source(PostgresQueue.dequeue)
    assert "except (asyncio.TimeoutError, asyncio.CancelledError):" in source
    assert "self._waiting += 1" in source
    assert "self._waiting -= 1" in source


async def test_dequeue_really_returns_none_when_cancelled_while_waiting() -> None:
    """The same property observed: a waiter parked on the buffer, cancelled, returns ``None``."""
    queue = _queue()
    # Force the no-claim path: a held listen lock makes dequeue wait on the buffer directly, and a
    # held dequeue lock makes its opening _dequeue() return without touching the database.
    async with queue._listen_lock, queue._dequeue_lock:
        waiter = asyncio.create_task(queue.dequeue())
        while queue._waiting == 0:
            await asyncio.sleep(0)
        waiter.cancel()
        assert await waiter is None
    assert queue._waiting == 0
