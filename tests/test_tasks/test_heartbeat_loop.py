"""Phase 46 — tests for the starvation-proof background heartbeat loop.

These cover the asyncio-background-task heartbeat that decouples agent liveness
from the SAQ job-dispatch concurrency pool (Phase 46 incident: a worker saturated
with multi-hour ``process_file`` jobs could not get a dispatch slot for the old
``heartbeat_tick`` cron, so a healthy busy agent was wrongly marked DEAD).

Covers:

* ``send_heartbeat`` degrades to ``queue_depth=0`` when ctx has no ``worker`` key
  (the loop reads ``ctx["worker"].queue`` lazily; it may not be attached yet).
* ``_heartbeat_loop`` fires repeatedly on cadence WITHOUT acquiring a SAQ dispatch
  slot (it runs entirely on the event loop) — the starvation-independence proof.
* ``_heartbeat_loop`` survives an unexpected exception in one iteration and keeps
  ticking (a dead loop = a silently DEAD agent).
* ``_heartbeat_loop`` re-raises ``asyncio.CancelledError`` for a clean shutdown.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phaze.schemas.agent_heartbeat import HeartbeatRequest
from phaze.schemas.agent_identity import AgentIdentity
from phaze.tasks.heartbeat import _heartbeat_loop, send_heartbeat


if TYPE_CHECKING:
    from collections.abc import Callable


def _make_ctx(*, queued: int = 5, raise_info: bool = False) -> dict[str, Any]:
    """Build a ctx dict shaped like the one SAQ injects (mirrors test_heartbeat_cron)."""
    client = AsyncMock()
    identity = AgentIdentity(
        agent_id="test-agent",
        name="Test Agent",
        scan_roots=["/data"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    worker = MagicMock()
    queue = AsyncMock()
    if raise_info:
        queue.info = AsyncMock(side_effect=RuntimeError("redis down"))
    else:
        queue.info = AsyncMock(
            return_value={
                "queued": queued,
                "active": 0,
                "scheduled": 0,
                "name": "phaze-agent-test-agent",
                "workers": {},
                "jobs": [],
            },
        )
    worker.queue = queue
    return {
        "api_client": client,
        "agent_identity": identity,
        "worker": worker,
        "job": MagicMock(),
    }


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Cooperatively yield until ``predicate()`` is true or ``timeout`` elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            return
        await asyncio.sleep(0)


async def test_send_heartbeat_degrades_to_zero_without_worker_key() -> None:
    """No ctx['worker'] (not yet attached) -> queue_depth=0 through the SAME try/except, still POSTs."""
    ctx = _make_ctx()
    del ctx["worker"]

    with patch("phaze.tasks.heartbeat.os.getpid", return_value=999):
        await send_heartbeat(ctx)

    client = ctx["api_client"]
    assert client.heartbeat.await_count == 1
    call_args = client.heartbeat.await_args
    payload = call_args.args[0] if call_args.args else call_args.kwargs["payload"]
    assert isinstance(payload, HeartbeatRequest)
    assert payload.queue_depth == 0


async def test_heartbeat_loop_fires_repeatedly_without_dispatch_slot() -> None:
    """Starvation-independence: the loop POSTs >=N times within a bounded budget with NO free SAQ slot.

    The loop never acquires a dispatch semaphore — it ticks on the event loop —
    so saturating ``worker_max_jobs`` cannot starve it. Proven by patching the
    interval to 0 and counting POSTs while nothing frees a slot.
    """
    ctx = _make_ctx()
    client = ctx["api_client"]
    target = 5

    with patch("phaze.tasks.heartbeat.AGENT_HEARTBEAT_INTERVAL_SECONDS", 0):
        task = asyncio.create_task(_heartbeat_loop(ctx))
        await _wait_until(lambda: client.heartbeat.await_count >= target)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert client.heartbeat.await_count >= target


async def test_heartbeat_loop_survives_iteration_exception(caplog: pytest.LogCaptureFixture) -> None:
    """A raised exception in one iteration is logged at WARNING and the loop keeps ticking."""
    ctx = _make_ctx()
    calls = {"n": 0}

    async def flaky(_ctx: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            msg = "boom"
            raise RuntimeError(msg)

    with (
        caplog.at_level("WARNING", logger="phaze.tasks.heartbeat"),
        patch("phaze.tasks.heartbeat.send_heartbeat", new=flaky),
        patch("phaze.tasks.heartbeat.AGENT_HEARTBEAT_INTERVAL_SECONDS", 0),
    ):
        task = asyncio.create_task(_heartbeat_loop(ctx))
        await _wait_until(lambda: calls["n"] >= 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # First iteration raised; subsequent iterations still ran -> loop survived.
    assert calls["n"] >= 3
    assert any(r.levelname == "WARNING" for r in caplog.records)


async def test_heartbeat_loop_reraises_cancelled() -> None:
    """asyncio.CancelledError propagates (not swallowed by the broad except) for clean shutdown."""
    ctx = _make_ctx()

    with patch("phaze.tasks.heartbeat.AGENT_HEARTBEAT_INTERVAL_SECONDS", 0):
        task = asyncio.create_task(_heartbeat_loop(ctx))
        await _wait_until(lambda: ctx["api_client"].heartbeat.await_count >= 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert task.cancelled()
