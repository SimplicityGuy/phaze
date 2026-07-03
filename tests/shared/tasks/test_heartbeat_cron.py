"""Phase 29 D-07..D-10 — happy-path tests for the SAQ heartbeat cron handler.

Covers:

* Success path: heartbeat_tick reads ctx, builds HeartbeatRequest with
  agent_version (from importlib.metadata), worker_pid (from os.getpid), and
  queue_depth (from ctx["worker"].queue.info()["queued"]), then POSTs via
  ctx["api_client"].heartbeat (D-08, D-10).
* Ctx-missing path: empty ctx logs WARNING and returns gracefully — no
  exception escapes (defensive guard for restart races).
* Queue.info() failure: heartbeat still goes out with queue_depth=0
  (D-10 defensive default; cron must not crash on transient queue read).
* importlib.metadata source: payload's agent_version equals the real
  installed `phaze` package version (0.1.0 at the time of writing).

All tests use `unittest.mock.patch` for `os.getpid` to make `worker_pid`
deterministic, and rely on pytest-asyncio `asyncio_mode = "auto"` so plain
`async def test_*` functions are collected without an explicit decorator.
"""

from __future__ import annotations

from datetime import UTC, datetime
import importlib.metadata
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from phaze.schemas.agent_heartbeat import HeartbeatRequest
from phaze.schemas.agent_identity import AgentIdentity
from phaze.tasks.heartbeat import heartbeat_tick


if TYPE_CHECKING:
    import pytest


def _make_ctx(*, queued: int = 5, raise_info: bool = False) -> dict[str, Any]:
    """Build a ctx dict shaped like the one SAQ injects into a cron handler.

    Mirrors RESEARCH Pattern 5 + Pitfall 8: SAQ pre-populates ``ctx["worker"]``
    in ``Worker.__init__`` and the agent_worker.startup hook adds
    ``api_client`` and ``agent_identity``. Queue access is via
    ``ctx["worker"].queue`` -- NEVER ``ctx["queue"]``.
    """
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


async def test_heartbeat_success(caplog: pytest.LogCaptureFixture) -> None:
    """D-08, D-10: heartbeat_tick POSTs once with the expected payload shape."""
    ctx = _make_ctx(queued=7)

    with (
        patch("phaze.tasks.heartbeat.os.getpid", return_value=12345),
        patch("phaze.tasks.heartbeat.importlib.metadata.version", return_value="0.1.0"),
    ):
        await heartbeat_tick(ctx)

    client = ctx["api_client"]
    assert client.heartbeat.await_count == 1
    call_args = client.heartbeat.await_args
    # Signature: heartbeat(payload) — positional or kwarg both acceptable.
    payload = call_args.args[0] if call_args.args else call_args.kwargs["payload"]
    assert isinstance(payload, HeartbeatRequest)
    assert payload.agent_version == "0.1.0"
    assert payload.worker_pid == 12345
    assert payload.queue_depth == 7


async def test_heartbeat_skips_when_ctx_missing(caplog: pytest.LogCaptureFixture) -> None:
    """Defensive guard: missing api_client / agent_identity → WARNING + return.

    No exception escapes; lets SAQ keep the cron running while the startup hook
    races to initialise ctx during worker restarts.
    """
    ctx: dict[str, Any] = {"worker": MagicMock(), "job": MagicMock()}

    with caplog.at_level("WARNING", logger="phaze.tasks.heartbeat"):
        await heartbeat_tick(ctx)

    assert "heartbeat_tick: ctx not initialized" in caplog.text


async def test_heartbeat_queue_info_failure_defaults_to_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-10 defensive default: queue.info() exception → queue_depth=0, still POST."""
    ctx = _make_ctx(raise_info=True)

    with caplog.at_level("WARNING", logger="phaze.tasks.heartbeat"):
        await heartbeat_tick(ctx)

    client = ctx["api_client"]
    assert client.heartbeat.await_count == 1
    call_args = client.heartbeat.await_args
    payload = call_args.args[0] if call_args.args else call_args.kwargs["payload"]
    assert isinstance(payload, HeartbeatRequest)
    assert payload.queue_depth == 0
    assert "queue.info() failed" in caplog.text


async def test_heartbeat_agent_version_from_importlib() -> None:
    """agent_version sources from importlib.metadata.version('phaze') — not hardcoded."""
    ctx = _make_ctx()

    await heartbeat_tick(ctx)

    expected_version = importlib.metadata.version("phaze")
    client = ctx["api_client"]
    call_args = client.heartbeat.await_args
    payload = call_args.args[0] if call_args.args else call_args.kwargs["payload"]
    assert isinstance(payload, HeartbeatRequest)
    assert payload.agent_version == expected_version
