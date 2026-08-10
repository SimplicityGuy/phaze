"""phaze-xuec1 -- liveness must reflect the ability to DEQUEUE, not just POST an HTTP beat.

The 2026-08-08 nox incident: `phaze-agent-worker-analyze` beat every 30s for 1h41m --
`last_seen_at` kept advancing and /admin/agents showed it alive -- while its psycopg3
broker pool was unusable and it dequeued nothing at all (302 jobs queued, 0 active).
`heartbeat`'s POST is independent HTTP traffic to the control-plane API; it proves the
process is up and can reach that API, and nothing about whether it can reach its broker.

``queue.info()`` exercises the SAME broker pool ``_dequeue()`` needs, so this module now
tracks consecutive probe failures and, once sustained, WITHHOLDS the heartbeat POST
(``AGENT_BROKER_UNHEALTHY_BEATS``) so ``last_seen_at`` goes stale and the EXISTING
``phaze.services.agent_liveness.classify`` thresholds degrade the agent to stale/dead --
and, if the broker never recovers, self-terminates (``AGENT_BROKER_EXIT_BEATS``) so the
container's restart policy can replace it rather than sitting wedged indefinitely.

These tests reproduce the bead's own "regression test" acceptance bullet directly: broker
unreachable at/after startup, then reachable -- assert the worker ends up consuming (here:
heartbeats resume, the direct, testable proxy for "liveness reflects real capability" the
design doc itself proposes), or fails visibly (here: it exits loudly once unreachable stays
sustained).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phaze.constants import AGENT_BROKER_EXIT_BEATS, AGENT_BROKER_UNHEALTHY_BEATS
from phaze.schemas.agent_identity import AgentIdentity
from phaze.tasks.heartbeat import _heartbeat_loop, send_heartbeat


if TYPE_CHECKING:
    from collections.abc import Callable


def _make_ctx() -> dict[str, Any]:
    """ctx shaped like SAQ's (mirrors test_heartbeat_loop._make_ctx). queue.info() always fails."""
    client = AsyncMock()
    identity = AgentIdentity(
        agent_id="test-agent",
        name="Test Agent",
        scan_roots=["/data"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    worker = MagicMock()
    queue = AsyncMock()
    queue.info = AsyncMock(side_effect=RuntimeError("connection to server ... failed: Network is unreachable"))
    worker.queue = queue
    return {"api_client": client, "agent_identity": identity, "worker": worker, "job": MagicMock()}


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            return
        await asyncio.sleep(0)


async def test_single_and_double_probe_failures_still_post(caplog: pytest.LogCaptureFixture) -> None:
    """Below AGENT_BROKER_UNHEALTHY_BEATS, a bad tick (or two) must not flip 'alive' --
    matches the existing single-hang behavior (tests/shared/tasks/test_heartbeat_hang.py)."""
    assert AGENT_BROKER_UNHEALTHY_BEATS >= 2, "test assumes at least 2 beats of tolerance"
    ctx = _make_ctx()

    for _ in range(AGENT_BROKER_UNHEALTHY_BEATS - 1):
        await send_heartbeat(ctx)

    assert ctx["api_client"].heartbeat.await_count == AGENT_BROKER_UNHEALTHY_BEATS - 1


async def test_sustained_broker_failure_withholds_heartbeat(caplog: pytest.LogCaptureFixture) -> None:
    """After AGENT_BROKER_UNHEALTHY_BEATS consecutive probe failures, the POST is withheld
    and an ERROR is logged -- the acceptance criterion: liveness can no longer report alive
    while the worker cannot verify it can dequeue. The first ``AGENT_BROKER_UNHEALTHY_BEATS
    - 1`` beats still post (a single bad tick, or two, must not flip 'alive' -- matches the
    existing single-hang tolerance)."""
    ctx = _make_ctx()

    with caplog.at_level("ERROR", logger="phaze.tasks.heartbeat"):
        for _ in range(AGENT_BROKER_UNHEALTHY_BEATS):
            await send_heartbeat(ctx)

    assert ctx["api_client"].heartbeat.await_count == AGENT_BROKER_UNHEALTHY_BEATS - 1, "only the Nth (sustained-failing) beat is withheld"
    assert any(r.levelname == "ERROR" and "withholding" in r.getMessage() for r in caplog.records)


async def test_broker_recovery_resumes_the_heartbeat() -> None:
    """The worker recovers on its own: once queue.info() starts succeeding again, the POST
    resumes and the failure counter resets -- the FIRST acceptable outcome named by the
    bead's regression-test acceptance bullet."""
    ctx = _make_ctx()
    for _ in range(AGENT_BROKER_UNHEALTHY_BEATS):
        await send_heartbeat(ctx)
    posts_before_recovery = ctx["api_client"].heartbeat.await_count
    assert posts_before_recovery == AGENT_BROKER_UNHEALTHY_BEATS - 1

    ctx["worker"].queue.info = AsyncMock(return_value={"queued": 2, "active": 0, "scheduled": 0, "name": "q", "workers": {}, "jobs": []})
    await send_heartbeat(ctx)

    assert ctx["api_client"].heartbeat.await_count == posts_before_recovery + 1
    assert ctx["_broker_probe_failures"] == 0


async def test_broker_never_recovering_terminates_the_worker(caplog: pytest.LogCaptureFixture) -> None:
    """The worker fails visibly: sustained broker unreachability past AGENT_BROKER_EXIT_BEATS
    terminates the process (SIGTERM, mocked here) so the container restart policy can recover
    it -- the SECOND acceptable outcome named by the bead's regression-test acceptance bullet."""
    ctx = _make_ctx()

    with (
        caplog.at_level("ERROR", logger="phaze.tasks.heartbeat"),
        patch("phaze.tasks.heartbeat._terminate_worker_process") as terminate,
    ):
        for _ in range(AGENT_BROKER_EXIT_BEATS):
            terminate.assert_not_called()
            await send_heartbeat(ctx)

        terminate.assert_called_once()

    assert any(r.levelname == "ERROR" and "exiting" in r.getMessage() for r in caplog.records)


async def test_loop_reproduces_unreachable_then_reachable_shape(caplog: pytest.LogCaptureFixture) -> None:
    """Full-loop reproduction of the bead's own scenario: broker unreachable at/shortly after
    startup (queue.info() fails every tick), then reachable -- the loop, driven exactly as the
    agent worker drives it, ends up consuming again without operator intervention."""
    ctx = _make_ctx()
    # A couple of beats past the withhold threshold stay unreachable, so the withheld/ERROR
    # window is actually exercised before the broker "comes back" (+1 alone would recover on
    # the very tick withholding first kicks in, racing the assertion below).
    switch_to_healthy_after = AGENT_BROKER_UNHEALTHY_BEATS + 2
    calls = {"n": 0}
    healthy_info = AsyncMock(return_value={"queued": 1, "active": 0, "scheduled": 0, "name": "q", "workers": {}, "jobs": []})

    real_info = ctx["worker"].queue.info

    async def _switching_info() -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] > switch_to_healthy_after:
            return await healthy_info()
        return await real_info()

    ctx["worker"].queue.info = _switching_info

    with (
        caplog.at_level("ERROR", logger="phaze.tasks.heartbeat"),
        patch("phaze.tasks.heartbeat.AGENT_HEARTBEAT_INTERVAL_SECONDS", 0),
    ):
        task = asyncio.create_task(_heartbeat_loop(ctx))
        # The first AGENT_BROKER_UNHEALTHY_BEATS - 1 beats still post (pre-threshold
        # tolerance); a POST beyond that count can only be a genuine post-recovery beat.
        recovery_target = AGENT_BROKER_UNHEALTHY_BEATS
        await _wait_until(lambda: ctx["api_client"].heartbeat.await_count >= recovery_target, timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert ctx["api_client"].heartbeat.await_count >= recovery_target, "the worker never resumed posting once the broker recovered"
    assert any(r.levelname == "ERROR" for r in caplog.records), "the unreachable window must have been logged loudly"
