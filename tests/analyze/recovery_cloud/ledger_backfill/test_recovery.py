"""Saq ledger parsing, backfill, and startup recovery ordering."""

from __future__ import annotations

import contextlib
from typing import Any
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.tasks.reenqueue import (
    _ledger_fids,
)


_MODELS_PATH = "/models"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_count_inflight_jobs_reads_real_saq_jobs() -> None:
    """Against the real broker, count_inflight_jobs reads the live saq_jobs depth (>=1 after enqueue).

    Self-contained (the stage_env fixture lives under tests/integration/, out of reach here), so this
    mirrors test_reenqueue.test_real_broker_dedup_returns_none: probe Postgres, build a real
    PostgresQueue, enqueue a real keyed process_file job, and assert count_inflight_jobs over an
    AsyncSession on the SAME DB rises by >=1. Skips when Postgres is unavailable; cleans up after.
    """
    import os

    import psycopg
    from sqlalchemy.ext.asyncio import create_async_engine

    from phaze.services.agent_task_router import AgentTaskRouter
    from phaze.services.analysis_enqueue import enqueue_process_file
    from phaze.services.pipeline import count_inflight_jobs
    from tests.db_guard import integration_dsns

    redis_url = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6379/0")
    raw_dsn, sa_dsn = integration_dsns()

    # Probe broker connectivity FIRST so the skip path creates nothing to clean up.
    try:
        probe = await psycopg.AsyncConnection.connect(raw_dsn)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    router = AgentTaskRouter(queue_url=raw_dsn, cache_redis_url=redis_url)
    queue = router.queue_for("recovery-itest", "analyze")
    await queue.connect()  # opens the psycopg pool + init_db() (creates saq_jobs)
    engine = create_async_engine(sa_dsn)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    file = FileRecord(
        id=uuid.uuid4(),
        sha256_hash="0" * 64,
        original_path="/music/recovery-itest.mp3",
        original_filename="recovery-itest.mp3",
        current_path="/music/recovery-itest.mp3",
        file_type="mp3",
        file_size=2048,
        agent_id="recovery-itest",
    )

    job = None
    try:
        async with session_factory() as ro_session:
            before = await count_inflight_jobs(ro_session)

        job = await enqueue_process_file(queue, file, "recovery-itest", _MODELS_PATH)
        assert job is not None

        async with session_factory() as ro_session:
            after = await count_inflight_jobs(ro_session)
        assert after >= 1
        assert after > before
    finally:
        if job is not None:
            with contextlib.suppress(Exception):
                await queue.abort(job, "test cleanup")
        await router.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_startup_backfills_ledger_before_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """controller.startup calls backfill_ledger_from_saq_jobs BEFORE recover_orphaned_work.

    The one-time idempotent backfill (Plan 04) must seed the ledger from live saq_jobs BEFORE the
    gated boot recovery reads it, so the in-flight cohort is recoverable on first boot (no blind
    window). Both run in their OWN try/except so neither aborts boot. We spy on both controller-side
    names with a shared call-order list and assert backfill precedes recovery, each awaited once.
    """
    import contextlib as _contextlib
    from unittest.mock import AsyncMock, MagicMock

    # Patch the heavyweight startup constructors so no real connections open.
    monkeypatch.setattr("phaze.database.create_async_engine", lambda *_a, **_kw: MagicMock())

    # async_session() must return an async-context-manager session so the backfill's
    # `async with ctx["async_session"]() as session` works against the spy.
    @_contextlib.asynccontextmanager
    async def _fake_session_cm() -> Any:
        yield MagicMock(name="session", commit=AsyncMock())

    def _fake_sessionmaker(*_a: Any, **_kw: Any) -> Any:
        return _fake_session_cm

    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", _fake_sessionmaker)
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
    fake_cfg.anthropic_api_key = None
    fake_cfg.openai_api_key = None
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)

    router_stub = MagicMock(name="AgentTaskRouterStub")
    router_stub.close = AsyncMock()
    router_stub.queue_for = MagicMock()
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: router_stub)

    call_order: list[str] = []

    async def _spy_backfill(_session: Any) -> dict[str, int]:
        call_order.append("backfill")
        return {"inserted": 0, "skipped": 0}

    async def _spy_recover(_ctx: dict[str, Any]) -> dict[str, Any]:
        call_order.append("recover")
        return {"detected_loss": False, "forced": False, "stages": {}}

    backfill_mock = AsyncMock(side_effect=_spy_backfill)
    recover_mock = AsyncMock(side_effect=_spy_recover)
    monkeypatch.setattr("phaze.tasks.controller.backfill_ledger_from_saq_jobs", backfill_mock)
    monkeypatch.setattr("phaze.tasks.controller.recover_orphaned_work", recover_mock)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    backfill_mock.assert_awaited_once()
    recover_mock.assert_awaited_once_with(ctx)
    assert call_order == ["backfill", "recover"], f"backfill must run before recovery, got {call_order}"


@pytest.mark.asyncio
async def test_startup_survives_raising_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backfill failure must NEVER abort controller boot, and recovery must still run after it."""
    import contextlib as _contextlib
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr("phaze.database.create_async_engine", lambda *_a, **_kw: MagicMock())

    @_contextlib.asynccontextmanager
    async def _fake_session_cm() -> Any:
        yield MagicMock(name="session", commit=AsyncMock())

    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: _fake_session_cm)
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
    fake_cfg.anthropic_api_key = None
    fake_cfg.openai_api_key = None
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)

    router_stub = MagicMock(name="AgentTaskRouterStub")
    router_stub.close = AsyncMock()
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: router_stub)

    backfill_mock = AsyncMock(side_effect=RuntimeError("backfill boom"))
    recover_mock = AsyncMock(return_value={"detected_loss": False, "forced": False, "stages": {}})
    monkeypatch.setattr("phaze.tasks.controller.backfill_ledger_from_saq_jobs", backfill_mock)
    monkeypatch.setattr("phaze.tasks.controller.recover_orphaned_work", recover_mock)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    # Must NOT raise -- the backfill's own try/except swallows the failure.
    await controller.startup(ctx)

    backfill_mock.assert_awaited_once()
    # Recovery still runs even though the backfill failed (independent try/except blocks).
    recover_mock.assert_awaited_once_with(ctx)


def test_ledger_fids_skips_a_non_uuid_natural_id() -> None:
    """A row whose ``payload["file_id"]`` is not UUID-parseable is dropped from the scope, not raised.

    ``_ledger_fids`` feeds the ``= ANY(:fids)`` bind for the done-set queries; a controller set-hash
    or malformed id can never match a per-file output-table row, so skipping it is a pure
    optimization -- proven here by checking the well-formed sibling row still makes it through.
    """
    good_id = uuid.uuid4()
    good_row = SchedulingLedger(key="push_file:good", function="push_file", routing="agent", payload={"file_id": str(good_id)})
    bad_row = SchedulingLedger(key="push_file:bad", function="push_file", routing="agent", payload={"file_id": "not-a-uuid"})

    fids = _ledger_fids([good_row, bad_row])

    assert fids == [good_id]
