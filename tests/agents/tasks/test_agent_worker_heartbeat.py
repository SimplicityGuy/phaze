"""Phase 46 — agent_worker wiring tests for the background-task heartbeat.

Asserts the heartbeat is launched as an asyncio background task in startup and
cancelled in shutdown, and that the old SAQ ``heartbeat_tick`` CronJob + function
registration are gone from ``settings`` (they competed for ``worker_max_jobs``
dispatch slots and starved the heartbeat under load — the Phase 46 incident).

Kept Postgres/DB-free so the module-level import-boundary (Phase 26 D-25,
enforced by tests/test_task_split.py) stays clean.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock


if TYPE_CHECKING:
    import pytest


def _set_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum env so importing/constructing agent_worker succeeds (no connections)."""
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test-id")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/var/empty")
    monkeypatch.setenv("PHAZE_QUEUE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")
    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://localhost:6379/0")


def test_settings_has_no_heartbeat_cron_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """The heartbeat CronJob is removed: cron_jobs is empty/absent and references no heartbeat fn."""
    _set_agent_env(monkeypatch)
    import phaze.tasks.agent_worker as aw

    cron_jobs = aw.settings.get("cron_jobs") or []
    # The only prior cron entry was the heartbeat -> the key should now be empty/absent.
    assert not cron_jobs
    # And defensively: no CronJob anywhere references a heartbeat function.
    assert all(getattr(getattr(cj, "function", None), "__name__", "") != "heartbeat_tick" for cj in cron_jobs)


def test_heartbeat_tick_not_registered_in_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """heartbeat_tick is no longer SAQ-dispatched (would be a misleading dead registration)."""
    _set_agent_env(monkeypatch)
    from phaze.tasks import heartbeat as hb
    import phaze.tasks.agent_worker as aw

    assert hb.heartbeat_tick not in aw.settings["functions"]
    func_names = [getattr(fn, "__name__", "") for fn in aw.settings["functions"]]
    assert "heartbeat_tick" not in func_names


async def test_startup_launches_heartbeat_background_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """startup() stores an asyncio.Task at ctx['heartbeat_task'] running _heartbeat_loop."""
    _set_agent_env(monkeypatch)
    from phaze.config import AgentSettings
    import phaze.tasks.agent_worker as aw

    fake_cfg = AgentSettings()
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)

    fake_identity = MagicMock(agent_id="test-id")
    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(return_value=fake_identity)
    monkeypatch.setattr(aw, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(aw, "ensure_models_present", lambda _p: None)
    # phaze-xuec1: startup() now probes real broker reachability before "startup complete";
    # this test is about the heartbeat task wiring, not the broker, so short-circuit it.
    monkeypatch.setattr(aw, "_wait_for_queue_ready", AsyncMock())

    ctx: dict[str, Any] = {}
    try:
        await aw.startup(ctx)
        task = ctx["heartbeat_task"]
        assert isinstance(task, asyncio.Task)
        assert not task.done()
        # phaze-mvq8z.21: the heartbeat carries the config poll here, so no second poller runs.
        assert "config_poll_task" not in ctx
    finally:
        task = ctx.get("heartbeat_task")
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def test_startup_skips_heartbeat_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """quick-260707-dh1: PHAZE_AGENT_HEARTBEAT=false -> startup launches NO heartbeat task.

    Compose sets this false on 3 of the 4 lane workers so exactly one lane (analyze) runs the
    heartbeat -- an agent reports one authoritative last_seen, not N duplicates.
    """
    _set_agent_env(monkeypatch)
    monkeypatch.setenv("PHAZE_AGENT_HEARTBEAT", "false")
    from phaze.config import AgentSettings
    from phaze.schemas.agent_config import AgentConfigResponse, compute_overrides_digest
    import phaze.tasks.agent_worker as aw

    fake_cfg = AgentSettings()
    assert fake_cfg.agent_heartbeat_enabled is False
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)

    fake_identity = MagicMock(agent_id="test-id")
    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(return_value=fake_identity)
    # phaze-mvq8z.21: this worker now runs the config poll on its own loop; give it a real response.
    fake_client.get_config = AsyncMock(return_value=AgentConfigResponse(overrides={}, digest=compute_overrides_digest({})))
    monkeypatch.setattr(aw, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(aw, "ensure_models_present", lambda _p: None)
    # phaze-xuec1: startup() now probes real broker reachability before "startup complete";
    # this test is about the heartbeat task wiring, not the broker, so short-circuit it.
    monkeypatch.setattr(aw, "_wait_for_queue_ready", AsyncMock())

    ctx: dict[str, Any] = {}
    try:
        await aw.startup(ctx)
        assert "heartbeat_task" not in ctx
        # phaze-mvq8z.21: ...but it still polls the runtime config, on a loop of its own.
        assert isinstance(ctx["config_poll_task"], asyncio.Task)
    finally:
        for key in ("heartbeat_task", "config_poll_task"):
            task = ctx.get(key)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


async def test_a_heartbeat_disabled_worker_still_applies_an_admin_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-mvq8z.21: PHAZE_AGENT_HEARTBEAT=false must not also switch off DB-override delivery.

    The heartbeat loop was the agent's ONLY path to ``GET /api/internal/agent/config``, so the
    worker-drain all-mode worker (heartbeat off, still running ``process_file``) never saw an
    admin override. A heartbeat-disabled worker now runs the config poll on its own loop -- and
    still sends no heartbeat, which is what the flag is for.
    """
    _set_agent_env(monkeypatch)
    monkeypatch.setenv("PHAZE_AGENT_HEARTBEAT", "false")
    from phaze.config import AgentSettings
    from phaze.schemas.agent_config import AgentConfigResponse, compute_overrides_digest
    import phaze.tasks.agent_worker as aw
    from tests._async_settle import wait_until

    fake_cfg = AgentSettings()
    assert fake_cfg.agent_heartbeat_enabled is False
    override_stall = fake_cfg.analysis_stall_timeout_sec + 600
    overrides = {"analysis_stall_timeout_sec": override_stall}
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)

    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(return_value=MagicMock(agent_id="test-id"))
    fake_client.get_config = AsyncMock(return_value=AgentConfigResponse(overrides=overrides, digest=compute_overrides_digest(overrides)))
    monkeypatch.setattr(aw, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(aw, "ensure_models_present", lambda _p: None)
    monkeypatch.setattr(aw, "_wait_for_queue_ready", AsyncMock())

    ctx: dict[str, Any] = {}
    try:
        await aw.startup(ctx)
        store = ctx["runtime_config_store"]
        await wait_until(
            lambda: store.current().analysis_stall_timeout_sec == override_stall,
            description="the admin override reaching a heartbeat-disabled worker",
        )
        assert store.snapshot().sources["analysis_stall_timeout_sec"] == "override"
        assert "heartbeat_task" not in ctx
        fake_client.heartbeat.assert_not_awaited()
    finally:
        for key in ("heartbeat_task", "config_poll_task"):
            task = ctx.get(key)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


async def test_shutdown_cancels_heartbeat_task() -> None:
    """shutdown() cancels + awaits ctx['heartbeat_task'] cleanly (CancelledError suppressed)."""
    import phaze.tasks.agent_worker as aw

    async def _forever() -> None:
        while True:
            await asyncio.sleep(3600)

    task = asyncio.create_task(_forever())
    await asyncio.sleep(0)  # let the task start running

    ctx: dict[str, Any] = {"heartbeat_task": task}
    await aw.shutdown(ctx)

    assert task.cancelled()


async def test_shutdown_cancels_config_poll_task() -> None:
    """phaze-mvq8z.21: shutdown() also cancels a heartbeat-disabled worker's ctx['config_poll_task']."""
    import phaze.tasks.agent_worker as aw

    async def _forever() -> None:
        while True:
            await asyncio.sleep(3600)

    task = asyncio.create_task(_forever())
    await asyncio.sleep(0)

    await aw.shutdown({"config_poll_task": task})

    assert task.cancelled()


async def test_shutdown_tolerates_missing_heartbeat_task() -> None:
    """shutdown() must not raise when ctx has no heartbeat_task key (defensive)."""
    import phaze.tasks.agent_worker as aw

    await aw.shutdown({})  # should not raise
