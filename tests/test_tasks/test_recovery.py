"""Tests for the Phase-42 gated, all-stages recovery producer (phaze.tasks.reenqueue).

``recover_orphaned_work(ctx, *, force=False)`` reconciles ALL eight pipeline stages after a
detected queue-loss, re-enqueuing each stage's shared pending set through the IDENTICAL keyed
producer the manual DAG triggers use. It must:

  - be a NO-OP on a durable Phase-36 restart (saq_jobs still has queued/active rows) -- D-02,
  - reconcile EVERY stage onto the CORRECT queue (agent vs controller) when saq_jobs is empty,
  - dedup an in-flight deterministic key to a ``skipped`` no-op (idempotent, Phase-32 backstop),
  - skip the four agent stages with a WARNING + zero counts when no agent is online (cold boot),
  - honor ``force=True`` (bypass ONLY the no-op gate, never the per-item dedup).

The queue-loss detector ``count_inflight_jobs`` is stubbed per unit test (the unit DB has no
``saq_jobs`` table); one ``@pytest.mark.integration`` test reads the REAL ``saq_jobs`` via the
``stage_env`` fixture. ``ctx`` mirrors the controller worker shape: ``async_session`` (a
sessionmaker bound to the test engine), ``queue`` (a controller-queue stand-in), ``task_router``
(a ``DedupFakeTaskRouter`` modeling SAQ deterministic-key dedup).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist
from phaze.services.analysis_enqueue import process_file_job_key
from phaze.tasks.reenqueue import recover_orphaned_work
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


_MODELS_PATH = "/models"


class _StubCfg:
    """Minimal stand-in for the control settings recover_orphaned_work reads."""

    def __init__(self, *, models_path: str = _MODELS_PATH, llm_batch_size: int = 10) -> None:
        self.models_path = models_path
        self.llm_batch_size = llm_batch_size


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, llm_batch_size: int = 10) -> None:
    """Pin recover_orphaned_work's get_settings() deterministically (models_path + llm_batch_size)."""
    monkeypatch.setattr("phaze.tasks.reenqueue.get_settings", lambda: _StubCfg(llm_batch_size=llm_batch_size))


def _patch_inflight(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    """Stub the saq_jobs queue-loss detector to report ``value`` in-flight jobs."""

    async def _fake(_session: AsyncSession) -> int:
        return value

    monkeypatch.setattr("phaze.tasks.reenqueue.count_inflight_jobs", _fake)


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter, controller_queue: DedupFakeQueue) -> dict[str, Any]:
    """Build a controller-shaped ctx: async_session + controller queue + per-agent dedup router."""
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "queue": controller_queue, "task_router": router}


def _make_file(*, file_type: str = "mp3", state: str = FileState.DISCOVERED) -> FileRecord:
    """Build a fully-populated FileRecord row for the recovery seed."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
        state=state,
    )


def _make_tracklist() -> Tracklist:
    """Build a bare Tracklist row (no file_id, no version, no discogs chain)."""
    uid = uuid.uuid4()
    return Tracklist(id=uid, external_id=f"tl-{uid.hex}", source_url=f"http://x/{uid.hex}")


@pytest.mark.asyncio
async def test_no_op_on_durable_restart(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """saq_jobs holds live jobs (count > 0) + force=False -> no-op, enqueues NOTHING (D-02)."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 5)  # durable Phase-36 restart: jobs survived
    await seed_active_agent(session, agent_id="nox")
    session.add(_make_file(state=FileState.DISCOVERED))
    await session.commit()

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result == {"detected_loss": False, "forced": False, "stages": {}}
    # No reconcile ran: the controller queue and every per-agent queue stayed untouched.
    assert controller_queue.captured == []
    assert router.queue_for_calls == []


@pytest.mark.asyncio
async def test_all_stages_reconcile_on_empty_queue(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty saq_jobs -> every stage reconciles onto the CORRECT queue with the CORRECT task name.

    Seed is crafted so each stage's shared pending set has deterministic membership. Note the
    intentional overlaps baked into the helpers: metadata pending = ALL music/video files, and
    untracked (search + scan) = ALL music/video files with no tracklist -- so the three mp3 files
    all land in metadata/search/scan, while analyze/fingerprint/proposals are state-gated to one
    file each. Controller stages route to ctx["queue"]; agent stages to phaze-agent-nox.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)  # genuine queue-loss
    await seed_active_agent(session, agent_id="nox")

    f_discovered = _make_file(state=FileState.DISCOVERED)  # analyze
    f_meta_extracted = _make_file(state=FileState.METADATA_EXTRACTED)  # fingerprint
    f_converged = _make_file(state=FileState.ANALYZED)  # proposals
    session.add_all([f_discovered, f_meta_extracted, f_converged])
    await session.flush()
    session.add_all([FileMetadata(file_id=f_converged.id, artist="A", title="T")])
    from phaze.models.analysis import AnalysisResult

    session.add(AnalysisResult(file_id=f_converged.id, bpm=120.0))
    tl = _make_tracklist()  # scrape + match (no version, no discogs)
    session.add(tl)
    await session.commit()

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["detected_loss"] is True
    assert result["forced"] is False

    # Per-stage tallies (deterministic given the seed + helper overlaps).
    stages = result["stages"]
    assert stages["analyze"] == {"reenqueued": 1, "skipped": 0}
    assert stages["metadata"] == {"reenqueued": 3, "skipped": 0}  # all 3 mp3 files
    assert stages["fingerprint"] == {"reenqueued": 1, "skipped": 0}
    assert stages["scan_live_set"] == {"reenqueued": 3, "skipped": 0}  # all 3 untracked mp3
    assert stages["search"] == {"reenqueued": 3, "skipped": 0}  # all 3 untracked mp3
    assert stages["scrape"] == {"reenqueued": 1, "skipped": 0}
    assert stages["match"] == {"reenqueued": 1, "skipped": 0}
    assert stages["proposals"] == {"reenqueued": 1, "skipped": 0}

    # Controller stages landed on ctx["queue"] with the right task names (= deterministic key prefix).
    controller_tasks = {task for task, _ in controller_queue.captured}
    assert controller_tasks == {"generate_proposals", "search_tracklist", "scrape_and_store_tracklist", "match_tracklist_to_discogs"}

    # Agent stages landed on phaze-agent-nox (NEVER the controller queue -- Pitfall 1).
    agent_queue = router.queues["nox"]
    agent_tasks = {task for task, _ in agent_queue.captured}
    assert agent_tasks == {"process_file", "extract_file_metadata", "fingerprint_file", "scan_live_set"}

    # The analyze enqueue carries the deterministic process_file:<id> key (explicit-key producer).
    analyze_keys = {p["key"] for p in agent_queue.captured_policy if p.get("key", "").startswith("process_file:")}
    assert analyze_keys == {process_file_job_key(f_discovered.id)}


@pytest.mark.asyncio
async def test_idempotent_dedup_skips_inflight_keys(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-marking half the analyze keys live -> those count skipped, the rest reenqueue (Phase-32).

    Generalizes test_cron_reenqueues_stragglers: the analyze stage uses the explicit-key
    enqueue_process_file producer, so the DedupFakeQueue models SAQ's deterministic-key dedup.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    agent = await seed_active_agent(session, agent_id="nox")
    files = [_make_file(state=FileState.DISCOVERED) for _ in range(4)]
    session.add_all(files)
    await session.commit()

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    # Pre-enqueue (make "live") the first two files' process_file keys on the agent queue.
    live_queue = router.queue_for(agent.id)
    for f in files[:2]:
        await live_queue.enqueue("process_file", key=process_file_job_key(f.id))

    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # Two analyze keys were in flight -> skipped; the other two -> reenqueued.
    assert result["stages"]["analyze"] == {"reenqueued": 2, "skipped": 2}


@pytest.mark.asyncio
async def test_agent_skip_when_no_active_agent(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No active agent -> the four agent stages skip (WARNING, zero) while controller stages reconcile."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    # NO active agent seeded -> select_active_agent raises NoActiveAgentError.

    f_converged = _make_file(state=FileState.ANALYZED)  # proposals + (untracked) search
    session.add(f_converged)
    await session.flush()
    session.add_all([FileMetadata(file_id=f_converged.id, artist="A", title="T")])
    from phaze.models.analysis import AnalysisResult

    session.add(AnalysisResult(file_id=f_converged.id, bpm=120.0))
    tl = _make_tracklist()  # scrape + match
    session.add(tl)
    await session.commit()

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # Agent stages all zero; the router was never asked for a per-agent queue.
    for stage in ("analyze", "metadata", "fingerprint", "scan_live_set"):
        assert result["stages"][stage] == {"reenqueued": 0, "skipped": 0}
    assert router.queue_for_calls == []
    assert "no active agent" in caplog.text.lower()

    # Controller stages still reconciled.
    assert result["stages"]["proposals"] == {"reenqueued": 1, "skipped": 0}
    assert result["stages"]["search"] == {"reenqueued": 1, "skipped": 0}
    assert result["stages"]["scrape"] == {"reenqueued": 1, "skipped": 0}
    assert result["stages"]["match"] == {"reenqueued": 1, "skipped": 0}


@pytest.mark.asyncio
async def test_force_bypasses_gate_not_dedup(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force=True reconciles even with live saq_jobs (bypasses the no-op gate); still idempotent."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 5)  # live queue -> the gate WOULD short-circuit without force
    await seed_active_agent(session, agent_id="nox")
    session.add(_make_file(state=FileState.DISCOVERED))
    await session.commit()

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue), force=True)

    # detected_loss reflects the (non-empty) detector; forced overrode the gate and reconciled.
    assert result["detected_loss"] is False
    assert result["forced"] is True
    assert result["stages"]["analyze"] == {"reenqueued": 1, "skipped": 0}


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

    redis_url = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6379/0")
    raw_dsn = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    sa_dsn = (os.environ.get("TEST_DATABASE_URL") or raw_dsn).replace("postgresql://", "postgresql+asyncpg://")

    # Probe broker connectivity FIRST so the skip path creates nothing to clean up.
    try:
        probe = await psycopg.AsyncConnection.connect(raw_dsn)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    router = AgentTaskRouter(queue_url=raw_dsn, cache_redis_url=redis_url)
    queue = router.queue_for("recovery-itest")
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
        state=FileState.DISCOVERED,
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
