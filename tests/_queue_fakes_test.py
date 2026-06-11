"""Direct coverage for the queue-fake harness extensions added in Phase 34-00.

These prove the new ``FakeQueue.count``/``set_counts``/``fail_count`` and
``FakeTaskRouter.set_counts`` behave as the downstream ``get_queue_activity`` tests
(Plans 01-04) rely on: seeded depths read back via ``await count``, an un-seeded kind
reads 0, ``fail_count`` makes ``count`` raise (the degrade path), and per-agent seeding
routes through the same cached fake ``queue_for`` returns.
"""

from __future__ import annotations

import pytest

from tests._queue_fakes import FakeQueue, FakeTaskRouter


@pytest.mark.asyncio
async def test_fake_queue_count_returns_seeded_depths() -> None:
    """``set_counts`` seeds per-kind depths that ``await count`` reads back."""
    queue = FakeQueue("phaze-agent-nox")
    queue.set_counts(queued=3, active=2)

    assert await queue.count("queued") == 3
    assert await queue.count("active") == 2


@pytest.mark.asyncio
async def test_fake_queue_count_unseeded_kind_is_zero() -> None:
    """An un-seeded kind degrades to 0 rather than raising."""
    queue = FakeQueue("phaze-agent-nox")

    assert await queue.count("incomplete") == 0
    assert await queue.count("unknown-kind") == 0


@pytest.mark.asyncio
async def test_fake_queue_fail_count_raises() -> None:
    """``fail_count`` makes ``count`` raise to exercise the degrade path."""
    queue = FakeQueue("phaze-agent-nox")
    queue.set_counts(queued=7)
    queue.fail_count()

    with pytest.raises(RuntimeError):
        await queue.count("queued")


@pytest.mark.asyncio
async def test_fake_task_router_set_counts_routes_to_cached_queue() -> None:
    """Per-agent seeding routes through the same cached fake ``queue_for`` returns."""
    router = FakeTaskRouter()
    router.set_counts("nox", queued=5, active=1)

    queue = router.queue_for("nox")

    assert queue is router.queues["nox"]
    assert await queue.count("queued") == 5
    assert await queue.count("active") == 1
