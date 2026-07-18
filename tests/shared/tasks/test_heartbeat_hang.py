"""phaze-kaf2 — the heartbeat must survive a HANG, not just a raise.

The 2026-07-18 nox incident: the analyze-lane worker was healthy and processing jobs,
but `Agent.last_seen_at` froze and /admin/agents showed nox DEAD. No "heartbeat failed"
line, no "loop iteration failed" line -- ZERO log evidence, because the loop was not
erroring. It was stuck inside an unbounded `await queue.info()`.

That distinction is the whole bead. `try/except` catches raises; an await that never
returns is not an exception, so the pre-existing "loop survives an iteration exception"
coverage passed while the real failure mode went unprotected. These tests therefore use
never-resolving futures rather than side_effect raises -- a raise-based test cannot
reproduce this defect at all.
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


def _make_ctx(*, hang_info: bool = False) -> dict[str, Any]:
    """ctx shaped like SAQ's. ``hang_info`` makes queue.info() never resolve."""
    client = AsyncMock()
    identity = AgentIdentity(
        agent_id="test-agent",
        name="Test Agent",
        scan_roots=["/data"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    worker = MagicMock()
    queue = AsyncMock()
    if hang_info:

        async def _never_returns() -> dict[str, Any]:
            # The exact production signature: a wedged psycopg pool acquire. Not a raise.
            await asyncio.Event().wait()
            raise AssertionError  # unreachable  # pragma: no cover

        queue.info = _never_returns
    else:
        queue.info = AsyncMock(return_value={"queued": 5, "active": 0, "scheduled": 0, "name": "q", "workers": {}, "jobs": []})
    worker.queue = queue
    return {"api_client": client, "agent_identity": identity, "worker": worker, "job": MagicMock()}


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            return
        await asyncio.sleep(0)


async def test_hung_queue_info_still_posts_the_heartbeat(caplog: pytest.LogCaptureFixture) -> None:
    """A queue.info() that never returns degrades depth to 0 -- it must NOT block the POST.

    Depth is enrichment; the POST is what keeps the agent alive. Before the fix this call
    never returned and the agent went DEAD while healthy.
    """
    ctx = _make_ctx(hang_info=True)

    with (
        caplog.at_level("WARNING", logger="phaze.tasks.heartbeat"),
        patch("phaze.tasks.heartbeat.QUEUE_INFO_TIMEOUT_SECONDS", 0.01),
        patch("phaze.tasks.heartbeat.os.getpid", return_value=999),
    ):
        await send_heartbeat(ctx)

    client = ctx["api_client"]
    assert client.heartbeat.await_count == 1
    call_args = client.heartbeat.await_args
    payload = call_args.args[0] if call_args.args else call_args.kwargs["payload"]
    assert isinstance(payload, HeartbeatRequest)
    assert payload.queue_depth == 0
    # The hang must be NAMED, not folded into the generic failure branch.
    assert any("timed out" in r.getMessage() for r in caplog.records)


async def test_loop_survives_a_beat_that_hangs_forever(caplog: pytest.LogCaptureFixture) -> None:
    """THE acceptance criterion: a send_heartbeat that blocks forever does not stop later beats.

    Beat 1 hangs indefinitely; the per-iteration deadline cancels it and the loop keeps
    ticking. Without the deadline this test hangs until the suite times out -- which is
    precisely what happened to liveness in production.
    """
    ctx = _make_ctx()
    calls = {"n": 0}

    async def first_beat_hangs(_ctx: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.Event().wait()  # never returns

    with (
        caplog.at_level("WARNING", logger="phaze.tasks.heartbeat"),
        patch("phaze.tasks.heartbeat.send_heartbeat", new=first_beat_hangs),
        patch("phaze.tasks.heartbeat.BEAT_TIMEOUT_SECONDS", 0.01),
        patch("phaze.tasks.heartbeat.AGENT_HEARTBEAT_INTERVAL_SECONDS", 0),
    ):
        task = asyncio.create_task(_heartbeat_loop(ctx))
        await _wait_until(lambda: calls["n"] >= 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls["n"] >= 3, "loop stopped after the hung beat -- liveness would freeze"
    assert any("timed out" in r.getMessage() for r in caplog.records)


async def test_beat_deadline_is_independent_of_the_cadence() -> None:
    """Patching the cadence to 0 must NOT disable the hang deadline.

    Regression guard for a real bug in this fix's first draft: reusing
    AGENT_HEARTBEAT_INTERVAL_SECONDS as the wait_for timeout meant every beat timed out
    the moment a test (or an operator) set the cadence low. Cadence and deadline are
    separate knobs.
    """
    ctx = _make_ctx()

    with (
        patch("phaze.tasks.heartbeat.AGENT_HEARTBEAT_INTERVAL_SECONDS", 0),
        patch("phaze.tasks.heartbeat.os.getpid", return_value=999),
    ):
        task = asyncio.create_task(_heartbeat_loop(ctx))
        await _wait_until(lambda: ctx["api_client"].heartbeat.await_count >= 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Beats actually POSTed rather than all timing out at a 0s deadline.
    assert ctx["api_client"].heartbeat.await_count >= 3


async def test_loop_emits_periodic_proof_of_life(caplog: pytest.LogCaptureFixture) -> None:
    """Cadence is observable at INFO so a frozen loop is distinguishable from a healthy one.

    Success was DEBUG-only, so both states logged nothing and the incident was unreadable.
    """
    ctx = _make_ctx()

    with (
        caplog.at_level("INFO", logger="phaze.tasks.heartbeat"),
        patch("phaze.tasks.heartbeat.HEARTBEAT_INFO_LOG_EVERY", 2),
        patch("phaze.tasks.heartbeat.AGENT_HEARTBEAT_INTERVAL_SECONDS", 0),
        patch("phaze.tasks.heartbeat.os.getpid", return_value=999),
    ):
        task = asyncio.create_task(_heartbeat_loop(ctx))
        await _wait_until(lambda: any("heartbeat loop alive" in r.getMessage() for r in caplog.records))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert any("heartbeat loop alive" in r.getMessage() for r in caplog.records)
