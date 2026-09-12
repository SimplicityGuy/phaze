"""Orphan classification, replay safety, reaping, and crash idempotency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
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
    _regenerate_row_isolated,
    _RegenTarget,
    is_domain_completed,
    recover_orphaned_work,
)
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


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
    assert all(t == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0} for t in result["stages"].values())
    assert controller_queue.captured == []
    assert router.queues == {}


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

    # phaze-71nz: the no-op shape carries the run-wide ``unreplayable`` total too, so every caller
    # (the startup log, the operator status fragment) can read it unconditionally.
    assert result == {"detected_loss": False, "forced": False, "unreplayable": 0, "stages": {}}
    assert controller_queue.captured == []
    assert router.queue_for_calls == []


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

    assert result["stages"]["process_file"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}
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

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert router.queues == {}


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

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
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

    assert result["stages"]["extract_file_metadata"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}


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

    assert result["stages"]["extract_file_metadata"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}


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

    assert result["stages"]["extract_file_metadata"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert controller_queue.captured == []
    assert router.queues == {}


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

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
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
    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
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

    assert result["stages"]["extract_file_metadata"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
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
    set, 81 D-11) and a ledger row is written; the job is lost before running (queue reset / restore);
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

    assert result["stages"]["extract_file_metadata"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert router.queues == {}


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


@pytest.mark.parametrize("function", sorted(_KEY_BUILDERS))
def test_every_keyed_function_is_predicate_covered_xor_live_keys_only(function: str) -> None:
    """Each keyed function is EITHER domain-predicate-covered XOR live-keys-only.

    No function may be both (double-classified) or neither (silently undefined). The
    predicate-covered functions are process_file/extract_file_metadata plus the Phase-50 push_file and
    (phaze-k95r7) s3_upload cloud-lane stages.
    """
    covered = function in _DOMAIN_COMPLETED_STAGES
    live_keys_only = function not in _DOMAIN_COMPLETED_STAGES
    assert covered != live_keys_only  # exclusive-or: exactly one is true


def test_domain_completed_stages_are_exactly_the_file_keyed_agent_stages() -> None:
    """The predicate-covered set is exactly the FILE-KEYED AGENT tasks (phaze-k95r7).

    Not a hand-kept list: the membership rule is derived here from the two registries that actually
    decide it -- ``AGENT_TASKS`` (the work runs off-controller, so its ledger clear is a control-side
    callback that can be lost) and ``_KEY_BUILDERS`` keyed on a ``file_id`` (so a per-file completion
    predicate can exist at all). ``write_file_tags`` is agent-routed but keyed on a ``log_id``, so it
    is correctly out; every controller task is out because its clear rides ``after_process``.

    This is the guard that would have caught phaze-k95r7's actual defect: ``s3_upload`` satisfied both
    halves of the rule and had simply never been added, so it had NO completion exclusion of any kind
    and its rows were recovery candidates forever.
    """
    from phaze.services.enqueue_router import AGENT_TASKS

    def _keyed_on_file_id(function: str) -> bool:
        """True iff the function's key builder derives its natural id from ``file_id`` alone."""
        try:
            return _KEY_BUILDERS[function]({"file_id": "the-file-id"}) == "the-file-id"
        except (KeyError, TypeError):  # keyed on tracklist_id / log_id / a batch hash -> not file-keyed
            return False

    file_keyed = {fn for fn in _KEY_BUILDERS if fn in AGENT_TASKS and _keyed_on_file_id(fn)}
    assert file_keyed == _DOMAIN_COMPLETED_STAGES
    assert {"process_file", "extract_file_metadata", "push_file", "s3_upload"} == _DOMAIN_COMPLETED_STAGES


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
        cloud_lane_done=set(),
    )
    assert is_domain_completed(row, empty) is False


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

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 1, "errored": 0, "unreplayable": 0}


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
    assert result["stages"]["process_file"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}


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
async def test_in_flight_cloud_job_does_not_block_metadata_recovery(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-fc2l: a file with an in-flight cloud_job still recovers its orphaned extract_file_metadata row.

    The in-flight cloud_job owns only the analyze/push re-drive; metadata has no cloud second owner, so
    its lost row MUST recover (onto the fileserver's meta lane) -- the exclusion must not over-reach.
    MUTATION: reverting to the unscoped exclusion drops the metadata row -> reenqueued 0 -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_cloud_job(session, f.id, status=CloudJobStatus.SUBMITTED)  # in-flight analyze/push owner
    await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # The metadata row recovers onto the fileserver's meta lane despite the in-flight cloud_job.
    assert "nox-meta" in router.queues
    assert result["stages"]["extract_file_metadata"]["reenqueued"] == 1


@pytest.mark.asyncio
async def test_awaiting_cloud_job_does_not_block_metadata_recovery(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-fc2l: a file HELD awaiting cloud still recovers its orphaned extract_file_metadata row.

    The stage_cloud_window drain owns only the held file's analyze re-drive; a lost metadata row has no
    cloud owner and MUST recover. The companion of the in-flight case for the awaiting set.
    MUTATION: reverting to the unscoped awaiting exclusion drops the metadata row -> reenqueued 0 -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_awaiting_cloud_job(session, f.id)  # held long file -> drain owns ONLY its analyze re-drive
    await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert "nox-meta" in router.queues
    assert result["stages"]["extract_file_metadata"]["reenqueued"] == 1


@pytest.mark.asyncio
async def test_awaiting_cloud_job_still_excludes_the_process_file_row(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-fc2l guard: scoping does NOT weaken the analyze exclusion -- a held file's process_file row stays excluded.

    process_file IS a cloud-owned function, so the CLOUDROUTE-02 / single-owner guarantee is unchanged:
    a held awaiting-cloud file's process_file row is still excluded (the drain owns local analyze), never
    routed to the fileserver -- co-seeded here alongside a metadata row that DOES recover, proving the
    per-function scoping splits the two correctly.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_awaiting_cloud_job(session, f.id)
    await _seed_ledger(session, function="process_file", file_id=f.id)  # cloud-owned -> excluded
    await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)  # not cloud-owned -> recovers

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # process_file stays drain-owned (never analyzed locally); metadata recovers.
    assert "nox-analyze" not in router.queues
    assert result["stages"]["process_file"]["reenqueued"] == 0
    assert "nox-meta" in router.queues
    assert result["stages"]["extract_file_metadata"]["reenqueued"] == 1


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

    assert all(t == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0} for t in result["stages"].values())
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

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
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
    """CR-02: the D-10 gate compares a DB-read ``enqueued_at`` against an aware ``failed_at`` without raising.

    Originally this test's premise was that ``scheduling_ledger.enqueued_at`` came back NAIVE (it was
    ``TIMESTAMP WITHOUT TIME ZONE``) while ``metadata.failed_at`` came back aware, so a bare
    ``naive <= aware`` raised ``TypeError`` and aborted the whole recovery run. phaze-cz3m / migration 049
    made every timestamp column ``timestamptz``, so that premise is now FALSE by construction -- and the
    inverted assertion below (``tzinfo is not None``) is what keeps it that way: if anything ever restores
    a naive column here, this fails rather than silently reverting to the old hazard.

    The Cell A/B tests above build the ledger row IN MEMORY and never round-trip through
    ``get_ledger_rows``, so this remains the only case that exercises the real DB representation.
    The gate's defensive naive->UTC coercion is covered separately by
    ``test_d10_gate_coerces_a_naive_enqueued_at``, which no longer depends on the schema to produce one.
    """
    failed_at = datetime.now(UTC)
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_metadata(session, f.id, failed_at=failed_at)  # metadata FAILED (aware failed_at)
    key = await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    # Read the row back the way production does. Post-049 this is tz-AWARE.
    row = next(r for r in await get_ledger_rows(session) if r.key == key)
    assert row.enqueued_at.tzinfo is not None, "post-049 the ledger's enqueued_at must round-trip tz-aware"
    done_sets = await _build_done_sets(session, _ledger_fids([row]))

    # The committed row's server-default enqueued_at is AFTER failed_at (an orphaned retry) -> re-drives,
    # but the point is that the comparison COMPLETES without a TypeError.
    assert is_domain_completed(row, done_sets) is False


@pytest.mark.asyncio
async def test_d10_gate_coerces_a_naive_enqueued_at(session: AsyncSession) -> None:
    """The gate's naive->UTC coercion still holds, proven WITHOUT relying on the schema to emit a naive stamp.

    Before phaze-cz3m the naive value arrived for free from a ``TIMESTAMP WITHOUT TIME ZONE`` column, so
    the coercion was covered as a side effect of the schema being wrong. Migration 049 removed that
    source, which would have left the coercion untested and free to be deleted as dead defence. Setting
    the naive stamp explicitly keeps the behaviour pinned to intent rather than to a schema accident.

    MUTATION: dropping the ``tzinfo``-coercion at the gate (bare ``row.enqueued_at <= failed_at``) -> RED
    (``TypeError: can't compare offset-naive and offset-aware datetimes``).
    """
    failed_at = datetime.now(UTC)
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_metadata(session, f.id, failed_at=failed_at)  # metadata FAILED (aware failed_at)
    key = await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    row = next(r for r in await get_ledger_rows(session) if r.key == key)
    done_sets = await _build_done_sets(session, _ledger_fids([row]))

    # Force the hazard the coercion exists for: a naive stamp that PRE-DATES the aware failure marker.
    # Without coercion this comparison raises; with it the row reads as domain-complete (terminal).
    row.enqueued_at = (failed_at - timedelta(minutes=5)).replace(tzinfo=None)
    assert is_domain_completed(row, done_sets) is True


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


@pytest.mark.asyncio
async def test_completed_analyze_row_is_neither_orphan_nor_reenqueued(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``process_file`` row whose file has a SUCCESSFUL terminal analysis is excluded from recovery.

    The acceptance shape stated on the bead: such a row is neither counted as an orphan nor
    re-enqueued. ``is_domain_completed`` is asserted directly (the predicate the amber badge shares by
    definition, so this is the badge assertion too) alongside the enqueue side, so a future change
    cannot satisfy one and quietly break the other.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="process_file", file_id=f.id)
    await _seed_analysis(session, f.id, completed=True)

    rows = await get_ledger_rows(session)
    done_sets = await _build_done_sets(session, _ledger_fids(rows))
    assert [is_domain_completed(r, done_sets) for r in rows] == [True]  # -> excluded from the orphan set AND the badge

    router = DedupFakeTaskRouter()
    result = await recover_orphaned_work(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert [c for q in router.queues.values() for c in q.captured] == []


@pytest.mark.asyncio
async def test_partial_analysis_row_is_still_a_recovery_candidate(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PARTIAL ``analysis`` row does NOT make its file domain-complete -- it re-drives (DERIV-03).

    The companion to the test above, and the reason a "has an analysis row" corpus query disagrees
    with what recovery actually does. This seeds exactly what ``POST /agent/analysis/{id}/progress``
    writes -- window counters and an aggregate, NO ``analysis_completed_at``, NO ``failed_at`` -- i.e.
    an analysis that started and never finished. Re-driving it is correct: the work is genuinely owed.

    MUTATION: relax ``done_clause(ANALYZE)`` to bare row existence (or to ``failed_at IS NULL``) and
    this file silently stops being re-analyzed forever, which is the strictly worse failure.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="process_file", file_id=f.id)
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=f.id, bpm=128.0, fine_windows_analyzed=12, fine_windows_total=40))
    await session.commit()

    rows = await get_ledger_rows(session)
    done_sets = await _build_done_sets(session, _ledger_fids(rows))
    assert [is_domain_completed(r, done_sets) for r in rows] == [False]

    router = DedupFakeTaskRouter()
    result = await recover_orphaned_work(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result["stages"]["process_file"]["reenqueued"] == 1


@pytest.mark.asyncio
async def test_completed_s3_upload_row_is_not_a_recovery_candidate(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE 17-ROW REGRESSION: an ``s3_upload`` row whose file is analyzed is excluded from recovery.

    The exact live shape: the file exists, its analysis SUCCEEDED, and its ``cloud_job`` row has since
    been cleaned up (so NEITHER cloud exclusion -- in-flight nor awaiting -- can see it, because both
    key off a ``cloud_job`` row that is gone). Before the fix ``s3_upload`` was absent from
    ``_DOMAIN_COMPLETED_STAGES`` entirely, so the row reached the regenerator on EVERY run and tallied
    ``unreplayable`` forever.

    MUTATION: drop ``s3_upload`` from ``_DOMAIN_COMPLETED_STAGES`` and this goes back to
    ``unreplayable: 1``.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="s3_upload", file_id=f.id)
    await _seed_analysis(session, f.id, completed=True)

    rows = await get_ledger_rows(session)
    done_sets = await _build_done_sets(session, _ledger_fids(rows))
    assert [is_domain_completed(r, done_sets) for r in rows] == [True]

    router = DedupFakeTaskRouter()
    result = await recover_orphaned_work(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result["stages"]["s3_upload"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert result["unreplayable"] == 0


@pytest.mark.asyncio
async def test_pending_s3_upload_row_still_recovers(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The over-exclusion guard: an ``s3_upload`` row for an UNANALYZED file is still a candidate.

    The new completion exclusion must not swallow the stage. This file has no analysis at all and no
    ``cloud_job``, so it is not cloud-lane-done -- it reaches the regenerator exactly as before and is
    reported (here, ``unreplayable``: with no ``cloud_job`` row there is no staging attempt to
    re-drive). What matters is that the row is NOT silently dropped from recovery.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="s3_upload", file_id=f.id)

    rows = await get_ledger_rows(session)
    done_sets = await _build_done_sets(session, _ledger_fids(rows))
    assert [is_domain_completed(r, done_sets) for r in rows] == [False]

    router = DedupFakeTaskRouter()
    result = await recover_orphaned_work(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result["stages"]["s3_upload"]["unreplayable"] == 1


@pytest.mark.asyncio
async def test_stale_s3_upload_row_is_reported_as_stale_not_as_time_limited(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``existing is None`` is reported DISTINCTLY -- never as a time-limited/expired payload.

    ``_redrive_bucket`` returned ``None`` for two structurally different cases and ``redrive_upload``
    collapsed both into "could not resolve a staging bucket", which the caller then logged as "its
    payload is time-limited and cannot be regenerated right now". For a file with no ``cloud_job`` row
    at all, every word of that was false, and it cost an investigation on 2026-08-08.

    Asserted on the RAISED exception type (the contract) and on the absence of the misleading wording.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="s3_upload", file_id=f.id)

    from phaze.services.cloud_staging import NoCloudJobToRedriveError, redrive_upload

    with pytest.raises(NoCloudJobToRedriveError) as excinfo:
        await redrive_upload(session, f, DedupFakeTaskRouter())
    message = str(excinfo.value)
    assert "no cloud_job row" in message
    assert "could not resolve a staging bucket" not in message  # the merged message this case used to borrow

    caplog.set_level("WARNING")
    router = DedupFakeTaskRouter()
    await recover_orphaned_work(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    # The per-row warning names the real reason...
    assert "points at work that is not pending" in caplog.text
    # ...and NEITHER the per-row line NOR the run-level summary asserts a time-limited payload. The
    # substring "time-limited" survives only inside the new line's explicit denial, so the assertion is
    # on the CLAIM, not the word.
    assert "payload is time-limited and cannot be regenerated" not in caplog.text
    assert "payload is time-limited and could not be regenerated" not in caplog.text


@pytest.mark.asyncio
async def test_regenerate_row_isolated_missing_file_id_is_unreplayable(
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``s3_upload`` regeneration needs ``payload["file_id"]``; absent, the row is reported unreplayable."""
    target = _RegenTarget(key="s3_upload:none", function="s3_upload", payload={})
    stages: dict[str, dict[str, int]] = {}

    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        await _regenerate_row_isolated(session, DedupFakeTaskRouter(), target, stages)

    assert stages["s3_upload"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 1}
    assert "nothing durable to regenerate from" in caplog.text


@pytest.mark.asyncio
async def test_regenerate_row_isolated_non_uuid_file_id_is_unreplayable(
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stored ``file_id`` that does not parse as a UUID is reported unreplayable, not raised uncaught."""
    target = _RegenTarget(key="s3_upload:bad", function="s3_upload", payload={"file_id": "not-a-uuid"})
    stages: dict[str, dict[str, int]] = {}

    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        await _regenerate_row_isolated(session, DedupFakeTaskRouter(), target, stages)

    assert stages["s3_upload"]["unreplayable"] == 1
    assert "non-UUID file_id" in caplog.text


@pytest.mark.asyncio
async def test_regenerate_row_isolated_missing_file_is_unreplayable(
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A well-formed ``file_id`` that no longer names a ``FileRecord`` is reported unreplayable."""
    vanished_id = uuid.uuid4()
    target = _RegenTarget(key="s3_upload:gone", function="s3_upload", payload={"file_id": str(vanished_id)})
    stages: dict[str, dict[str, int]] = {}

    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        await _regenerate_row_isolated(session, DedupFakeTaskRouter(), target, stages)

    assert stages["s3_upload"]["unreplayable"] == 1
    assert "no longer exists" in caplog.text


@pytest.mark.asyncio
async def test_regenerate_row_isolated_unexpected_error_rolls_back_and_tallies_errored(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unanticipated exception from ``redrive_upload`` rolls back, drops parked enqueues, and re-raises
    to the caller -- which isolates it as ``errored`` rather than aborting the whole recovery run.
    """
    f = _make_file()
    session.add(f)
    await session.commit()
    target = _RegenTarget(key="s3_upload:boom", function="s3_upload", payload={"file_id": str(f.id)})
    stages: dict[str, dict[str, int]] = {}

    async def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "unexpected transport failure"
        raise RuntimeError(msg)

    monkeypatch.setattr("phaze.tasks.reenqueue.cloud_staging.redrive_upload", _boom)

    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        await _regenerate_row_isolated(session, DedupFakeTaskRouter(), target, stages)

    assert stages["s3_upload"]["errored"] == 1
    assert "row regeneration failed" in caplog.text
    # the session must still be usable afterward (BaseException handler rolled it back cleanly)
    assert await session.get(FileRecord, f.id) is not None


@pytest.mark.asyncio
async def test_regenerate_s3_upload_fired_zero_tallies_skipped(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``flush_pending_s3_enqueues`` returning 0 (SAQ deduped against a still-live job) tallies ``skipped``,
    the same "already covered, not lost" semantics a verbatim replay's dedup-``None`` return carries.
    """
    f = _make_file()
    session.add(f)
    await session.commit()
    target = _RegenTarget(key="s3_upload:dedup", function="s3_upload", payload={"file_id": str(f.id)})
    stages: dict[str, dict[str, int]] = {}

    async def _noop_redrive(*_args: object, **_kwargs: object) -> None:
        return None

    async def _zero_fired(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr("phaze.tasks.reenqueue.cloud_staging.redrive_upload", _noop_redrive)
    monkeypatch.setattr("phaze.tasks.reenqueue.cloud_staging.flush_pending_s3_enqueues", _zero_fired)

    await _regenerate_row_isolated(session, DedupFakeTaskRouter(), target, stages)

    assert stages["s3_upload"] == {"reenqueued": 0, "skipped": 1, "errored": 0, "unreplayable": 0}
