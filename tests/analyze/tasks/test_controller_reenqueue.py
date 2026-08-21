"""Phase 42 Plan 02: controller wiring for the recovery-only automation gate.

Two groups:
1. Registration (``-k cron`` / ``-k functions``) -- ``recover_orphaned_work`` is in
   ``settings["functions"]``; the legacy ``reenqueue_discovered`` is FULLY removed (no
   import, no function, no cron); the every-5-min ``*/5 * * * *`` auto-advance cron is
   GONE; the existing ``reap_stalled_scans`` cron remains; and ``refresh_tracklists`` is
   registered as a function but is deliberately NOT a cron (phaze-2akf).
2. Startup behavior (``-k startup``) -- patch the heavyweight constructors, stash
   ``ctx["task_router"]``, await the gated ``recover_orphaned_work(ctx)`` once on boot,
   close the router in shutdown, and prove a raising recovery never aborts boot.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


@contextlib.asynccontextmanager
async def _fake_session_cm() -> Any:
    """An async-context-manager session stub so the startup ledger backfill's `async with` works."""
    yield MagicMock(name="session", commit=AsyncMock())


def _patch_startup_constructors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the heavyweight startup constructors so no real connections open."""
    monkeypatch.setattr("phaze.tasks.controller.create_async_engine", lambda *_a, **_kw: MagicMock())
    # async_sessionmaker(...) must return a CALLABLE that yields an async-cm session: the Phase-45
    # startup ledger backfill opens `async with ctx["async_session"]() as session`.
    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: lambda *_a2, **_kw2: _fake_session_cm())
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
    # startup() bridges the LLM keys into os.environ; pin to None so the MagicMock
    # doesn't yield a non-str auto-attribute for the env assignment.
    fake_cfg.anthropic_api_key = None
    fake_cfg.openai_api_key = None
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


def test_functions_list_includes_recover_orphaned_work() -> None:
    """recover_orphaned_work must be registered as a controller task function."""
    from phaze.tasks import controller
    from phaze.tasks.reenqueue import recover_orphaned_work

    assert recover_orphaned_work in controller.settings["functions"], "recover_orphaned_work not registered in settings['functions']"


def test_reenqueue_discovered_is_fully_removed() -> None:
    """The legacy reenqueue_discovered producer must be gone -- no import, no function, no registration."""
    import phaze.tasks.controller as controller
    import phaze.tasks.reenqueue as reenqueue

    # The function no longer exists on the reenqueue module.
    assert not hasattr(reenqueue, "reenqueue_discovered"), "reenqueue_discovered should be deleted from phaze.tasks.reenqueue"
    # The controller no longer imports it under any name.
    assert not hasattr(controller, "reenqueue_discovered"), "phaze.tasks.controller should no longer import reenqueue_discovered"
    # No registered function still carries that name.
    fn_names = {getattr(fn, "__name__", "") for fn in controller.settings["functions"]}
    assert "reenqueue_discovered" not in fn_names, "reenqueue_discovered must not remain in settings['functions']"


def test_no_auto_advance_cron() -> None:
    """No GENERAL pipeline auto-advance cron survives, and recovery is never a CronJob.

    Phase 50: the ONLY ``*/5 * * * *`` cron permitted is ``stage_cloud_window`` -- a NARROW cron
    scoped solely to the bounded cloud-window top-up (CLOUDPIPE-01; it replaced the Phase-49
    ``release_awaiting_cloud`` drain). It is NOT a general pipeline auto-advance (the deleted
    ``reenqueue_discovered`` premise), so the schedule-string ban is refined to "no */5 cron OTHER
    than the bounded staging cron".

    Phase 54 (KSUBMIT-06): ``reconcile_cloud_jobs`` joins the sanctioned-narrow allow-list -- an
    every-minute safety-net that owns the K8s Job lifecycle (in-flight reconcile + bounded re-drive + terminal
    cleanup). Like ``stage_cloud_window`` it is bounded and idempotent, NOT a general pipeline
    auto-advance, so the ban stays "no */5 cron OTHER than the sanctioned narrow crons".

    phaze-2u8v.2: ``reap_resolved_ledger_rows`` joins the allow-list, and it is the SAFEST possible
    member -- it is not merely bounded, it is structurally incapable of being an auto-advance, because
    it enqueues nothing at all. The assertion below proves that from the module source rather than
    taking the allow-list entry on trust, so widening the list here did not weaken the guard: a future
    edit that teaches the reaper to enqueue turns this test RED even though it is allow-listed.
    """
    import inspect

    from phaze.tasks import controller, ledger_reaper
    from phaze.tasks.ledger_reaper import reap_resolved_ledger_rows
    from phaze.tasks.reconcile_cloud_jobs import reconcile_cloud_jobs
    from phaze.tasks.reenqueue import recover_orphaned_work
    from phaze.tasks.release_awaiting_cloud import stage_cloud_window

    sanctioned_narrow_crons = {stage_cloud_window, reconcile_cloud_jobs, reap_resolved_ledger_rows}
    cron_jobs = controller.settings["cron_jobs"]
    # Steady-state produces ZERO GENERAL auto-advance enqueues -- the only */5 crons are the bounded
    # cloud-window top-up and the K8s reconcile safety-net; any OTHER */5 cron is a forbidden
    # general auto-advance.
    offenders = [cj for cj in cron_jobs if getattr(cj, "cron", "") == "*/5 * * * *" and cj.function not in sanctioned_narrow_crons]
    assert offenders == [], "no general */5 auto-advance cron may survive (only the sanctioned narrow crons are allowed)"
    # recover_orphaned_work is startup/manual-only -- it must NEVER be wired as a cron.
    assert all(cj.function is not recover_orphaned_work for cj in cron_jobs), "recover_orphaned_work must not be a CronJob (startup/manual-only)"
    # The allow-list entry above is EARNED, not asserted: the reaper only ever DELETEs resolved ledger
    # rows, so no producer verb may appear in its source.
    reaper_src = inspect.getsource(ledger_reaper)
    for producer in (".enqueue(", "task_router", "queue_for("):
        assert producer not in reaper_src, f"the ledger reaper must never produce work (found {producer!r})"


def test_cron_does_not_regress_existing_jobs() -> None:
    """The reap_stalled_scans cron must remain -- and refresh_tracklists must NOT be one.

    phaze-2akf inverted the second half of this assertion deliberately, so read it as the record of
    a decision rather than as a relaxation. The monthly ``refresh_tracklists`` cron re-fetched every
    tracklist older than 90 days, which directly contradicts the drain's cache ("a published
    tracklist does not change, so never re-fetch") and made a second, unbounded consumer of a
    whole-host budget of ~1 request / 8 s. The operator decision (2026-08-03, phaze-2akf) was: the
    drain keeps never re-fetching, and refresh becomes on-demand and targeted. Re-adding the cron
    re-opens that, which is why the absence is asserted rather than merely un-asserted.
    """
    from phaze.tasks import controller
    from phaze.tasks.scan_reaper import reap_stalled_scans
    from phaze.tasks.tracklist import refresh_tracklists

    cron_functions = {cj.function for cj in controller.settings["cron_jobs"]}
    assert reap_stalled_scans in cron_functions, "reap_stalled_scans cron regressed (missing)"
    assert refresh_tracklists not in cron_functions, "refresh_tracklists must stay on-demand -- never a cron (phaze-2akf)"
    assert refresh_tracklists in controller.settings["functions"], "refresh_tracklists must stay operator-enqueueable"


# --------------------------------------------------------------------------- #
# Group 2: startup behavior                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_startup_stashes_router_and_calls_recovery_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """startup() stashes ctx['task_router'] and awaits the gated recover_orphaned_work(ctx) exactly once."""
    _patch_startup_constructors(monkeypatch)

    router_stub = _make_router_stub()
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: router_stub)

    recover_mock = AsyncMock(return_value={"detected_loss": False, "forced": False, "stages": {}})
    monkeypatch.setattr("phaze.tasks.controller.recover_orphaned_work", recover_mock)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    assert ctx["task_router"] is router_stub, "startup did not stash the AgentTaskRouter in ctx['task_router']"
    # Gated boot recovery: force defaults False so a durable Phase-36 restart is a no-op.
    recover_mock.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_shutdown_closes_task_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """shutdown() awaits task_router.close() when a router is present in ctx."""
    from phaze.tasks import controller

    router_stub = _make_router_stub()
    ctx: dict[str, Any] = {"task_router": router_stub}
    await controller.shutdown(ctx)

    router_stub.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_survives_raising_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recovery failure must NEVER abort controller boot (boot resilience).

    phaze-sekvl: a non-``DBAPIError`` failure (a real bug, not the schema-race this bead fixes) is
    NOT retried -- it is logged once and boot proceeds, exactly as before ``_run_boot_reconcile_with_retry``
    was introduced. The retry budget is reserved for the specific "schema not ready yet" shape,
    covered separately by ``test_startup_recovery_retries_transient_dbapi_errors_then_succeeds``.
    """
    _patch_startup_constructors(monkeypatch)

    router_stub = _make_router_stub()
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: router_stub)

    recover_mock = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr("phaze.tasks.controller.recover_orphaned_work", recover_mock)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    # Must NOT raise -- the broad try/except swallows the failure.
    await controller.startup(ctx)

    recover_mock.assert_awaited_once_with(ctx)
    # The router is still stashed (built before the guarded call), so shutdown can close it.
    assert ctx["task_router"] is router_stub


@pytest.mark.asyncio
async def test_startup_recovery_retries_transient_dbapi_errors_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact race this bead fixes: the schema isn't ready yet on the first attempt(s), but the
    api's migration lands before the bounded retry budget is exhausted (phaze-sekvl).

    A missing table/column surfaces through SQLAlchemy as a ``DBAPIError`` subclass (asyncpg's
    ``UndefinedTableError`` arrives wrapped as ``ProgrammingError``) -- fail ``recover_orphaned_work``
    with exactly that shape twice (simulating the worker racing ahead of the api's
    ``alembic upgrade head``), then succeed on the third attempt, and assert startup surfaces the
    eventual success rather than giving up after one try. The retry delay is zeroed so the test
    doesn't actually sleep.
    """
    from sqlalchemy.exc import ProgrammingError

    _patch_startup_constructors(monkeypatch)
    monkeypatch.setattr("phaze.tasks.controller._BOOT_RECONCILE_RETRY_DELAY_SECONDS", 0.0)

    router_stub = _make_router_stub()
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: router_stub)

    def _schema_not_ready() -> ProgrammingError:
        return ProgrammingError("SELECT 1 FROM saq_jobs", {}, Exception('relation "saq_jobs" does not exist'))

    success_result = {"detected_loss": False, "forced": False, "stages": {}}
    recover_mock = AsyncMock(side_effect=[_schema_not_ready(), _schema_not_ready(), success_result])
    monkeypatch.setattr("phaze.tasks.controller.recover_orphaned_work", recover_mock)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    assert recover_mock.await_count == 3, "must retry past the transient DBAPIError failures and stop once it succeeds"
