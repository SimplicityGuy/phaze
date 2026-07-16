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
    monkeypatch.setattr(aw, "AudfprintAdapter", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(aw, "PanakoAdapter", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(aw, "FingerprintOrchestrator", lambda **_k: MagicMock(engines=[]))
    monkeypatch.setattr(aw, "ensure_models_present", lambda _p: None)

    ctx: dict[str, Any] = {}
    try:
        await aw.startup(ctx)
        task = ctx["heartbeat_task"]
        assert isinstance(task, asyncio.Task)
        assert not task.done()
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
    import phaze.tasks.agent_worker as aw

    fake_cfg = AgentSettings()
    assert fake_cfg.agent_heartbeat_enabled is False
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)

    fake_identity = MagicMock(agent_id="test-id")
    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(return_value=fake_identity)
    monkeypatch.setattr(aw, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(aw, "AudfprintAdapter", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(aw, "PanakoAdapter", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(aw, "FingerprintOrchestrator", lambda **_k: MagicMock(engines=[]))
    monkeypatch.setattr(aw, "ensure_models_present", lambda _p: None)

    ctx: dict[str, Any] = {}
    try:
        await aw.startup(ctx)
        assert "heartbeat_task" not in ctx
    finally:
        task = ctx.get("heartbeat_task")
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


async def test_shutdown_tolerates_missing_heartbeat_task() -> None:
    """shutdown() must not raise when ctx has no heartbeat_task key (defensive)."""
    import phaze.tasks.agent_worker as aw

    await aw.shutdown({})  # should not raise
