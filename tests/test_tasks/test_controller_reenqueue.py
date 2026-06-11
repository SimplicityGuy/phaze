"""Phase 32 Plan 03: controller wiring for reboot re-enqueue.

Two groups:
1. Registration (``-k cron`` / ``-k functions``) -- ``reenqueue_discovered`` is in
   ``settings["functions"]`` and a ``CronJob(reenqueue_discovered, cron="*/5 * * * *")``
   is in ``settings["cron_jobs"]``, with no regression to the existing
   ``reap_stalled_scans`` / ``refresh_tracklists`` crons.
2. Startup behavior (``-k startup``) -- mirrors ``test_controller_startup_banner.py``:
   patch the heavyweight constructors, stash ``ctx["task_router"]``, await
   ``reenqueue_discovered(ctx)`` once on boot, close the router in shutdown, and
   prove a raising re-enqueue never aborts boot.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _patch_startup_constructors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the heavyweight startup constructors so no real connections open."""
    monkeypatch.setattr("phaze.tasks.controller.create_async_engine", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.DiscogsographyClient", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.load_prompt_template", lambda: "stub")
    monkeypatch.setattr("phaze.tasks.controller.ProposalService", lambda *_a, **_kw: MagicMock())

    fake_cfg = MagicMock()
    fake_cfg.redis_url = "redis://localhost:6379/0"
    fake_cfg.database_url = "postgresql+asyncpg://test"
    fake_cfg.debug = False
    fake_cfg.discogsography_url = "http://test"
    fake_cfg.llm_model = "stub-model"
    fake_cfg.llm_max_rpm = 60
    fake_cfg.log_level = "INFO"
    fake_cfg.log_json = True
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)


def _make_router_stub() -> MagicMock:
    """A stand-in AgentTaskRouter exposing an async ``close()`` and ``queue_for``."""
    stub = MagicMock(name="AgentTaskRouterStub")
    stub.close = AsyncMock()
    stub.queue_for = MagicMock()
    return stub


# --------------------------------------------------------------------------- #
# Group 1: registration                                                       #
# --------------------------------------------------------------------------- #


def test_functions_list_includes_reenqueue_discovered() -> None:
    """reenqueue_discovered must be registered as a controller task function."""
    from phaze.tasks import controller
    from phaze.tasks.reenqueue import reenqueue_discovered

    assert reenqueue_discovered in controller.settings["functions"], "reenqueue_discovered not registered in settings['functions']"


def test_cron_registers_reenqueue_every_five_minutes() -> None:
    """A CronJob(reenqueue_discovered, cron='*/5 * * * *') must be registered."""
    from phaze.tasks import controller
    from phaze.tasks.reenqueue import reenqueue_discovered

    cron_jobs = controller.settings["cron_jobs"]
    matches = [cj for cj in cron_jobs if cj.function is reenqueue_discovered]
    assert len(matches) == 1, f"expected exactly one reenqueue_discovered CronJob, found {len(matches)}"
    assert matches[0].cron == "*/5 * * * *", f"reenqueue_discovered cron should be every 5 min, got {matches[0].cron!r}"


def test_cron_does_not_regress_existing_jobs() -> None:
    """The existing reap_stalled_scans + refresh_tracklists crons must remain."""
    from phaze.tasks import controller
    from phaze.tasks.scan_reaper import reap_stalled_scans
    from phaze.tasks.tracklist import refresh_tracklists

    cron_functions = {cj.function for cj in controller.settings["cron_jobs"]}
    assert reap_stalled_scans in cron_functions, "reap_stalled_scans cron regressed (missing)"
    assert refresh_tracklists in cron_functions, "refresh_tracklists cron regressed (missing)"


# --------------------------------------------------------------------------- #
# Group 2: startup behavior                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_startup_stashes_router_and_calls_reenqueue_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """startup() stashes ctx['task_router'] and awaits reenqueue_discovered(ctx) exactly once."""
    _patch_startup_constructors(monkeypatch)

    router_stub = _make_router_stub()
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: router_stub)

    reenqueue_mock = AsyncMock(return_value={"reenqueued": 3, "skipped": 1})
    monkeypatch.setattr("phaze.tasks.controller.reenqueue_discovered", reenqueue_mock)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    assert ctx["task_router"] is router_stub, "startup did not stash the AgentTaskRouter in ctx['task_router']"
    reenqueue_mock.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_shutdown_closes_task_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """shutdown() awaits task_router.close() when a router is present in ctx."""
    from phaze.tasks import controller

    router_stub = _make_router_stub()
    ctx: dict[str, Any] = {"task_router": router_stub}
    await controller.shutdown(ctx)

    router_stub.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_survives_raising_reenqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-enqueue failure must NEVER abort controller boot (boot resilience)."""
    _patch_startup_constructors(monkeypatch)

    router_stub = _make_router_stub()
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: router_stub)

    reenqueue_mock = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr("phaze.tasks.controller.reenqueue_discovered", reenqueue_mock)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    # Must NOT raise -- the broad try/except swallows the failure.
    await controller.startup(ctx)

    reenqueue_mock.assert_awaited_once_with(ctx)
    # The router is still stashed (built before the guarded call), so shutdown can close it.
    assert ctx["task_router"] is router_stub
