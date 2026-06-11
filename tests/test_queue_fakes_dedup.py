"""Self-tests for the dedup-aware queue doubles (Phase 32 Wave-0 harness).

These pin the load-bearing primitive of the whole phase: SAQ's deterministic-key
dedup no-op, modelled WITHOUT a live Redis. The real
``saq.queue.redis.Queue._enqueue`` returns ``None`` when a job's deterministic key is
already in the per-queue ``incomplete`` set, and accepts it again only after the job
finishes (32-RESEARCH §Q1). :class:`DedupFakeQueue` mirrors that; this module proves it.

asyncio_mode="auto" + testpaths=["tests"] (pyproject) collect these automatically.
"""

from __future__ import annotations

from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter


async def test_dedup_repeat_live_key_returns_none() -> None:
    """A repeat enqueue of an in-flight deterministic key is a no-op (returns None)."""
    queue = DedupFakeQueue("phaze-agent-nox")
    first = await queue.enqueue("process_file", key="process_file:X", file_id="X")
    second = await queue.enqueue("process_file", key="process_file:X", file_id="X")

    assert first is not None
    assert second is None
    # The deduped enqueue never lands the payload (mirrors SAQ: nil → no append).
    assert len(queue.captured) == 1
    assert len(queue.captured_policy) == 1


async def test_dedup_keyless_never_dedups() -> None:
    """A keyless enqueue always returns a job — it never deduplicates."""
    queue = DedupFakeQueue("phaze-agent-nox")
    first = await queue.enqueue("process_file", file_id="X")
    second = await queue.enqueue("process_file", file_id="X")

    assert first is not None
    assert second is not None
    assert len(queue.captured) == 2


async def test_dedup_finish_allows_reenqueue() -> None:
    """After ``finish(key)`` the same deterministic key enqueues again (job completed)."""
    queue = DedupFakeQueue("phaze-agent-nox")
    first = await queue.enqueue("process_file", key="process_file:X", file_id="X")
    blocked = await queue.enqueue("process_file", key="process_file:X", file_id="X")
    queue.finish("process_file:X")
    after_finish = await queue.enqueue("process_file", key="process_file:X", file_id="X")

    assert first is not None
    assert blocked is None
    assert after_finish is not None
    # Two landed payloads (the deduped middle call did not land).
    assert len(queue.captured) == 2


async def test_dedup_taskrouter_returns_dedup_queue() -> None:
    """``DedupFakeTaskRouter.queue_for`` yields a cached ``DedupFakeQueue`` per agent."""
    router = DedupFakeTaskRouter()
    queue = router.queue_for("nox")
    again = router.queue_for("nox")

    assert isinstance(queue, DedupFakeQueue)
    assert queue is again
    assert queue.name == "phaze-agent-nox"
    assert router.queue_for_calls == ["nox", "nox"]
