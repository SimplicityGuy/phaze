"""Unit tests for the resizable FIFO capacity limiter (``phaze-mvq8z.7``).

Every test here is deterministic by construction: a polling loop waits on a STRUCTURAL condition
(a waiter queued, a token released) with a bounded deadline, never a fixed sleep tuned to this
host's speed -- this repo's own gate-file helper (``tests/analyze/_child_stubs.py``) uses the
identical shape, and a sleep-tuned test already flaked once in this epic (``phaze-mvq8z.5``).

The REAL-subprocess test proving a shrink never kills or cancels an in-flight analysis child --
the acceptance criterion this unit suite cannot exercise on its own, since nothing here spawns a
process -- lives alongside the rest of the driver's real-subprocess suite in
``tests/analyze/services/pipeline/test_analysis_exec.py``
(``test_a_shrink_mid_flight_never_kills_the_children_already_borrowed``).
"""

from __future__ import annotations

import asyncio

import pytest

from phaze.services.resizable_limiter import ResizableLimiter
from tests._async_settle import wait_until


# Bounds every poll below (tests/_async_settle.py's `wait_until`) -- a genuine deadlock is
# reported promptly rather than hanging the suite.
_POLL_DEADLINE_SEC = 5.0


def test_rejects_a_non_positive_initial_ceiling() -> None:
    with pytest.raises(ValueError, match="total_tokens must be >= 1"):
        ResizableLimiter(0)


def test_rejects_a_non_positive_resize() -> None:
    limiter = ResizableLimiter(2)
    with pytest.raises(ValueError, match="total_tokens must be >= 1"):
        limiter.resize(-1)
    assert limiter.total_tokens == 2, "a rejected resize must not partially apply"


async def test_acquire_and_release_happy_path() -> None:
    limiter = ResizableLimiter(1)
    assert limiter.available_tokens == 1
    assert not limiter.locked()

    await limiter.acquire()

    assert limiter.available_tokens == 0
    assert limiter.borrowed_tokens == 1
    assert limiter.locked()

    limiter.release()

    assert limiter.available_tokens == 1
    assert limiter.borrowed_tokens == 0
    assert not limiter.locked()


def test_release_without_a_matching_acquire_raises() -> None:
    limiter = ResizableLimiter(1)
    with pytest.raises(ValueError, match=r"release\(\) called more times than acquire\(\)"):
        limiter.release()


async def test_async_context_manager_acquires_and_releases() -> None:
    limiter = ResizableLimiter(1)
    async with limiter:
        assert limiter.borrowed_tokens == 1
    assert limiter.borrowed_tokens == 0


async def test_async_context_manager_releases_even_when_the_body_raises() -> None:
    limiter = ResizableLimiter(1)
    with pytest.raises(RuntimeError, match="boom"):
        async with limiter:
            raise RuntimeError("boom")
    assert limiter.borrowed_tokens == 0


async def test_a_third_acquire_waits_behind_two_already_borrowed() -> None:
    limiter = ResizableLimiter(2)
    await limiter.acquire()
    await limiter.acquire()
    assert limiter.locked()

    admitted = False

    async def _third() -> None:
        nonlocal admitted
        await limiter.acquire()
        admitted = True

    task = asyncio.ensure_future(_third())
    await wait_until(lambda: limiter.waiting == 1, timeout=_POLL_DEADLINE_SEC, description="the third acquire never queued")
    assert not admitted, "a full limiter must not admit a third acquire"

    limiter.release()  # one of the two borrowed tokens comes back
    await asyncio.wait_for(task, timeout=_POLL_DEADLINE_SEC)
    assert admitted
    assert limiter.borrowed_tokens == 2  # the freed token went straight to the waiter


async def test_grow_wakes_a_waiting_acquire_immediately() -> None:
    """Per docs/design/0019-runtime-config-hot-reload.md §5/§7: grow admits waiting work immediately -- no release required."""
    limiter = ResizableLimiter(1)
    await limiter.acquire()  # the one token is out; the limiter is full

    admitted = asyncio.Event()

    async def _waiter() -> None:
        await limiter.acquire()
        admitted.set()

    task = asyncio.ensure_future(_waiter())
    await wait_until(lambda: limiter.waiting == 1, timeout=_POLL_DEADLINE_SEC, description="the waiter never queued")
    assert not admitted.is_set()

    limiter.resize(2)  # grow -- nothing released

    await asyncio.wait_for(admitted.wait(), timeout=_POLL_DEADLINE_SEC)
    assert limiter.borrowed_tokens == 2
    assert limiter.total_tokens == 2
    await task


async def test_grow_wakes_every_waiter_the_new_ceiling_admits_in_one_resize() -> None:
    """A multi-step grow (1 -> 4) wakes ALL newly-admissible waiters at once, not one per call."""
    limiter = ResizableLimiter(1)
    await limiter.acquire()

    admitted_count = 0

    async def _waiter() -> None:
        nonlocal admitted_count
        await limiter.acquire()
        admitted_count += 1

    tasks = [asyncio.ensure_future(_waiter()) for _ in range(3)]
    await wait_until(lambda: limiter.waiting == 3, timeout=_POLL_DEADLINE_SEC, description="not all waiters queued")

    limiter.resize(4)  # 1 already borrowed + 3 waiters = exactly the new ceiling

    await asyncio.wait_for(asyncio.gather(*tasks), timeout=_POLL_DEADLINE_SEC)
    assert admitted_count == 3
    assert limiter.borrowed_tokens == 4
    assert limiter.waiting == 0


async def test_shrink_never_revokes_an_already_borrowed_token() -> None:
    """Per docs/design/0019-runtime-config-hot-reload.md §5/§7: shrink lowers the ceiling but never touches a holder already in."""
    limiter = ResizableLimiter(3)
    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()
    assert limiter.borrowed_tokens == 3

    limiter.resize(1)  # shrink below what is already borrowed

    # Nothing was revoked: still 3 borrowed. Only each holder's own release() -- never
    # resize() itself -- drops it; no forced release, no cancellation.
    assert limiter.borrowed_tokens == 3
    assert limiter.total_tokens == 1
    assert limiter.available_tokens == 0
    assert limiter.locked()


async def test_shrink_admits_no_new_work_until_below_the_new_ceiling() -> None:
    limiter = ResizableLimiter(2)
    await limiter.acquire()
    await limiter.acquire()
    limiter.resize(1)  # ceiling below the 2 already borrowed

    admitted = False

    async def _waiter() -> None:
        nonlocal admitted
        await limiter.acquire()
        admitted = True

    task = asyncio.ensure_future(_waiter())
    await wait_until(lambda: limiter.waiting == 1, timeout=_POLL_DEADLINE_SEC, description="the waiter never queued")
    assert not admitted

    limiter.release()  # 2 -> 1 borrowed: AT the new ceiling, not below it
    # Give the loop every chance to run anything a (buggy) implementation might have woken --
    # structural, not time-based: nothing here is racing a clock, only draining ready callbacks.
    for _ in range(5):
        await asyncio.sleep(0)
    assert not admitted, "one release only reaches the new ceiling, not below it -- must not admit yet"
    assert limiter.borrowed_tokens == 1

    limiter.release()  # 1 -> 0 borrowed: now below the ceiling of 1
    await asyncio.wait_for(task, timeout=_POLL_DEADLINE_SEC)
    assert admitted
    assert limiter.borrowed_tokens == 1


async def test_fifo_order_is_preserved_across_a_resize() -> None:
    """Waiters are admitted in arrival order, whether the trigger is a release or a grow."""
    limiter = ResizableLimiter(1)
    await limiter.acquire()

    order: list[str] = []

    async def _waiter(name: str) -> None:
        await limiter.acquire()
        order.append(name)

    task_a = asyncio.ensure_future(_waiter("a"))
    await wait_until(lambda: limiter.waiting == 1, timeout=_POLL_DEADLINE_SEC, description="'a' never queued")
    task_b = asyncio.ensure_future(_waiter("b"))
    await wait_until(lambda: limiter.waiting == 2, timeout=_POLL_DEADLINE_SEC, description="'b' never queued")

    limiter.resize(2)  # admits exactly one: 1 already borrowed + 1 new
    await wait_until(lambda: order == ["a"], timeout=_POLL_DEADLINE_SEC, description="'a' (first in line) was not admitted first")

    limiter.release()
    await asyncio.wait_for(task_b, timeout=_POLL_DEADLINE_SEC)
    assert order == ["a", "b"]
    await task_a


async def test_cancelling_a_waiting_acquire_does_not_consume_a_token() -> None:
    limiter = ResizableLimiter(1)
    await limiter.acquire()  # the one token is out

    task = asyncio.ensure_future(limiter.acquire())
    await wait_until(lambda: limiter.waiting == 1, timeout=_POLL_DEADLINE_SEC, description="the acquire never queued")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert limiter.waiting == 0
    assert limiter.borrowed_tokens == 1  # only the original holder's token, never the cancelled one

    limiter.release()
    assert limiter.borrowed_tokens == 0
    assert limiter.available_tokens == 1


async def test_cancel_racing_a_grant_gives_the_token_back() -> None:
    """The narrow race ``asyncio.Semaphore.acquire``'s own source comments on: a resize (or a
    release) grants a waiter's future SYNCHRONOUSLY, with no intervening ``await`` -- so a
    ``task.cancel()`` issued immediately after still lands on that task, because the event
    loop has not yet run the future's done-callback that would clear it as the task's
    current waiter. The coroutine then receives ``CancelledError`` despite the future already
    holding a result. The grant must be given back, not leaked to a cancelled acquirer.
    """
    limiter = ResizableLimiter(1)
    await limiter.acquire()  # borrowed=1, total=1 -- the limiter is full

    task = asyncio.ensure_future(limiter.acquire())
    await wait_until(lambda: limiter.waiting == 1, timeout=_POLL_DEADLINE_SEC, description="the acquire never queued")

    limiter.resize(2)  # synchronously grants the waiter's future: borrowed -> 2, no await yet
    assert limiter.borrowed_tokens == 2
    task.cancel()  # races the grant -- see docstring

    with pytest.raises(asyncio.CancelledError):
        await task

    # The grant was undone: the cancelled task never actually took the token it was handed.
    assert limiter.total_tokens == 2
    assert limiter.borrowed_tokens == 1
    assert limiter.available_tokens == 1


def test_repr_reports_the_headline_state() -> None:
    limiter = ResizableLimiter(2)
    text = repr(limiter)
    assert "total:2" in text
    assert "borrowed:0" in text
    assert "waiters:" not in text, "an empty queue must not print a waiters count"


async def test_repr_reports_a_waiters_count_when_something_is_queued() -> None:
    limiter = ResizableLimiter(1)
    await limiter.acquire()  # the one token is out; the limiter is full

    task = asyncio.ensure_future(limiter.acquire())
    await wait_until(lambda: limiter.waiting == 1, timeout=_POLL_DEADLINE_SEC, description="the waiter never queued")

    assert "waiters:1" in repr(limiter)

    limiter.release()
    await asyncio.wait_for(task, timeout=_POLL_DEADLINE_SEC)
