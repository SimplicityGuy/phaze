"""Tests for the Phase-45 ledger-driven recovery producer (phaze.tasks.reenqueue).

``recover_orphaned_work(ctx, *, force=False)`` recovers exactly

    orphaned = (scheduling_ledger rows) MINUS (live saq_jobs keys) MINUS (domain-completed)

replaying each orphaned row's STORED payload through the SAME keyed producer it was
originally enqueued by (``ctx["queue"].enqueue`` for controller rows; the active agent's
per-agent queue for agent rows). This is the Phase-45 incident fix: a never-scheduled
``DISCOVERED`` file has NO ledger row, so the ~11.4k-file sweep that detonated the queue
cannot recur. It must still:

  - be a NO-OP on a durable Phase-36 restart (saq_jobs has live rows) -- D-02 gate kept,
  - exclude a row whose key is a live saq_jobs key (still in flight),
  - exclude the predicate-covered agent stages when the file is domain-completed
    (analyze: state in {ANALYZED, ANALYSIS_FAILED}; push: state in {PUSHED, ANALYZED,
    ANALYSIS_FAILED}; metadata/fingerprint: NOT in the stage's pending set),
  - leave the FIVE live-keys-only stages (scan_live_set + 4 controller stages) to the
    live-key filter alone,
  - dedup an in-flight deterministic key to a ``skipped`` no-op (idempotency backstop),
  - skip agent-routed rows with a WARNING when no agent is online (cold boot),
  - honor ``force=True`` (bypass ONLY the no-op gate, never the per-item dedup).

The queue-loss detector ``count_inflight_jobs`` and the live-key set ``get_live_job_keys``
are stubbed per unit test (the unit DB has no ``saq_jobs`` table). ``ctx`` mirrors the
controller worker shape: ``async_session`` (a sessionmaker bound to the test engine),
``queue`` (a controller-queue stand-in), ``task_router`` (a ``DedupFakeTaskRouter`` modeling
SAQ deterministic-key dedup).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.services.scheduling_ledger import clear_ledger_entry, get_ledger_rows, upsert_ledger_entry
from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS
from phaze.tasks.reenqueue import (
    _DOMAIN_COMPLETED_STAGES,
    _awaiting_cloud_job_ids,
    _build_done_sets,
    _DoneSets,
    _ledger_fids,
    is_domain_completed,
    recover_orphaned_work,
)
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


def _patch_live_keys(monkeypatch: pytest.MonkeyPatch, keys: set[str]) -> None:
    """Stub get_live_job_keys to return a fixed set of live (queued/active) saq_jobs keys."""

    async def _fake(_session: AsyncSession) -> set[str]:
        return set(keys)

    monkeypatch.setattr("phaze.tasks.reenqueue.get_live_job_keys", _fake)


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter, controller_queue: DedupFakeQueue) -> dict[str, Any]:
    """Build a controller-shaped ctx: async_session + controller queue + per-agent dedup router.

    92-04 (CLEAN-02): ``async_session`` is sourced from ``phaze.database.async_session`` -- monkeypatched by the
    ``session`` fixture's ``_route_stats_fanout`` to a factory BOUND to the per-test ``_db_connection``
    (create_savepoint), exactly as the production controller wires ``ctx["async_session"]``. This lets the task
    SEE seeded rows and makes its commits visible to sibling reads under create_savepoint isolation.
    """
    from phaze.database import async_session

    return {"async_session": async_session, "queue": controller_queue, "task_router": router}


def _make_file(*, file_type: str = "mp3") -> FileRecord:
    """Build a fully-populated FileRecord row for the recovery seed."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
    )


def _agent_payload(function: str, file_id: uuid.UUID) -> dict[str, Any]:
    """Build a minimal stored payload an agent-routed ledger row would carry for ``function``."""
    return {
        "file_id": str(file_id),
        "original_path": f"/music/{file_id}.mp3",
        "file_type": "mp3",
        "agent_id": "nox",
    }


async def _seed_ledger(
    session: AsyncSession,
    *,
    function: str,
    file_id: uuid.UUID,
    payload: dict[str, Any] | None = None,
    timeout: int | None = None,
    retries: int | None = None,
) -> str:
    """Upsert one ledger row for ``<function>:<file_id>`` and return its deterministic key."""
    builder = _KEY_BUILDERS[function]
    pay = payload if payload is not None else _agent_payload(function, file_id)
    key = f"{function}:{builder(pay)}"
    await upsert_ledger_entry(session, key=key, function=function, kwargs=pay, timeout=timeout, retries=retries)
    await session.commit()
    return key


# --- Phase-80 output-table seeds (the derived done/failed source, replacing scalar-state reads) ---


async def _seed_analysis(session: AsyncSession, file_id: uuid.UUID, *, completed: bool = False, failed: bool = False) -> None:
    """Seed the ``analysis`` row Phase-80 derives analyze done/failed from (NAND: never both markers)."""
    session.add(
        AnalysisResult(
            id=uuid.uuid4(),
            file_id=file_id,
            analysis_completed_at=datetime.now(UTC) if completed else None,
            failed_at=datetime.now(UTC) if failed else None,
        )
    )
    await session.commit()


async def _seed_metadata(session: AsyncSession, file_id: uuid.UUID, *, failed_at: datetime | None = None) -> None:
    """Seed the ``metadata`` row Phase-80 derives metadata done (failed_at NULL) / failed (failed_at set) from."""
    session.add(FileMetadata(id=uuid.uuid4(), file_id=file_id, failed_at=failed_at))
    await session.commit()


async def _seed_fingerprint(session: AsyncSession, file_id: uuid.UUID, *, status: str = "success", engine: str = "chromaprint") -> None:
    """Seed one ``fingerprint_results`` engine row (status='success' => fingerprint done, DERIV-05)."""
    session.add(FingerprintResult(id=uuid.uuid4(), file_id=file_id, engine=engine, status=status))
    await session.commit()


async def _seed_awaiting_cloud_job(session: AsyncSession, file_id: uuid.UUID) -> None:
    """Seed a ``cloud_job(status='awaiting')`` sidecar row -- the Phase-83 representation of a parked file."""
    session.add(CloudJob(id=uuid.uuid4(), file_id=file_id, backend_id=None, s3_key=None, status=CloudJobStatus.AWAITING.value))
    await session.commit()


async def _seed_stage_skip(session: AsyncSession, file_id: uuid.UUID, *, stage: str) -> None:
    """Seed a ``stage_skip(file_id, stage)`` force-skip marker (Phase 87, D-08).

    Recovery reads ``domain_completed_clause`` for analyze/metadata, into which Plan 02 threaded
    ``skipped_clause`` as an unconditional disjunct -- so a force-skipped file is domain-complete and
    NEVER re-enqueued (behavior 5). The marker is ADDITIVE: it never clears a failure row.
    """
    session.add(StageSkip(id=uuid.uuid4(), file_id=file_id, stage=stage, reason="operator force-skip"))
    await session.commit()


# --- The incident regression -----------------------------------------------------------


@pytest.mark.asyncio
async def test_never_scheduled_files_are_left_alone(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11 DISCOVERED files + 0 ledger rows -> 0 reenqueued (the Phase-45 incident regression).

    The pre-fix recovery derived work from the complement-of-done pending sets and swept every
    never-scheduled DISCOVERED file, detonating the queue to ~44.5k jobs. Ledger-driven recovery
    reads ONLY rows that were actually scheduled, so a backlog of unscheduled files is untouched.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)  # genuine queue-loss
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    session.add_all([_make_file() for _ in range(11)])
    await session.commit()

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["detected_loss"] is True
    assert all(t == {"reenqueued": 0, "skipped": 0} for t in result["stages"].values())
    assert controller_queue.captured == []
    assert router.queues == {}


# --- The no-op gate (unchanged) --------------------------------------------------------


@pytest.mark.asyncio
async def test_no_op_on_durable_restart(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """saq_jobs holds live jobs (count > 0) + force=False -> no-op, enqueues NOTHING (D-02)."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 5)  # durable Phase-36 restart: jobs survived
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="process_file", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result == {"detected_loss": False, "forced": False, "stages": {}}
    assert controller_queue.captured == []
    assert router.queue_for_calls == []


# --- Replay of a genuinely-orphaned row ------------------------------------------------


@pytest.mark.asyncio
async def test_orphaned_agent_row_replays_through_keyed_producer(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ledger row whose key is NOT live AND NOT domain-completed replays on the agent queue.

    The stored payload is replayed verbatim through the per-agent queue with the deterministic
    key re-stamped from the ledger key (the before_enqueue hook does this in production; the
    fake dedups on the explicit key here), and the row counts as reenqueued.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()  # NOT analyze-done
    session.add(f)
    await session.commit()
    key = await _seed_ledger(session, function="process_file", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["process_file"] == {"reenqueued": 1, "skipped": 0}
    agent_queue = router.queues["nox-analyze"]
    assert [t for t, _ in agent_queue.captured] == ["process_file"]
    # The deterministic key matches the ledger key (re-stamped, so dedup works in production).
    assert agent_queue.captured_policy[0]["key"] == key
    # The stored payload round-tripped (file_id present, never a re-derived FileRecord).
    assert agent_queue.captured[0][1]["file_id"] == str(f.id)


@pytest.mark.asyncio
async def test_replay_preserves_stored_timeout_and_retries(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery replays a row with its STORED SAQ policy so a recovered long ``process_file``
    keeps its 7200s/retries=2 bound -- not the 600s before_enqueue default that would time out
    every long concert set. Regression for the recover-button timeout-loss bug.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="process_file", file_id=f.id, timeout=7200, retries=2)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    policy = router.queues["nox-analyze"].captured_policy[0]
    assert policy["timeout"] == 7200
    assert policy["retries"] == 2


@pytest.mark.asyncio
async def test_replay_omits_policy_when_ledger_has_none(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing ledger row with NULL timeout/retries (written before this change, or a
    producer that set no explicit policy) replays WITHOUT timeout/retries, so the queue's
    before_enqueue default applies exactly as before -- backward compatible."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="process_file", file_id=f.id)  # no timeout/retries

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    policy = router.queues["nox-analyze"].captured_policy[0]
    assert "timeout" not in policy
    assert "retries" not in policy


@pytest.mark.asyncio
async def test_orphaned_controller_row_replays_on_controller_queue(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A controller-routed orphaned ledger row replays on ctx["queue"], never an agent queue."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    tl_id = uuid.uuid4()
    await _seed_ledger(session, function="scrape_and_store_tracklist", file_id=tl_id, payload={"tracklist_id": str(tl_id)})

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["scrape_and_store_tracklist"] == {"reenqueued": 1, "skipped": 0}
    assert [t for t, _ in controller_queue.captured] == ["scrape_and_store_tracklist"]
    assert router.queue_for_calls == []  # never asked for an agent queue


# --- The live-key exclusion ------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_key_row_is_excluded(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ledger row whose key IS a live saq_jobs key is still in flight -> never replayed."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    key = await _seed_ledger(session, function="process_file", file_id=f.id)
    _patch_live_keys(monkeypatch, {key})  # the only row is live

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0}
    assert router.queues == {}


# --- The per-stage domain-completed exclusions -----------------------------------------


@pytest.mark.asyncio
async def test_analyze_done_row_is_excluded(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process_file row whose analyze is done (completed_at) OR terminally-failed (failed_at) is excluded.

    Phase 80 (D-01): analyze domain-completion is ``domain_completed_clause(ANALYZE)`` == done OR
    terminal-failed, derived from the ``analysis`` output row -- NOT a scalar-state read. Both a completed
    and a terminally-failed analyze are domain-complete (FAILURE_IS_TERMINAL[analyze] -> never auto-re-driven).
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f_done = _make_file()
    f_failed = _make_file()
    session.add_all([f_done, f_failed])
    await session.commit()
    await _seed_analysis(session, f_done.id, completed=True)
    await _seed_analysis(session, f_failed.id, failed=True)
    await _seed_ledger(session, function="process_file", file_id=f_done.id)
    await _seed_ledger(session, function="process_file", file_id=f_failed.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0}
    assert router.queues == {}


@pytest.mark.asyncio
async def test_metadata_done_row_is_excluded(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An extract_file_metadata row whose file has a completed metadata row (failed_at NULL) is excluded.

    Phase 80 (D-05): metadata done is now DERIVED DIRECTLY via ``done_clause(METADATA)`` (a ``metadata``
    row present AND ``failed_at IS NULL``), not the retired "absent from the pending set" complement.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_metadata(session, f.id)  # failed_at NULL -> metadata done
    await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["extract_file_metadata"] == {"reenqueued": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_metadata_pending_row_replays(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An extract_file_metadata row whose file IS in the metadata pending set replays (not done)."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()  # music file -> in metadata pending set
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["extract_file_metadata"] == {"reenqueued": 1, "skipped": 0}


@pytest.mark.asyncio
async def test_fingerprint_done_row_is_excluded(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fingerprint_file row whose file has a successful fingerprint engine row is excluded.

    Phase 80 (D-05): fingerprint done is now DERIVED DIRECTLY via ``done_clause(FINGERPRINT)`` -- a
    ``success``/``completed`` engine row (DERIV-05) -- not the retired "absent from the pending set".
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f_done = _make_file()
    session.add(f_done)
    await session.commit()
    await _seed_fingerprint(session, f_done.id, status="success")  # a success engine -> fingerprint done
    await _seed_ledger(session, function="fingerprint_file", file_id=f_done.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["fingerprint_file"] == {"reenqueued": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_fingerprint_pending_row_replays(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fingerprint_file row whose file IS METADATA_EXTRACTED (pending) replays (not done)."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()  # in fingerprint pending set
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="fingerprint_file", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["fingerprint_file"] == {"reenqueued": 1, "skipped": 0}


# --- CR-02 regression: the terminal-failure clear (not the predicate) closes the loop -------


@pytest.mark.asyncio
async def test_cleared_metadata_row_is_not_reenqueued(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-02: a terminally-failed metadata file whose ledger row was CLEARED is NOT re-enqueued.

    The file is a music DISCOVERED file -- it IS in get_metadata_pending_files, so
    is_domain_completed can NEVER fire for it (the broken predicate the phase relied on). Yet
    after the /failed terminal-ack clears extract_file_metadata:<file_id>, the row is simply
    absent from the ledger, so recover_orphaned_work cannot replay it. This proves the CLEAR
    closes the unbounded recovery re-enqueue loop -- independent of the predicate.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()  # music file -> still in metadata pending set
    session.add(f)
    await session.commit()
    key = await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)
    # Simulate the POST /{file_id}/failed terminal ack: the control-side clear removes the row.
    await clear_ledger_entry(session, key)
    await session.commit()

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["extract_file_metadata"] == {"reenqueued": 0, "skipped": 0}
    assert controller_queue.captured == []
    assert router.queues == {}


@pytest.mark.asyncio
async def test_cleared_fingerprint_row_is_not_reenqueued(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-02: a terminally-failed fingerprint file whose ledger row was CLEARED is NOT re-enqueued.

    The file is METADATA_EXTRACTED -- it IS in get_fingerprint_pending_files, so the predicate
    can never mark it done. After the /failed terminal-ack clears fingerprint_file:<file_id>,
    the absent row keeps recover_orphaned_work from replaying it. The CLEAR is what stops the loop.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()  # still in fingerprint pending set
    session.add(f)
    await session.commit()
    key = await _seed_ledger(session, function="fingerprint_file", file_id=f.id)
    await clear_ledger_entry(session, key)
    await session.commit()

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["fingerprint_file"] == {"reenqueued": 0, "skipped": 0}
    assert controller_queue.captured == []
    assert router.queues == {}


# --- Phase 87 (D-08): a FORCE-SKIPPED file is domain-complete -> NOT re-enqueued (behavior 5) ------
#
# A file the operator force-skipped (a ``stage_skip`` marker) must never be re-driven by recovery even
# when its ledger row survived a crash/restart and its saq_jobs key is not live -- otherwise the skip is
# meaningless and the file re-enters the queue (the over-enqueue class D-08 guards). analyze/metadata
# recovery read ``domain_completed_clause``, into which Plan 02 threaded ``skipped_clause`` as an
# unconditional disjunct, so the exclusion propagates with ZERO edits to reenqueue.py.


@pytest.mark.asyncio
async def test_skipped_analyze_row_is_excluded_from_recovery(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process_file row whose analyze is force-SKIPPED is domain-complete -> NOT re-enqueued (behavior 5).

    The file is a music DISCOVERED file with NO analysis row -- absent the skip marker it WOULD be an
    orphaned recovery candidate (proved by ``test_orphaned_agent_row_replays_through_keyed_producer``).
    The ``stage_skip(analyze)`` marker makes ``domain_completed_clause(ANALYZE)`` True (its ``skipped_clause``
    disjunct), so ``is_domain_completed`` excludes it and recovery replays NOTHING. The marker is additive
    (no ``analysis`` row cleared/created), so this attributes the exclusion to the skip alone.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()  # would-be orphan absent the marker
    session.add(f)
    await session.commit()
    await _seed_stage_skip(session, f.id, stage="analyze")
    await _seed_ledger(session, function="process_file", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0}
    assert router.queues == {}


@pytest.mark.asyncio
async def test_skipped_analyze_row_is_excluded_on_manual_force(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manual ``force=True`` "Recover" path ALSO excludes a force-skipped analyze file (behavior 5).

    ``is_domain_completed`` gates the orphan set identically on both paths (``force`` only bypasses the
    no-op DETECT gate, never the domain-completed exclusion), so the skip holds under a manual reconcile
    over a live queue too.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 3)  # queue NOT empty: force=True is the only way past the detect gate
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_stage_skip(session, f.id, stage="analyze")
    await _seed_ledger(session, function="process_file", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue), force=True)

    assert result["forced"] is True
    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0}
    assert router.queues == {}


@pytest.mark.asyncio
async def test_skipped_metadata_row_is_excluded_from_recovery(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An extract_file_metadata row whose metadata is force-SKIPPED is domain-complete -> NOT re-enqueued.

    Absent the marker this music DISCOVERED file IS in the metadata pending set and WOULD replay
    (``test_metadata_pending_row_replays``). ``domain_completed_clause(METADATA)`` gains the file via its
    ``skipped_clause`` disjunct, and since no metadata ``failed_at`` row exists the D-10 gate is not even
    reached -- ``metadata_domain_completed`` membership alone excludes it.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_stage_skip(session, f.id, stage="metadata")
    await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["extract_file_metadata"] == {"reenqueued": 0, "skipped": 0}
    assert router.queues == {}


@pytest.mark.asyncio
async def test_force_skipped_metadata_with_stale_failed_at_is_excluded_from_recovery(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-3m5n regression: a force-SKIPPED metadata stage is NEVER re-driven, even with a stale ``failed_at``.

    Reproduces the exact failure scenario the bug report describes: metadata extraction fails at T1
    (``failed_at`` set); the operator retries (``retry_metadata_failed`` deliberately LEAVES ``failed_at``
    set, 81 D-11) and a ledger row is written; the job is lost before running (queue truncate / restore);
    the operator gives up and force-skips metadata (the additive-only ``force_skip_stage`` writer, T-87-20,
    never clears ``failed_at`` nor the ledger row). On the next Recover, the file is domain-complete via the
    ``skipped_clause`` disjunct of ``domain_completed_clause(METADATA)``, but it ALSO carries a non-NULL
    ``metadata_failed_at`` entry -- pre-fix, ``is_domain_completed`` fell through to the D-10
    ``enqueued_at <= failed_at`` gate and re-drove the force-skipped extraction (the ledger row's
    ``enqueued_at`` postdates ``failed_at``, exactly D-10 Cell A). The fix checks ``metadata_skipped``
    membership BEFORE the D-10 gate, so this now stays terminal and NOTHING is re-enqueued.

    MUTATION: reverting the ``metadata_skipped`` short-circuit (falling straight through to the D-10 gate
    on every row in ``metadata_domain_completed``) makes this RED -- the force-skipped file re-drives.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)  # genuine queue-loss: the lost operator-retry job never ran
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_metadata(session, f.id, failed_at=datetime.now(UTC))  # T1: metadata FAILED
    # T2 > T1: the operator's lost retry ledger row survives the failure marker (D-10 Cell A shape).
    await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)
    # The operator gives up and force-skips metadata AFTER the failure -- additive-only, failed_at stays set.
    await _seed_stage_skip(session, f.id, stage="metadata")

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["extract_file_metadata"] == {"reenqueued": 0, "skipped": 0}
    assert router.queues == {}


@pytest.mark.asyncio
async def test_skipped_fingerprint_row_is_excluded_from_recovery(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A force-SKIPPED fingerprint file is excluded from recovery (phase-87 behavior 5).

    ``_build_done_sets`` now derives ``fingerprint_done`` from
    ``or_(done_clause(FINGERPRINT), skipped_clause(FINGERPRINT))``: a FAILED-but-not-skipped fingerprint
    still auto-retries, but an operator-force-SKIPPED fingerprint whose ``fingerprint_file`` ledger row
    survives a crash/restart is NOT re-enqueued -- recovery never re-drives a stage the operator skipped.
    (Regression guard for the 87-03 gap; previously a strict-xfail tripwire, made GREEN by the 87-03 fix.)
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()  # in fingerprint pending set absent the marker
    session.add(f)
    await session.commit()
    await _seed_stage_skip(session, f.id, stage="fingerprint")
    await _seed_ledger(session, function="fingerprint_file", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["fingerprint_file"] == {"reenqueued": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_scan_row_is_live_keys_only(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan_live_set row has NO domain predicate: an ANALYZED file (a "done"-looking state) still replays.

    scan_live_set is live-keys-only -- its ledger row is cleared by Plan 02's terminal ack on every
    outcome, so any row that reaches recovery IS orphaned. The domain-completed predicate must NOT
    apply to it (no scalar-state/pending-set exclusion), so even an ANALYZED file replays.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()  # would be "done" for analyze, but irrelevant to scan
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="scan_live_set", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["scan_live_set"] == {"reenqueued": 1, "skipped": 0}


# --- Phase 49 D-04: AWAITING_CLOUD stays pending in recovery ----------------------------


@pytest.mark.asyncio
async def test_awaiting_cloud_file_stays_pending_in_recovery(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-04: a parked (cloud_job='awaiting') file with no analysis row is NOT analyze-domain-completed.

    A held (duration-routed) file must keep being driven to completion by the ``stage_cloud_window`` drain
    (83-06: the drain is its single owner) until it is genuinely analyzed, so it must NEVER be classified
    as done. Phase 80 derives analyze-done from the ``analysis`` output row via
    ``domain_completed_clause(ANALYZE)``; a parked file has none, so it is ABSENT from the analyze done-set.
    This test guards the omission so a future derivation edit cannot silently mark a held file complete.
    """
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_awaiting_cloud_job(session, f.id)
    key = await _seed_ledger(session, function="process_file", file_id=f.id)

    rows = [SchedulingLedger(key=key, function="process_file", routing="agent", payload={"file_id": str(f.id)})]
    done_sets = await _build_done_sets(session, _ledger_fids(rows))

    # The parked file has no analysis row -> NOT in the analyze done-set.
    assert str(f.id) not in done_sets.analyze_done

    # It is NOT domain-completed, so it would not be dropped by the done-set filter -- but 83-06's
    # awaiting-cloud exclusion in recover_orphaned_work keeps the drain its single owner (not the ledger).
    assert is_domain_completed(rows[0], done_sets) is False


# --- 83-06 (CONSCIOUSLY REVERSES D-09): the drain is the SINGLE owner of held AWAITING_CLOUD files ----
#
# 83-06 reversed D-09: the backfill no longer SEEDS a ``process_file:<id>`` ledger row for a held compute
# file (it CLEARS ``analysis.failed_at`` and DELETES the orphaned row, keeping only the awaiting
# ``cloud_job`` row). So no held file carries a process_file ledger row any more, and the former
# ``_get_awaiting_cloud_ids`` + ``held_agent_rows`` compute-only recovery partition became unreachable and
# was REMOVED. ``recover_orphaned_work`` now EXCLUDES any file with an awaiting ``cloud_job`` row from the
# orphan set (:func:`_awaiting_cloud_job_ids`) -- the ``stage_cloud_window`` drain is the single owner --
# which preserves the CLOUDROUTE-02 invariant ROBUSTLY (a held long file is never routed kind-agnostically
# to a fileserver and analyzed locally), even for a LEGACY pre-83-06 row that still carries a process_file
# ledger row.


@pytest.mark.asyncio
async def test_awaiting_cloud_exclusion_set_surfaces_held_files(
    session: AsyncSession,
) -> None:
    """83-06: every file with an awaiting ``cloud_job`` row is in the drain-owned exclusion set.

    ``_awaiting_cloud_job_ids`` is the recovery exclusion the single-owner drain relies on: a genuinely
    parked file (no process_file ledger row) AND a legacy held file (still carrying a process_file ledger
    row) are BOTH surfaced, so recovery never re-drives either onto an agent.
    MUTATION: narrowing the query to also require ``~inflight_clause(ANALYZE)`` would drop the legacy
    file below -> re-opening the CLOUDROUTE-02 local-analysis hole -> this test goes RED.
    """
    parked = _make_file()
    legacy = _make_file()
    session.add_all([parked, legacy])
    await session.commit()
    await _seed_awaiting_cloud_job(session, parked.id)  # parked, no process_file ledger row
    await _seed_awaiting_cloud_job(session, legacy.id)
    await _seed_ledger(session, function="process_file", file_id=legacy.id)  # a legacy pre-83-06 held row

    awaiting = await _awaiting_cloud_job_ids(session)
    assert {str(parked.id), str(legacy.id)} <= awaiting


@pytest.mark.asyncio
async def test_held_awaiting_cloud_file_is_not_recovered_with_only_a_fileserver(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """83-06 (CLOUDROUTE-02): a held AWAITING_CLOUD file is NEVER re-driven onto a fileserver for local analysis.

    A held long file carries an ``awaiting`` cloud_job (+ possibly a LEGACY process_file ledger row). The
    drain owns it, so recovery EXCLUDES it -- it must never land on the fileserver's analyze lane.
    MUTATION: dropping the ``awaiting_cloud`` exclusion in ``recover_orphaned_work`` lets the legacy row
    fall into ``other_agent_rows`` -> ``nox-analyze`` -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")  # only a fileserver online
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_awaiting_cloud_job(session, f.id)
    await _seed_ledger(session, function="process_file", file_id=f.id)  # a legacy pre-83-06 held row

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # Held awaiting-cloud file -> owned by the drain, excluded from recovery, NEVER analyzed locally.
    assert "nox-analyze" not in router.queues
    assert result["stages"]["process_file"]["reenqueued"] == 0


@pytest.mark.asyncio
async def test_held_awaiting_cloud_file_is_not_recovered_even_with_a_compute_agent(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """83-06 (reverses D-09): a held AWAITING_CLOUD file is NOT recovered even when a COMPUTE agent is online.

    Under the old D-09 model recovery re-drove the held file onto the compute agent; 83-06 makes the
    ``stage_cloud_window`` drain the SINGLE owner, so recovery excludes the held file regardless of which
    agent kind is online (the drain, not the ledger, dispatches it). Companion lock to the fileserver case.
    MUTATION: dropping the ``awaiting_cloud`` exclusion re-drives the legacy row onto ``cloud-analyze`` ->
    a second owner -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="cloud", kind="compute")  # a compute agent online
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_awaiting_cloud_job(session, f.id)
    await _seed_ledger(session, function="process_file", file_id=f.id)  # a legacy pre-83-06 held row

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # The drain owns the held file: recovery re-drives NOTHING (no second owner, no compute re-enqueue).
    assert "cloud-analyze" not in router.queues
    assert result["stages"]["process_file"]["reenqueued"] == 0


@pytest.mark.asyncio
async def test_non_held_process_file_row_still_routes_to_any_agent(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-01 guard: a NON-held process_file row still recovers to any online agent (no over-restrict).

    A normal lost analyze of a short (not-AWAITING_CLOUD) file must keep recovering through the
    kind-agnostic path -- the compute-only restriction applies ONLY to held files.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")  # only a fileserver online
    normal = _make_file()  # not AWAITING_CLOUD, not analyze-done
    session.add(normal)
    await session.commit()
    await _seed_ledger(session, function="process_file", file_id=normal.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # The short file recovers normally onto the only online agent (the fileserver).
    assert "nox-analyze" in router.queues
    assert [str(normal.id)] == [payload["file_id"] for _name, payload in router.queues["nox-analyze"].captured]
    assert result["stages"]["process_file"]["reenqueued"] == 1


# --- Predicate totality ----------------------------------------------------------------


@pytest.mark.parametrize("function", sorted(_KEY_BUILDERS))
def test_every_keyed_function_is_predicate_covered_xor_live_keys_only(function: str) -> None:
    """Each keyed function is EITHER domain-predicate-covered XOR live-keys-only.

    No function may be both (double-classified) or neither (silently undefined). The
    predicate-covered functions are process_file/extract_file_metadata/fingerprint_file plus
    the Phase-50 push_file stage.
    """
    covered = function in _DOMAIN_COMPLETED_STAGES
    live_keys_only = function not in _DOMAIN_COMPLETED_STAGES
    assert covered != live_keys_only  # exclusive-or: exactly one is true


def test_domain_completed_stages_are_exactly_the_four_agent_stages() -> None:
    """The predicate-covered set is exactly process_file/extract_file_metadata/fingerprint_file/push_file."""
    assert {"process_file", "extract_file_metadata", "fingerprint_file", "push_file"} == _DOMAIN_COMPLETED_STAGES
    # And every covered stage is a real keyed function (no typos / drift from _KEY_BUILDERS).
    assert set(_KEY_BUILDERS) >= _DOMAIN_COMPLETED_STAGES


def test_is_domain_completed_replays_a_predicate_row_with_no_file_id() -> None:
    """A predicate-covered row whose stored payload lacks ``file_id`` is NOT domain-completed.

    Defensive: a malformed/legacy ledger payload with no natural id must replay (return False)
    rather than be silently dropped as "done" -- the live-key filter + deterministic-key dedup
    still backstop a still-live item, so replaying is the safe default.
    """
    row = SchedulingLedger(key="process_file:ghost", function="process_file", routing="agent", payload={})
    empty = _DoneSets(
        analyze_done=set(),
        metadata_domain_completed=set(),
        metadata_failed_at={},
        metadata_skipped=set(),
        fingerprint_done=set(),
        push_done=set(),
    )
    assert is_domain_completed(row, empty) is False


# --- Idempotency backstop --------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_skip_backstop_for_a_slipped_live_item(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A still-live item that slips past the (stubbed-empty) live filter dedups to None -> skipped.

    Models the Phase-32 backstop: get_live_job_keys returns empty (a stale read), but the agent
    queue already holds the deterministic key, so the replay returns None and counts as skipped --
    recovery can never double the queue.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())  # stale: reports nothing live
    agent = await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    key = await _seed_ledger(session, function="process_file", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    # Pre-enqueue the deterministic key on the agent queue (it is actually still live).
    live_queue = router.queue_for(agent.id, "analyze")
    await live_queue.enqueue("process_file", key=key)
    router.queue_for_calls.clear()  # reset so the recovery call's bookkeeping is clean

    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 1}


# --- force bypasses ONLY the gate ------------------------------------------------------


@pytest.mark.asyncio
async def test_force_bypasses_gate_not_dedup(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force=True reconciles even with live saq_jobs (bypasses the no-op gate); still idempotent."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 5)  # live queue -> the gate WOULD short-circuit without force
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="process_file", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue), force=True)

    assert result["detected_loss"] is False
    assert result["forced"] is True
    assert result["stages"]["process_file"] == {"reenqueued": 1, "skipped": 0}


# --- No active agent: agent rows skip, controller rows replay --------------------------


@pytest.mark.asyncio
async def test_agent_rows_skip_when_no_active_agent_controller_rows_replay(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No active agent -> agent-routed rows skip (WARNING) while controller-routed rows still replay."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    # NO active agent seeded -> select_active_agent raises NoActiveAgentError.
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="process_file", file_id=f.id)  # agent-routed
    tl_id = uuid.uuid4()
    await _seed_ledger(session, function="search_tracklist", file_id=tl_id, payload={"file_id": str(tl_id)})  # controller-routed

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # Agent-routed row skipped (zero), controller-routed row replayed.
    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0}
    assert result["stages"]["search_tracklist"] == {"reenqueued": 1, "skipped": 0}
    assert router.queue_for_calls == []
    assert "no active agent" in caplog.text.lower()


# --- Integration: live saq_jobs --------------------------------------------------------


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


# --- Phase 45 Plan 04: startup wiring -- backfill runs BEFORE recovery ------------------


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
    monkeypatch.setattr("phaze.tasks.controller.create_async_engine", lambda *_a, **_kw: MagicMock())

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

    monkeypatch.setattr("phaze.tasks.controller.create_async_engine", lambda *_a, **_kw: MagicMock())

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


# --- Phase 69 SCHED-05: single recovery owner per backend kind (in-flight cloud_job exclusion) -----
#
# After Phase-68 BACK-03 a cloud-burst file carries BOTH an in-flight cloud_job row (any backend_id)
# AND a process_file / push_file scheduling-ledger row. Both the backend reconcile/`/pushed` callback
# and this ledger recovery could otherwise claim ownership of that file's re-drive -- the exact
# double-owner vector that produced the 44.5k over-enqueue incident. recover_orphaned_work MUST skip
# any ledger row whose file has an in-flight cloud_job, leaving the backend callback/reconcile as the
# single owner. A file with NO in-flight cloud_job (a genuinely-orphaned held AWAITING_CLOUD file)
# keeps its existing held recovery path -- the fix must not over-exclude.


async def _seed_cloud_job(
    session: AsyncSession,
    file_id: uuid.UUID,
    *,
    status: CloudJobStatus = CloudJobStatus.SUBMITTED,
    backend_id: str = "oci-a1",
) -> None:
    """Seed the compute cloud_job sidecar row ComputeAgentBackend.dispatch writes (backend_id set, s3_key NULL)."""
    session.add(CloudJob(id=uuid.uuid4(), file_id=file_id, backend_id=backend_id, s3_key=None, status=status.value))
    await session.commit()


@pytest.mark.asyncio
async def test_single_owner_in_flight_cloud_job_skips_ledger_recovery(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHED-05: a compute file with an in-flight cloud_job + a process_file ledger row is NOT re-enqueued.

    The backend reconcile / `/pushed` callback is the single owner for any file with a live cloud_job
    row; recovery must exclude it so the file gains no second recovery path (the 44.5k over-enqueue
    incident class). Even with a compute agent online (so the held path COULD otherwise route it),
    the in-flight cloud_job exclusion wins.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)  # genuine queue-loss
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="cloud", kind="compute")
    burst = _make_file()
    session.add(burst)
    await session.commit()
    await _seed_cloud_job(session, burst.id, status=CloudJobStatus.SUBMITTED)  # in-flight -> owned by its callback
    await _seed_ledger(session, function="process_file", file_id=burst.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # The in-flight cloud_job file must NOT be recovered by the ledger -- its callback/reconcile owns it.
    assert "cloud" not in router.queues
    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_single_owner_terminal_cloud_job_does_not_block_recovery(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHED-05 guard: a file whose cloud_job is TERMINAL (FAILED) is NOT in the in-flight set.

    Only {UPLOADING, UPLOADED, SUBMITTED, RUNNING} are in-flight; a spilled/terminal FAILED row means
    no backend owns the re-drive anymore, so a still-orphaned process_file row must recover (here to the
    only online agent, a compute agent). The exclusion is by in-flight status, not by row presence.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="cloud", kind="compute")
    held = _make_file()
    session.add(held)
    await session.commit()
    await _seed_cloud_job(session, held.id, status=CloudJobStatus.FAILED)  # terminal -> NOT in-flight
    await _seed_ledger(session, function="process_file", file_id=held.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # Terminal cloud_job -> no backend owner -> the process_file row recovers onto the only online agent.
    assert "cloud-analyze" in router.queues
    assert result["stages"]["process_file"]["reenqueued"] == 1


@pytest.mark.asyncio
async def test_single_owner_no_cloud_job_keeps_held_recovery_path(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHED-05 guard: an orphaned process_file row with NO cloud_job still recovers -- no regression.

    A genuinely-orphaned file (no cloud_job row was ever written) is not owned by any backend callback,
    so recovery must still re-drive its process_file row (here to the only online agent) -- no regression.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="cloud", kind="compute")
    held = _make_file()
    session.add(held)
    await session.commit()
    # No cloud_job row seeded -- genuinely orphaned.
    await _seed_ledger(session, function="process_file", file_id=held.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert "cloud-analyze" in router.queues
    assert result["stages"]["process_file"]["reenqueued"] == 1


# --- Phase 80 (READ-03): SC-2 / SC-3 / D-10 both-cells / D-11 regression cases ----------------------
#
# Each is mutation-named: it names the source mutation that turns it RED, so a future edit that
# re-opens the 44.5K over-enqueue class (SC-2), auto-re-drives a terminal analyze (SC-3), mis-resolves
# the metadata in_flight-and-failed cell (D-10), or falls into the ~inflight_clause trap (D-11) is caught.


@pytest.mark.asyncio
async def test_sc2_never_scheduled_discovered_file_with_no_ledger_row_is_not_recovered(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-2: a never-scheduled ``discovered`` file with NO ledger row is NOT recovered (the 44.5K guard).

    Recovery drives EXCLUSIVELY off ``get_ledger_rows`` -- a file that was never scheduled has no ledger
    row, so it is invisible to recovery even after a genuine queue-loss. This is the headline guard
    against the 2026-06-18 over-enqueue incident class (recovery sweeping never-scheduled discovered files).
    MUTATION: iterating the file corpus (e.g. ``get_files_by_state(DISCOVERED)``) instead of the ledger
    re-enqueues this file -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)  # genuine queue-loss
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    orphan = _make_file()  # never scheduled -> no ledger row
    session.add(orphan)
    await session.commit()

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert all(t == {"reenqueued": 0, "skipped": 0} for t in result["stages"].values())
    assert controller_queue.captured == []
    assert router.queues == {}


@pytest.mark.asyncio
async def test_sc3_failed_analyze_with_surviving_ledger_row_is_terminal_never_reenqueued(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-3: a FAILED analyze with a surviving process_file ledger row is domain-complete -> never auto-re-driven.

    ``FAILURE_IS_TERMINAL[analyze]`` is True, so ``domain_completed_clause(ANALYZE)`` counts a terminally
    failed analyze as complete -- an un-analyzable file is NEVER auto-looped by recovery (manual retry
    only, which clears ``failed_at`` first). This encodes ELIG-03's twin at the recovery layer.
    MUTATION: dropping the ``failed_clause`` disjunct from ``domain_completed_clause(ANALYZE)`` (or
    bypassing it in ``is_domain_completed``) re-drives the failed analyze -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_analysis(session, f.id, failed=True)  # terminal analyze failure (failed_at set)
    await _seed_ledger(session, function="process_file", file_id=f.id)  # ledger row SURVIVES the failure

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0}
    assert router.queues == {}


async def _metadata_done_sets_for(
    session: AsyncSession, *, file_id: uuid.UUID, key: str, enqueued_at: datetime
) -> tuple[SchedulingLedger, _DoneSets]:
    """Build the ledger row (with an explicit ``enqueued_at``) + the ledger-scoped done-sets for a metadata probe."""
    row = SchedulingLedger(key=key, function="extract_file_metadata", routing="agent", payload={"file_id": str(file_id)}, enqueued_at=enqueued_at)
    done_sets = await _build_done_sets(session, _ledger_fids([row]))
    return row, done_sets


@pytest.mark.asyncio
async def test_d10_cell_a_orphaned_operator_retry_redrives_metadata(session: AsyncSession) -> None:
    """D-10 Cell A: metadata failed AND ``enqueued_at > failed_at`` (an orphaned OPERATOR retry) -> re-drives.

    ``retry_metadata_failed`` LEAVES ``metadata.failed_at`` set then re-enqueues, so a ledger row whose
    ``enqueued_at`` is AFTER ``failed_at`` is a fresh operator retry that MUST re-drive (not stay terminal).
    MUTATION: flipping the gate comparison to ``>=`` / ``<`` (or dropping it) makes this domain-complete -> RED.
    """
    failed_at = datetime.now(UTC)
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_metadata(session, f.id, failed_at=failed_at)  # metadata FAILED
    key = await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    row, done_sets = await _metadata_done_sets_for(session, file_id=f.id, key=key, enqueued_at=failed_at + timedelta(minutes=5))

    # The failed metadata IS in the domain-completed set (done OR failed), but the D-10 gate re-drives it
    # because enqueued_at (the retry) is AFTER failed_at -> is_domain_completed False -> recovery replays.
    assert str(f.id) in done_sets.metadata_domain_completed
    assert str(f.id) in done_sets.metadata_failed_at
    assert is_domain_completed(row, done_sets) is False


@pytest.mark.asyncio
async def test_d10_gate_never_applies_to_a_force_skipped_metadata_row(session: AsyncSession) -> None:
    """phaze-3m5n: force-skip membership short-circuits the D-10 gate, even in the Cell A shape.

    Unit-level twin of ``test_force_skipped_metadata_with_stale_failed_at_is_excluded_from_recovery``:
    isolates ``is_domain_completed`` directly against a done-sets snapshot carrying BOTH a stale
    ``metadata_failed_at`` entry (``enqueued_at > failed_at``, which alone would fail the D-10 gate --
    see ``test_d10_cell_a_orphaned_operator_retry_redrives_metadata``) AND ``metadata_skipped``
    membership for the same file. The skip must win: ``is_domain_completed`` returns True without ever
    reaching the ``enqueued_at <= failed_at`` comparison.
    MUTATION: dropping the ``metadata_skipped`` short-circuit (falling through to the D-10 gate
    unconditionally) makes this RED, identically to Cell A.
    """
    failed_at = datetime.now(UTC)
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_metadata(session, f.id, failed_at=failed_at)  # metadata FAILED
    await _seed_stage_skip(session, f.id, stage="metadata")  # force-skipped AFTER the failure
    key = await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    # enqueued_at (the orphaned retry) is AFTER failed_at -- the D-10 gate alone would return False here
    # (proved by Cell A), so this row only stays domain-complete if the skip is checked first.
    row, done_sets = await _metadata_done_sets_for(session, file_id=f.id, key=key, enqueued_at=failed_at + timedelta(minutes=5))

    assert str(f.id) in done_sets.metadata_skipped
    assert str(f.id) in done_sets.metadata_failed_at
    assert is_domain_completed(row, done_sets) is True


@pytest.mark.asyncio
async def test_d10_cell_b_callback_partial_failure_stays_terminal(session: AsyncSession) -> None:
    """D-10 Cell B: metadata failed AND ``enqueued_at < failed_at`` (a callback that wrote the marker but crashed) -> terminal.

    The failure ack wrote ``failed_at`` but crashed before clearing the ledger, so the surviving row's
    ``enqueued_at`` PRE-DATES ``failed_at``: the stage IS domain-complete and must stay terminal (never re-drive).
    MUTATION: dropping the ``enqueued_at <= failed_at`` gate (bare ``done OR failed``) leaves this True but
    turns Cell A RED -- the pair proves the gate is non-vacuous.
    """
    failed_at = datetime.now(UTC)
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_metadata(session, f.id, failed_at=failed_at)  # metadata FAILED
    key = await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    row, done_sets = await _metadata_done_sets_for(session, file_id=f.id, key=key, enqueued_at=failed_at - timedelta(minutes=5))

    # enqueued_at (the lost callback's row) PRE-DATES failed_at -> domain-complete -> stays terminal.
    assert is_domain_completed(row, done_sets) is True


@pytest.mark.asyncio
async def test_d10_gate_does_not_crash_on_db_read_ledger_row(session: AsyncSession) -> None:
    """CR-02: the D-10 gate must NOT raise on a DB-read (naive) ``enqueued_at`` vs aware ``failed_at``.

    ``scheduling_ledger.enqueued_at`` is ``TIMESTAMP WITHOUT TIME ZONE`` (migration 022) -> asyncpg returns
    it NAIVE, while ``metadata.failed_at`` is ``timezone=True`` (aware). The Cell A/B tests above build the
    ledger row IN MEMORY with an aware ``enqueued_at`` and never round-trip through ``get_ledger_rows``, so
    they miss the mismatch. Reading the row back from the DB (the production ``recover_orphaned_work`` path)
    makes ``enqueued_at`` naive; a bare ``naive <= aware`` raises ``TypeError`` and aborts the whole recovery
    run. This asserts the coercion holds against the real DB representation.
    MUTATION: dropping the ``tzinfo``-coercion at the gate (bare ``row.enqueued_at <= failed_at``) -> RED
    (``TypeError: can't compare offset-naive and offset-aware datetimes``).
    """
    failed_at = datetime.now(UTC)
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_metadata(session, f.id, failed_at=failed_at)  # metadata FAILED (aware failed_at)
    key = await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)  # committed -> naive enqueued_at

    # Read the row back the way production does -> enqueued_at is NAIVE (from the WITHOUT TIME ZONE column).
    row = next(r for r in await get_ledger_rows(session) if r.key == key)
    assert row.enqueued_at.tzinfo is None  # guards the premise: the DB really returns a naive stamp
    done_sets = await _build_done_sets(session, _ledger_fids([row]))

    # The committed row's server-default enqueued_at is AFTER failed_at (an orphaned retry) -> re-drives,
    # but the point is that the comparison COMPLETES without a TypeError.
    assert is_domain_completed(row, done_sets) is False


def test_d10_analyze_clears_failed_at_but_metadata_does_not() -> None:
    """The analyze/metadata retry ASYMMETRY that is the root of the D-10 cell (guards a future symmetric change).

    ``retry_analysis_failed`` CLEARS ``analysis.failed_at`` before re-enqueuing (so analyze has no ambiguous
    ``in_flight AND failed`` cell), while ``retry_metadata_failed`` deliberately LEAVES ``metadata.failed_at``
    set (81 D-11) -- which is exactly why only metadata carries the D-10 ``enqueued_at`` gate. Asserting the
    asymmetry at the source pins it: a future change that made metadata symmetric (clearing failed_at on
    retry) would need to revisit the D-10 gate, and this test forces that conversation.
    """
    import inspect

    from phaze.routers import pipeline as pipeline_router

    analyze_src = inspect.getsource(pipeline_router.retry_analysis_failed).replace(" ", "")
    metadata_src = inspect.getsource(pipeline_router.retry_metadata_failed).replace(" ", "")
    # analyze retry CLEARS the failure marker (values(failed_at=None, ...)); metadata retry does NOT.
    assert "failed_at=None" in analyze_src
    assert "failed_at=None" not in metadata_src


@pytest.mark.asyncio
async def test_d11_inflight_clause_is_not_in_domain_completed_clause(session: AsyncSession) -> None:
    """D-11: ``~inflight_clause`` must NEVER be a conjunct of ``domain_completed_clause`` -- the both-cells lock.

    Every recovery candidate is a scheduling-ledger row BY CONSTRUCTION, so a metadata file that has both
    a ``failed_at`` marker AND a committed ``extract_file_metadata`` ledger row (inflight) MUST still
    resolve as domain-complete via the Cell B path. Adding ``~inflight_clause(METADATA)`` to
    ``domain_completed_clause`` would make it False for EVERY candidate -- silently disabling the secondary
    over-enqueue net (the 44.5K incident class) while staying a green no-op for the drain/card.
    MUTATION: adding ``~inflight_clause(stage)`` to ``domain_completed_clause`` makes this row re-drive -> RED.
    """
    failed_at = datetime.now(UTC)
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_metadata(session, f.id, failed_at=failed_at)  # metadata FAILED
    # The ledger row (inflight by construction) has enqueued_at BEFORE failed_at -> Cell B terminal.
    key = await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    row, done_sets = await _metadata_done_sets_for(session, file_id=f.id, key=key, enqueued_at=failed_at - timedelta(minutes=5))

    # Despite the inflight ledger row, the terminal cell still resolves domain-complete (D-11 trap avoided).
    assert is_domain_completed(row, done_sets) is True
