"""Unit tests for the startup dequeue-readiness probe (bead phaze-xuec1).

The 2026-08-08 nox incident: `phaze-agent-worker-analyze` logged a textbook-clean
`startup complete` and then dequeued nothing for 1h41m. One theory (the design doc's
"Likely context") is a broker/PgBouncer blip DURING startup -- SAQ's own
`worker.queue.connect()` opens the psycopg3 pool non-blocking (`pool.open(wait=False)`)
and always "succeeds" immediately, so it proves nothing about actual reachability.

`_wait_for_queue_ready` closes that specific gap: `startup complete` must not be logged
while the worker cannot prove (via `queue.info()`, the same broker path `_dequeue()`
needs) that it can reach its broker.

Covers:
1. `_wait_for_queue_ready` returns cleanly once `queue.info()` succeeds, after retrying
   through transient failures (no real sleep -- the backoff tuple is patched to zeros).
2. `_wait_for_queue_ready` raises RuntimeError, naming the last error, once the retry
   budget is exhausted without a single success.
3. End-to-end: `startup()` propagates that RuntimeError and never logs "startup complete"
   when the broker never becomes reachable -- the acceptance-criterion wording, verified
   directly against captured stdout (PR3 routes structlog through the root pipeline, not
   caplog -- see the existing banner test for the same pattern).
4. End-to-end happy path: `startup()` DOES log "startup complete" once the probe
   eventually succeeds (self-recovery within the retry budget).
"""

from __future__ import annotations

import contextlib
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_module_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the ONE env var required at module-import time (phaze.tasks.agent_worker:290-295).

    Only matters when this test file's import is the FIRST in the process to import
    ``phaze.tasks.agent_worker`` -- once imported, Python caches the module and later
    imports (even under different env) are no-ops. Setting it defensively here keeps
    this file runnable standalone, not just as part of the full suite.
    """
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test-id")


async def _cancel_heartbeat_task(ctx: dict[str, Any]) -> None:
    """Cancel + await ctx["heartbeat_task"] if startup() got far enough to launch it.

    Prevents "Task was destroyed but it is pending" noise: these tests call startup()
    directly (never through saq.worker.Worker, which owns the real cancel-on-shutdown
    path), so the background heartbeat loop needs the same cleanup by hand.
    """
    heartbeat_task = ctx.get("heartbeat_task")
    if heartbeat_task is not None:
        heartbeat_task.cancel()
        with contextlib.suppress(BaseException):
            await heartbeat_task


async def test_wait_for_queue_ready_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient failures are retried; a later success returns cleanly."""
    _ensure_module_importable(monkeypatch)
    import phaze.tasks.agent_worker as aw

    monkeypatch.setattr(aw, "_QUEUE_READY_BACKOFF_S", (0.0, 0.0))
    info = AsyncMock(side_effect=[RuntimeError("network unreachable"), RuntimeError("network unreachable"), {"queued": 0}])
    monkeypatch.setattr(aw.queue, "info", info)

    await aw._wait_for_queue_ready()

    assert info.await_count == 3


async def test_wait_for_queue_ready_raises_after_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every attempt fails -> RuntimeError naming the last error, budget exhausted."""
    _ensure_module_importable(monkeypatch)
    import phaze.tasks.agent_worker as aw

    monkeypatch.setattr(aw, "_QUEUE_READY_BACKOFF_S", (0.0, 0.0))
    info = AsyncMock(side_effect=RuntimeError("connection to server ... failed: Network is unreachable"))
    monkeypatch.setattr(aw.queue, "info", info)

    with pytest.raises(RuntimeError, match="cannot reach its broker"):
        await aw._wait_for_queue_ready()

    # 2 backoff attempts + 1 final = 3 calls, mirroring _whoami_with_retry's contract.
    assert info.await_count == 3


def _patch_common_startup_bits(monkeypatch: pytest.MonkeyPatch, aw: Any) -> None:
    """Shared plumbing so startup() reaches the queue-readiness probe without real I/O."""
    from phaze.config import AgentSettings

    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_SECRET-BYTES-ABCDEF1234567890")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test-id")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/var/empty")
    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://localhost:6379/0")

    fake_cfg = AgentSettings()
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)

    fake_identity = MagicMock(agent_id="test-id")
    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(return_value=fake_identity)
    fake_client.close = AsyncMock()
    monkeypatch.setattr(aw, "construct_agent_client", lambda _cfg: fake_client)

    monkeypatch.setattr(pathlib.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(aw, "ensure_models_present", lambda _models_dir: None)


async def test_startup_never_logs_complete_when_broker_stays_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Acceptance criterion, verified directly: `startup complete` is not logged while the
    worker cannot prove it can dequeue -- and the failure is loud (RuntimeError propagates,
    matching the existing queue/token-mismatch "exit non-zero" posture in this module)."""
    _ensure_module_importable(monkeypatch)
    import phaze.tasks.agent_worker as aw

    _patch_common_startup_bits(monkeypatch, aw)
    monkeypatch.setattr(aw, "_QUEUE_READY_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(aw.queue, "info", AsyncMock(side_effect=RuntimeError("Network is unreachable")))

    ctx: dict[str, Any] = {}
    with pytest.raises(RuntimeError, match="cannot reach its broker"):
        await aw.startup(ctx)
    await _cancel_heartbeat_task(ctx)

    out = capsys.readouterr().out
    assert "startup complete" not in out, f"startup complete logged despite an unreachable broker: {out!r}"


async def test_startup_logs_complete_once_broker_becomes_reachable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Self-recovery: a broker unreachable at startup that becomes reachable within the
    retry budget still reaches a genuine `startup complete` -- the worker ends up able to
    dequeue rather than failing visibly, the OTHER acceptable outcome named by the
    regression-test acceptance bullet."""
    _ensure_module_importable(monkeypatch)
    import phaze.tasks.agent_worker as aw

    _patch_common_startup_bits(monkeypatch, aw)
    monkeypatch.setattr(aw, "_QUEUE_READY_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(
        aw.queue,
        "info",
        AsyncMock(side_effect=[RuntimeError("Network is unreachable"), {"queued": 0}]),
    )

    ctx: dict[str, Any] = {}
    await aw.startup(ctx)
    await _cancel_heartbeat_task(ctx)

    out = capsys.readouterr().out
    assert "startup complete" in out, f"startup complete never logged despite the broker recovering: {out!r}"
