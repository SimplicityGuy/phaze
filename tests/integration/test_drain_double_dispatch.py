"""SC#3 (D-08) HARD GATE: the two-tick drain double-dispatch integration test (Phase 83, plan 06).

The sharpest regression this phase can introduce is a drain re-pick that dispatches an already
locally-dispatched (or terminally-failed) file a SECOND time -- and, worse, to a *cloud* backend.
The ROADMAP designates this a **hard gate**, not a recommendation: the sidecar drain cutover
(D-05/D-06/D-07) is not complete unless this test exists and passes.

It drives TWO sequential ``stage_cloud_window`` ticks across the three outcomes the drain must
exclude on the second tick, asserting each file is dispatched **exactly once** and **never to a
cloud backend after a local dispatch**:

  (a) LOCAL DISPATCH (happy path): tick 1 dispatches the held file to the local backend, committing
      its ``scheduling_ledger`` row with the tick; tick 2 must NOT re-pick it (``~inflight_clause``
      excludes it) even though its ``cloud_job(status='awaiting')`` row is still present (the D-05
      conjunct never deletes the row).

  (b) ROLLED-BACK TICK WITH A COMMITTED LEDGER ROW (the case row-deletion FAILS): tick 1 dispatches
      locally -- the ``process_file:<id>`` ledger row is committed by the ``before_enqueue`` hook's
      OWN session (modelled here by a separate committed session) -- but the drain tick then rolls
      back on a poisoned txn, discarding the drain's own writes (the ``LOCAL_ANALYZING`` flip). On
      tick 2, with a cloud backend now ONLINE, a re-pick would dispatch to CLOUD; the committed
      ledger row alone must re-exclude the file so it is dispatched exactly once, never cloud. The
      row-deletion variant would restore the deleted ``awaiting`` row on the rollback and re-pick.

  (c) TERMINALLY-FAILED LOCAL ANALYZE: a file whose analyze failed (``FAILURE_IS_TERMINAL[ANALYZE]``
      is True, so it is ``domain_completed``) carries its ``awaiting`` row but must be excluded by
      ``~domain_completed_clause(ANALYZE)`` and never dispatched, across both ticks.

Reuses the ``tests/analyze/tasks/test_release_awaiting_cloud.py`` scaffold (``_IsoCfg`` /
``_patch_backends`` / ``_make_ctx`` / ``DedupFakeQueue`` / ``DedupFakeTaskRouter`` /
``seed_active_agent``) to drive ``stage_cloud_window`` hermetically. Written to FAIL against the
current ``FileRecord.state``-based drain (RED): cases (b) and (c) re-pick / dispatch a file the
sidecar drain must exclude. Task 2 (the D-05/D-06/D-07 cutover) turns it GREEN.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.backends import LocalBackend
from phaze.tasks.release_awaiting_cloud import stage_cloud_window
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


# A fixed naive lane-entry timestamp: create_all yields NAIVE created_at/updated_at (models/base.py
# carries no timezone=True), so the select_backend staleness subtraction (now - file.updated_at)
# stays same-awareness. Well within cloud_spill_to_local_after_seconds so the staleness gate never
# fires -- local eligibility is decided by cloud availability alone in these cells.
_SEEDED_AT = datetime(2024, 1, 1, 12, 0, 0)  # naive on purpose (matches create_all's non-tz columns)


class _IsoCfg:
    """Minimal registry-derived cfg (cloud on; the two bounded select_backend knobs)."""

    def __init__(self) -> None:
        self.cloud_enabled = True
        self.cloud_submit_max_attempts = 3
        self.cloud_spill_to_local_after_seconds = 900


def _patch_backends(monkeypatch: pytest.MonkeyPatch, backends: list[Any]) -> None:
    """Pin the drain's ``get_settings`` + ``resolve_backends`` seams to the stub cfg + backend list."""
    monkeypatch.setattr("phaze.tasks.release_awaiting_cloud.get_settings", lambda: _IsoCfg())
    monkeypatch.setattr("phaze.services.backends.resolve_backends", lambda cfg: list(backends))  # noqa: ARG005


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter) -> dict[str, Any]:
    """Build a controller-shaped ctx: async_session sessionmaker + controller queue + dedup router."""
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "queue": DedupFakeQueue("controller"), "task_router": router}


class _CommitRaisesSessionmaker:
    """Wrap a sessionmaker so the yielded drain session RAISES on ``commit()`` -- models a poisoned
    txn whose whole-tick rollback discards the drain's own writes (the ``LOCAL_ANALYZING`` flip),
    while an independently-committed ledger row (written by dispatch's separate hook session) survives.
    ``rollback()`` is untouched, so the tick's outer safety net cleanly holds every candidate.
    """

    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sm

    def __call__(self) -> _CommitRaisesCtx:
        return _CommitRaisesCtx(self._sm())


class _CommitRaisesCtx:
    def __init__(self, cm: Any) -> None:
        self._cm = cm

    async def __aenter__(self) -> AsyncSession:
        session = await self._cm.__aenter__()

        async def _raise() -> None:
            raise RuntimeError("poisoned txn: the whole drain tick rolls back")

        session.commit = _raise  # type: ignore[method-assign]  # AsyncSession permits instance attr assignment
        return session

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._cm.__aexit__(*exc)


class _LocalStub(LocalBackend):
    """A ``LocalBackend`` (so ``select_backend`` treats it as local) whose ``dispatch`` records the
    call and writes the ``process_file:<id>`` scheduling_ledger row the way the real ``before_enqueue``
    hook does -- the fact that makes ``~inflight_clause(ANALYZE)`` exclude the file on the next tick.

    ``ledger_engine`` set => commit the ledger row in a SEPARATE session (models the hook's OWN
    independent commit that survives a rolled-back drain tick, case (b)); unset => write it in the
    caller's drain session so it commits WITH the tick (case (a)). Flips ``file.state`` to
    ``LOCAL_ANALYZING`` (D-13 dual-write) and never commits the drain session (dispatch discipline).
    """

    def __init__(self, *, id: str = "local", rank: int = 99, cap: int = 100, ledger_engine: AsyncEngine | None = None) -> None:
        super().__init__(id=id, rank=rank, cap=cap, config=None)
        self.dispatched_ids: list[uuid.UUID] = []
        self._ledger_engine = ledger_engine

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- protocol signature
        return 0

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: Any) -> bool:  # noqa: ARG002
        self.dispatched_ids.append(file.id)
        ledger = SchedulingLedger(
            key=f"process_file:{file.id}",
            function="process_file",
            routing="agent",
            payload={"file_id": str(file.id)},
        )
        if self._ledger_engine is not None:
            # The before_enqueue hook commits the ledger in its OWN session -> survives a tick rollback.
            hook_sm = async_sessionmaker(self._ledger_engine, class_=AsyncSession, expire_on_commit=False)
            async with hook_sm() as hook_session:
                hook_session.add(ledger)
                await hook_session.commit()
        else:
            # Case (a): write in the drain session -> committed WITH the tick's single post-loop commit.
            session.add(ledger)
        file.state = FileState.LOCAL_ANALYZING  # D-13 dual-write (rolled back in case (b), stands in case (a))
        return True

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:  # noqa: ARG002
        return None


class _CloudStub:
    """A duck-typed (non-``LocalBackend``) cloud backend: ``select_backend`` treats it as cloud, so a
    lower ``rank`` beats local when it is available. ``available`` is MUTABLE so a cell can force local
    on tick 1 (offline) then bring cloud ONLINE on tick 2 -- making any buggy re-pick land on CLOUD,
    which the sidecar drain must prevent. ``dispatch`` promotes the file's ``awaiting`` cloud_job row.
    """

    def __init__(self, *, id: str = "kueue-a", rank: int = 10, cap: int = 50, available: bool = False) -> None:
        self.id = id
        self.rank = rank
        self.cap = cap
        self.available = available
        self.dispatched_ids: list[uuid.UUID] = []

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature
        return self.available

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- protocol signature
        return 0

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: Any) -> bool:  # noqa: ARG002
        self.dispatched_ids.append(file.id)
        # Promote the existing awaiting row (uq_cloud_job_file_id -> UPDATE, not a second INSERT).
        await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(status=CloudJobStatus.SUBMITTED.value, backend_id=self.id))
        file.state = FileState.PUSHING
        return True

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:  # noqa: ARG002
        return None


async def _seed_awaiting_file(session: AsyncSession, *, attempts: int = 0) -> FileRecord:
    """Seed ONE held file: ``state=AWAITING_CLOUD`` + a ``cloud_job(status='awaiting')`` sidecar row."""
    uid = uuid.uuid4()
    file = FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.flac",
        original_filename=f"{uid.hex}.flac",
        current_path=f"/music/{uid.hex}.flac",
        file_type="flac",
        file_size=1000,
        state=FileState.AWAITING_CLOUD,
        created_at=_SEEDED_AT,
        updated_at=_SEEDED_AT,
    )
    session.add(file)
    await session.commit()  # commit the FileRecord first so the cloud_job FK resolves (no ORM relationship to order them)
    session.add(CloudJob(id=uuid.uuid4(), file_id=uid, status=CloudJobStatus.AWAITING.value, attempts=attempts))
    await session.commit()
    return file


async def _cloud_job_status(session: AsyncSession, file_id: uuid.UUID) -> str | None:
    # A scalar-column select always round-trips to the DB (cloud_job.status is not identity-mapped), so
    # no expire is needed -- and expiring here would evict the caller's still-referenced FileRecord.
    return (await session.execute(select(CloudJob.status).where(CloudJob.file_id == file_id))).scalar_one_or_none()


# --- Case (a): local dispatch on tick 1, NOT re-picked on tick 2 (exactly once, never cloud) -------


@pytest.mark.asyncio
async def test_sc3_case_a_local_dispatch_not_repicked_on_second_tick(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) A held file dispatched to local on tick 1 is excluded on tick 2 by its committed ledger row.

    Cloud is OFFLINE on tick 1 (forcing local) and ONLINE on tick 2 (so a buggy re-pick would dispatch
    to CLOUD). The ``cloud_job(status='awaiting')`` row is NEVER deleted (D-05 conjunct, not deletion),
    yet ``~inflight_clause(ANALYZE)`` -- backed by the committed ``process_file:<id>`` ledger row --
    keeps the file out of the tick-2 candidate set. Dispatched exactly once, to local, never cloud.
    """
    local = _LocalStub(id="local", rank=99, cap=100)  # ledger written in the drain session (commits with the tick)
    cloud = _CloudStub(id="kueue-a", rank=10, cap=50, available=False)
    _patch_backends(monkeypatch, [cloud, local])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    file = await _seed_awaiting_file(session)

    router = DedupFakeTaskRouter()
    tick1 = await stage_cloud_window(_make_ctx(async_engine, router))
    assert tick1 == {"staged": 1, "skipped": 0}  # dispatched to local this tick

    cloud.available = True  # bring cloud ONLINE: a re-pick on tick 2 would land on cloud (rank 10 < 99)
    tick2 = await stage_cloud_window(_make_ctx(async_engine, router))
    assert tick2 == {"staged": 0, "skipped": 0}  # excluded -> no candidate, nothing dispatched

    assert local.dispatched_ids == [file.id]  # exactly once, to local
    assert cloud.dispatched_ids == []  # NEVER cloud
    # The awaiting sidecar row is retained (the conjunct excludes by ~inflight, it does not delete).
    assert await _cloud_job_status(session, file.id) == CloudJobStatus.AWAITING.value


# --- Case (b): rolled-back tick with a committed ledger row (the row-deletion FAILURE case) --------


@pytest.mark.asyncio
async def test_sc3_case_b_rolled_back_tick_committed_ledger_not_repicked(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) A locally-dispatched file whose drain tick ROLLS BACK is still excluded on tick 2 by the
    independently-committed ledger row -- dispatched exactly once, never cloud.

    Tick 1 dispatches locally (the ``process_file:<id>`` ledger row is committed by a SEPARATE session,
    modelling the ``before_enqueue`` hook's own commit), then the drain tick's post-loop ``commit()``
    raises -> the whole tick rolls back, discarding the ``LOCAL_ANALYZING`` flip (state stays
    AWAITING_CLOUD, the awaiting row stays). Tick 2 brings cloud ONLINE: the deletion variant would
    have restored a deleted awaiting row and re-picked the file to CLOUD, but the committed ledger row
    alone re-excludes it (``~inflight_clause``). This is the load-bearing D-05 case.
    """
    local = _LocalStub(id="local", rank=99, cap=100, ledger_engine=async_engine)  # ledger committed in a SEPARATE session
    cloud = _CloudStub(id="kueue-a", rank=10, cap=50, available=False)
    _patch_backends(monkeypatch, [cloud, local])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    file = await _seed_awaiting_file(session)

    router = DedupFakeTaskRouter()
    # Tick 1: the drain session's post-loop commit raises -> whole-tick rollback (clean hold).
    poisoned_ctx = _make_ctx(async_engine, router)
    poisoned_ctx["async_session"] = _CommitRaisesSessionmaker(poisoned_ctx["async_session"])
    tick1 = await stage_cloud_window(poisoned_ctx)
    assert tick1 == {"staged": 0, "skipped": 1}  # rolled back -> reported held

    # The drain's own writes were discarded, but the hook's ledger row survives, and the awaiting row stands.
    state = (await session.execute(select(FileRecord.state).where(FileRecord.id == file.id))).scalar_one()
    assert state == FileState.AWAITING_CLOUD  # LOCAL_ANALYZING flip rolled back
    assert await _cloud_job_status(session, file.id) == CloudJobStatus.AWAITING.value

    cloud.available = True  # ONLINE: a re-pick on tick 2 would dispatch to CLOUD
    tick2 = await stage_cloud_window(_make_ctx(async_engine, router))
    assert tick2 == {"staged": 0, "skipped": 0}  # committed ledger row excludes -> no candidate

    assert local.dispatched_ids == [file.id]  # dispatched exactly once (tick 1), to local
    assert cloud.dispatched_ids == []  # NEVER cloud after the local dispatch


# --- Case (c): terminally-failed local analyze is never (re-)picked -------------------------------


@pytest.mark.asyncio
async def test_sc3_case_c_terminally_failed_analyze_never_dispatched(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) A file whose local analyze terminally FAILED carries its awaiting row but is excluded by
    ``~domain_completed_clause(ANALYZE)`` and is never dispatched across two ticks.

    ``FAILURE_IS_TERMINAL[ANALYZE]`` is True, so a failed ``analysis`` row (``failed_at`` set,
    ``analysis_completed_at`` NULL) makes the file ``domain_completed`` -- it must not be re-driven. A
    cloud backend is ONLINE both ticks (rank 10), so the state-based drain would dispatch it to CLOUD;
    the sidecar drain must exclude it entirely.
    """
    local = _LocalStub(id="local", rank=99, cap=100)
    cloud = _CloudStub(id="kueue-a", rank=10, cap=50, available=True)
    _patch_backends(monkeypatch, [cloud, local])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    file = await _seed_awaiting_file(session)
    # Terminal analyze failure: domain_completed(ANALYZE) True (FAILED ∧ FAILURE_IS_TERMINAL[ANALYZE]).
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=file.id, failed_at=_SEEDED_AT, analysis_completed_at=None))
    await session.commit()

    router = DedupFakeTaskRouter()
    tick1 = await stage_cloud_window(_make_ctx(async_engine, router))
    tick2 = await stage_cloud_window(_make_ctx(async_engine, router))

    assert tick1 == {"staged": 0, "skipped": 0}  # excluded -> no candidate
    assert tick2 == {"staged": 0, "skipped": 0}
    assert local.dispatched_ids == []  # never dispatched
    assert cloud.dispatched_ids == []  # never dispatched -- and specifically never cloud
