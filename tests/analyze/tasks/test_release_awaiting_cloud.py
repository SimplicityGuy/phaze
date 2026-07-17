"""Phase 70 (MKUE-03 / D-07): per-cluster failure isolation in the ``stage_cloud_window`` drain tick.

A single flaky Kueue cluster whose once-per-tick ``is_available()`` / ``in_flight_count()`` snapshot
probe OR whose ``dispatch()`` call raises or times out must NOT abort the whole ``*/5`` drain tick
(research Pitfall 8). This extends Phase 68's "``is_available`` never raises" discipline to the
N-cluster snapshot loop:

  * a raise from the per-backend SNAPSHOT (is_available / in_flight_count) -> that backend contributes
    0 free slots for the tick (available=False, remaining=0), is logged (``backend_id`` only, T-70-03-02),
    and every healthy backend (and local) still receives work;
  * a GENERIC kube/S3 raise from ``dispatch()`` -> a clean per-candidate hold (the file stays
    AWAITING_CLOUD, counted skipped) and the loop CONTINUES to the next candidate -- the tick never
    aborts and never raises;
  * a ``NoActiveAgentError`` from ``dispatch()`` still holds ALL remaining candidates and BREAKS (the
    existing fileserver-vanish semantics are preserved -- distinct from the generic path).

The drain resolves its backends via ``resolve_backends(cfg)``; these tests patch that seam to inject
lightweight stub backends whose probe/dispatch behavior is controllable. The pure ``select_backend``
policy (real, unpatched) routes candidates rank-first over the resulting snapshot. Local detection in
that policy is ``isinstance(..., LocalBackend)``, so a non-local stub is treated as a cloud backend --
exactly the Kueue role these tests model.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select, update

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services.enqueue_router import NoActiveAgentError
from phaze.tasks import release_awaiting_cloud
from phaze.tasks.release_awaiting_cloud import _bounded_is_available, stage_cloud_window
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


# --- Stub backends whose snapshot/dispatch behavior is controllable ----------------------


class _StubBackend:
    """A duck-typed ``Backend`` (kueue role) whose per-tick probe + dispatch can be made to raise.

    NOT a ``LocalBackend`` subclass, so the pure ``select_backend`` policy treats it as a cloud
    backend (``isinstance(..., LocalBackend)`` is False). ``raise_on`` selects which lifecycle call
    blows up: ``"is_available"`` / ``"in_flight_count"`` (the snapshot legs), ``"dispatch"`` (a generic
    kube/S3 raise -- phaze-uciu.3: writes the row THEN raises, inside its own ``session.begin_nested()``
    SAVEPOINT, mirroring the fixed ``ComputeAgentBackend`` / ``KueueBackend`` post-write-raise shape) or
    ``"dispatch_noagent"`` (a fileserver-vanish ``NoActiveAgentError``, raised before any write). ``hang_on``
    (DRAIN-02) selects a lifecycle call that HANGS forever instead of raising -- currently only
    ``"is_available"`` is modeled (the real hang vector: KueueBackend.is_available -> kr8s LocalQueue
    refresh() on an httpx client built ``timeout=None``). A healthy stub flips its candidate to PUSHING +
    writes a ``backend_id``-scoped ``cloud_job`` row (no commit -- the drain owns the single post-loop
    commit) and returns ``True`` (a genuine stage).
    """

    def __init__(self, *, id: str, rank: int, cap: int, raise_on: str | None = None, hang_on: str | None = None) -> None:
        self.id = id
        self.rank = rank
        self.cap = cap
        self._raise_on = raise_on
        self._hang_on = hang_on
        self.dispatch_calls = 0

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature
        if self._hang_on == "is_available":
            # DRAIN-02: model the real hang vector -- a probe that NEVER returns (partitioned/hung
            # API server on a ``timeout=None`` kr8s client). Without the drain's asyncio.wait_for bound
            # this would stall the whole tick while holding the advisory lock.
            await asyncio.Event().wait()
        if self._raise_on == "is_available":
            raise RuntimeError(f"{self.id}: cluster reachability probe blew up")
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- protocol signature
        if self._raise_on == "in_flight_count":
            raise RuntimeError(f"{self.id}: in_flight_count query blew up")
        return 0

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: Any) -> bool:  # noqa: ARG002 -- protocol signature
        self.dispatch_calls += 1
        if self._raise_on == "dispatch":
            # phaze-uciu.3: WRITE the row FIRST, then raise -- mirroring the real ComputeAgentBackend /
            # KueueBackend post-write-raise shape (an enqueue failure AFTER the cloud_job upsert) rather
            # than the pre-fix stub, which raised BEFORE touching the row and so could never distinguish
            # "the write never happened" from "the write happened and was rolled back". Wrapping the
            # write in the SAME session.begin_nested() SAVEPOINT the fixed backends use proves the
            # assertions below (awaiting/backend_id=None) hold because the SAVEPOINT rolled the write
            # back -- not merely because nothing was ever written.
            async with session.begin_nested():
                await session.execute(
                    update(CloudJob).where(CloudJob.file_id == file.id).values(backend_id=self.id, status=CloudJobStatus.SUBMITTED.value)
                )
                raise RuntimeError(f"{self.id}: kube submit / S3 stage blew up")
        if self._raise_on == "dispatch_noagent":
            raise NoActiveAgentError("fileserver")
        # Post-MIG-04 there is no ``files.state`` dual-write: the file's push status is DERIVED from the
        # cloud_job sidecar alone. The held file already carries an awaiting cloud_job row (the sidecar
        # drain's INNER join requires it), so PROMOTE it (mirrors ComputeAgentBackend/KueueBackend's
        # on_conflict_do_update) rather than INSERTing a second row -- a fresh INSERT would violate
        # uq_cloud_job_file_id.
        await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(backend_id=self.id, status=CloudJobStatus.SUBMITTED.value))
        return True

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:  # noqa: ARG002
        return None


class _IsoCfg:
    """Minimal registry-derived cfg for the isolation tests (cloud on; the two select_backend knobs)."""

    def __init__(self) -> None:
        self.cloud_enabled = True
        # The pure select_backend policy reads these two bounded knobs (D-04 attempt-exclusion + the
        # D-01/D-03 local-spill staleness gate). No local backend is present in these tests, so the
        # staleness gate never fires; the values just need to exist.
        self.cloud_submit_max_attempts = 3
        self.cloud_spill_to_local_after_seconds = 900


def _patch_backends(monkeypatch: pytest.MonkeyPatch, backends: list[_StubBackend]) -> None:
    """Pin the drain's ``get_settings`` + ``resolve_backends`` seams to the stub cfg + stub backend list.

    ``stage_cloud_window`` imports ``resolve_backends`` deferred from ``phaze.services.backends`` (to
    break the backend<->drain import cycle), so patch the attribute on that source module -- the
    ``from ... import resolve_backends`` inside the function then binds the stub at call time.
    """
    monkeypatch.setattr("phaze.tasks.release_awaiting_cloud.get_settings", lambda: _IsoCfg())
    monkeypatch.setattr("phaze.services.backends.resolve_backends", lambda cfg: list(backends))  # noqa: ARG005


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter) -> dict[str, Any]:
    """Build a controller-shaped ctx: async_session + controller queue + dedup router.

    92-04 (CLEAN-02): ``async_session`` is sourced from ``phaze.database.async_session`` -- monkeypatched by the
    ``session`` fixture's ``_route_stats_fanout`` to a factory BOUND to the per-test ``_db_connection``
    (``join_transaction_mode="create_savepoint"``), exactly as the production controller wires
    ``ctx["async_session"]``. This lets the task SEE seeded rows and makes its commits visible to sibling
    reads under create_savepoint isolation (a fresh ``async_sessionmaker(async_engine)`` would open a
    DIFFERENT pool connection and read ZERO/STALE).
    """
    from phaze.database import async_session

    return {"async_session": async_session, "queue": DedupFakeQueue("controller"), "task_router": router}


def _make_file() -> FileRecord:
    """Build a fully-populated AWAITING_CLOUD FileRecord row."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.flac",
        original_filename=f"{uid.hex}.flac",
        current_path=f"/music/{uid.hex}.flac",
        file_type="flac",
        file_size=1000,
    )


async def _seed_awaiting_rows(session: AsyncSession, files: list[FileRecord]) -> None:
    """Give each held AWAITING_CLOUD file its ``cloud_job(status='awaiting')`` sidecar row (Phase 83, D-05).

    The sidecar drain (``get_cloud_staging_candidates``) INNER-joins ``cloud_job`` on ``status='awaiting'``
    (SC#1: no ``FileRecord.state`` read), so a bare ``state`` write is no longer a drain candidate.
    """
    for f in files:
        session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, status=CloudJobStatus.AWAITING.value))
    await session.commit()


async def _states_for(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Derive each file's push status from its cloud_job sidecar row (post-MIG-04 authority).

    ``awaiting`` = still held (AWAITING_CLOUD); ``submitted`` = dispatched to a cloud backend
    (the old PUSHING). There is no scalar ``files.state`` to read anymore.
    """
    session.expire_all()
    rows = (await session.execute(select(CloudJob.file_id, CloudJob.status).where(CloudJob.file_id.in_(ids)))).all()
    return dict(rows)


async def _backend_ids_for(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    session.expire_all()
    rows = (await session.execute(select(CloudJob.file_id, CloudJob.backend_id).where(CloudJob.file_id.in_(ids)))).all()
    return dict(rows)


# --- MKUE-03 / D-07: a flaky cluster's SNAPSHOT probe raising is isolated ------------------


@pytest.mark.asyncio
async def test_stage_cloud_window_isolation_is_available_raise_does_not_poison_tick(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N=2 kueue backends; backend A.is_available RAISES -> the tick survives; A gets 0 slots, B still dispatches.

    Before the D-07 guard (RED): the un-wrapped snapshot loop propagates A's RuntimeError straight out
    of ``stage_cloud_window`` -- the whole ``*/5`` tick aborts and NO healthy backend gets work (Pitfall 8).
    After: A degrades to available=False / remaining=0 (logged), so both FIFO candidates route to the
    healthy backend B (rank-first over the surviving snapshot).
    """
    flaky = _StubBackend(id="kueue-a", rank=10, cap=5, raise_on="is_available")
    healthy = _StubBackend(id="kueue-b", rank=20, cap=5)
    _patch_backends(monkeypatch, [flaky, healthy])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(2)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    # MUST NOT raise -- one flaky cluster cannot poison the whole drain tick.
    result = await stage_cloud_window(_make_ctx(async_engine, router))

    assert result == {"staged": 2, "skipped": 0}
    # Both candidates landed on the HEALTHY backend; the flaky one contributed nothing.
    backend_ids = await _backend_ids_for(session, ids)
    assert set(backend_ids.values()) == {"kueue-b"}
    assert set((await _states_for(session, ids)).values()) == {CloudJobStatus.SUBMITTED.value}
    assert flaky.dispatch_calls == 0  # a 0-slot flaky backend is never selected


@pytest.mark.asyncio
async def test_stage_cloud_window_isolation_in_flight_count_raise_treats_backend_as_zero_slots(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend whose ``in_flight_count`` RAISES is treated as remaining=0 for the tick; others unaffected.

    The snapshot guard wraps BOTH probe legs -- a raise from ``in_flight_count`` (not just
    ``is_available``) collapses that backend's slot to available=False / remaining=0, so the healthy
    backend absorbs every candidate and the tick never aborts.
    """
    flaky = _StubBackend(id="kueue-a", rank=10, cap=5, raise_on="in_flight_count")
    healthy = _StubBackend(id="kueue-b", rank=20, cap=5)
    _patch_backends(monkeypatch, [flaky, healthy])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(2)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router))

    assert result == {"staged": 2, "skipped": 0}
    assert set((await _backend_ids_for(session, ids)).values()) == {"kueue-b"}
    assert flaky.dispatch_calls == 0


# --- MCOMP-05: a flaky COMPUTE lane degrades to 0 slots; a healthy sibling compute lane still dispatches ---


@pytest.mark.asyncio
async def test_mcomp05_flaky_compute_backend_degrades_to_zero_slots_healthy_compute_lane_still_dispatches(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCOMP-05 (compute failure isolation): N=2 COMPUTE lanes; lane A.is_available RAISES -> A gets 0 slots, the tick COMPLETES, and healthy lane B still dispatches.

    The per-agent twin of the Kueue isolation cell: one flaky compute backend (its liveness probe
    raises / times out) must NOT abort the whole ``*/5`` drain tick and starve every healthy compute
    lane. The per-backend snapshot try/except (release_awaiting_cloud.py:151-157) degrades the flaky
    lane to ``available=False / remaining=0`` (T-73-11 DoS mitigation), so both FIFO candidates route to
    the healthy sibling compute lane, and the flaky lane is never selected (0 dispatch calls). Runs on
    the existing one-row-per-file cloud_job schema (D-05, no migration).
    """
    flaky_compute = _StubBackend(id="compute-a", rank=10, cap=5, raise_on="is_available")
    healthy_compute = _StubBackend(id="compute-b", rank=20, cap=5)
    _patch_backends(monkeypatch, [flaky_compute, healthy_compute])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(2)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    # The tick MUST COMPLETE (never raise) despite the flaky compute lane's probe blowing up.
    result = await stage_cloud_window(_make_ctx(async_engine, router))

    assert result == {"staged": 2, "skipped": 0}
    # Every candidate landed on the HEALTHY compute lane; the flaky one contributed nothing.
    assert set((await _backend_ids_for(session, ids)).values()) == {"compute-b"}
    assert set((await _states_for(session, ids)).values()) == {CloudJobStatus.SUBMITTED.value}
    assert flaky_compute.dispatch_calls == 0  # a 0-slot flaky lane is never selected


# --- MKUE-03 / D-07: a GENERIC dispatch raise is a clean per-candidate hold (loop continues) ---


@pytest.mark.asyncio
async def test_stage_cloud_window_isolation_generic_dispatch_raise_holds_candidate_and_continues(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic kube/S3 raise from ``dispatch`` holds THIS candidate (skipped) and CONTINUES the loop.

    Before the D-07 widening (RED): the dispatch guard catches ONLY ``NoActiveAgentError``, so a generic
    ``RuntimeError`` propagates straight out and aborts the tick. After: a distinct ``except Exception``
    branch counts the candidate skipped, leaves it AWAITING_CLOUD, and iterates to the NEXT candidate.
    With 2 candidates both routing to the raising backend, ``dispatch`` is invoked TWICE (the loop did
    NOT break) and the tick returns cleanly without raising.

    phaze-uciu.3 (was "cements the wrong invariant"): the stub's ``dispatch`` now WRITES the
    ``cloud_job`` row (``status='submitted'``, ``backend_id`` set) BEFORE raising, inside its own
    ``session.begin_nested()`` SAVEPOINT -- mirroring the real ``ComputeAgentBackend`` /
    ``KueueBackend`` post-write-raise shape (an enqueue failure AFTER the row write) instead of the
    old pre-fix stub that raised before touching the row at all (a shape a pre-D-01 backend could
    never actually produce, since the real bug was a write that SURVIVED an enqueue failure). The
    "no state mutated" assertions below now hold because the SAVEPOINT rolled the write back, not
    because dispatch never attempted one -- this is what a regression on the SAVEPOINT fix would fail.
    """
    raiser = _StubBackend(id="kueue-a", rank=10, cap=5, raise_on="dispatch")
    _patch_backends(monkeypatch, [raiser])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(2)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    # MUST NOT raise -- a generic dispatch failure is a clean per-candidate hold.
    result = await stage_cloud_window(_make_ctx(async_engine, router))

    assert result == {"staged": 0, "skipped": 2}
    # The loop CONTINUED past the first raise (both candidates attempted), never broke.
    assert raiser.dispatch_calls == 2
    # Both files stay AWAITING_CLOUD and grow NO cloud_job row (the raising path mutated nothing).
    assert set((await _states_for(session, ids)).values()) == {CloudJobStatus.AWAITING.value}
    # The awaiting sidecar rows are RETAINED with backend_id NULL -- no file was dispatched to a backend
    # (D-05 keeps the row; the raising/rollback path stamps no backend_id).
    assert set((await _backend_ids_for(session, ids)).values()) == {None}


# --- Preserved semantics: a dispatch NoActiveAgentError holds ALL remaining + BREAKS -----------


@pytest.mark.asyncio
async def test_stage_cloud_window_isolation_dispatch_noactiveagent_holds_all_and_breaks(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``NoActiveAgentError`` from ``dispatch`` still holds ALL remaining candidates and BREAKS (unchanged).

    The fileserver-vanish semantics are DISTINCT from the generic hold: a fileserver revoked mid-tick
    affects EVERY remaining dispatch, so the drain holds all not-yet-dispatched candidates and breaks
    (``dispatch`` invoked exactly ONCE), rather than continuing per-candidate. This preserves the
    existing WR-02 behavior alongside the new generic branch.
    """
    vanish = _StubBackend(id="kueue-a", rank=10, cap=5, raise_on="dispatch_noagent")
    _patch_backends(monkeypatch, [vanish])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(2)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router))

    assert result == {"staged": 0, "skipped": 2}
    # BREAK semantics: the fileserver-vanish path holds ALL remaining after ONE failing dispatch.
    assert vanish.dispatch_calls == 1
    assert set((await _states_for(session, ids)).values()) == {CloudJobStatus.AWAITING.value}
    # The awaiting sidecar rows are RETAINED with backend_id NULL -- no file was dispatched to a backend
    # (D-05 keeps the row; the raising/rollback path stamps no backend_id).
    assert set((await _backend_ids_for(session, ids)).values()) == {None}


# --- CR-02: an unexpected raise (poisoned txn) is caught by the tick safety net (cron NEVER raises) ---


@pytest.mark.asyncio
async def test_stage_cloud_window_unexpected_error_rolls_back_and_never_raises(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-02 safety net: an unexpected raise from the loop body (a poisoned-txn statement) is caught, the tick rolls back, and the cron returns a clean hold.

    Models a Postgres serialization/deadlock surfaced from a ``session.execute`` in the loop body that sits
    OUTSIDE the per-candidate dispatch try (here: ``_cloud_attempts_for``). Before the fix this propagated
    straight out of ``stage_cloud_window`` (and, downstream, a poisoned txn made the single post-loop
    ``session.commit()`` raise), violating the T-50-cron-raise NEVER-raises discipline. After the fix the
    outer guard rolls the whole tick back and returns ``{"staged": 0, "skipped": len(candidates)}`` -- every
    candidate stays AWAITING_CLOUD with no cloud_job row, and no partial write is committed.
    """
    healthy = _StubBackend(id="kueue-a", rank=10, cap=5)
    _patch_backends(monkeypatch, [healthy])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(2)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    # Force an unexpected raise from the loop body OUTSIDE the per-candidate dispatch try.
    monkeypatch.setattr(
        "phaze.tasks.release_awaiting_cloud._cloud_attempts_for",
        AsyncMock(side_effect=RuntimeError("current transaction is aborted")),
    )

    router = DedupFakeTaskRouter()
    # MUST NOT raise -- the tick safety net degrades an unexpected/poisoned-txn error to a clean hold.
    result = await stage_cloud_window(_make_ctx(async_engine, router))

    assert result == {"staged": 0, "skipped": 2}
    assert healthy.dispatch_calls == 0  # the raise fired before any dispatch was attempted
    # Whole tick rolled back: no file flipped AWAITING_CLOUD -> PUSHING, no cloud_job row written.
    assert set((await _states_for(session, ids)).values()) == {CloudJobStatus.AWAITING.value}
    # The awaiting sidecar rows are RETAINED with backend_id NULL -- no file was dispatched to a backend
    # (D-05 keeps the row; the raising/rollback path stamps no backend_id).
    assert set((await _backend_ids_for(session, ids)).values()) == {None}


# --- DRAIN-02 (Phase 98): a HUNG availability probe is bounded, not left to stall the tick ---------


@pytest.mark.asyncio
async def test_bounded_is_available_times_out_on_a_hanging_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """DRAIN-02 unit (no DB): a probe that never returns raises TimeoutError within the bound; a healthy probe passes through.

    Directly exercises the ``_bounded_is_available`` guard that the snapshot loop now uses. This is the
    fails-before/passes-after crux distilled to a runnable, DB-free assertion: the UNBOUNDED baseline (a
    direct ``await backend.is_available(session)``) never completes within the window, while the bounded
    helper converts that same hang into a ``TimeoutError`` -- the error class the snapshot loop's existing
    ``except Exception`` treats as "unavailable, 0 slots". ``_PROBE_TIMEOUT_SEC`` is shrunk via monkeypatch
    so the assertion is fast.
    """
    # The stub's is_available ignores its session arg (ARG002), so a sentinel is sufficient here (no DB).
    _UNUSED_SESSION: Any = object()
    monkeypatch.setattr(release_awaiting_cloud, "_PROBE_TIMEOUT_SEC", 0.05)
    hanging = _StubBackend(id="kueue-hung", rank=10, cap=5, hang_on="is_available")

    # Baseline (the pre-fix behavior): the raw probe hangs -- it does NOT resolve within the window.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(hanging.is_available(_UNUSED_SESSION), 0.05)

    # The fix: the bounded helper turns the hang into a TimeoutError (caught downstream as 0 slots).
    with pytest.raises(TimeoutError):
        await _bounded_is_available(hanging, _UNUSED_SESSION)

    # A healthy probe passes straight through the bound unchanged.
    healthy = _StubBackend(id="kueue-ok", rank=20, cap=5)
    assert await _bounded_is_available(healthy, _UNUSED_SESSION) is True


@pytest.mark.asyncio
async def test_stage_cloud_window_hung_probe_does_not_stall_tick_healthy_backend_still_dispatches(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRAIN-02 end-to-end: N=2 kueue backends; backend A.is_available HANGS -> the tick still COMPLETES and healthy backend B dispatches.

    Before the wait_for bound (RED): the snapshot loop's un-bounded ``await backend.is_available(session)``
    blocks FOREVER on A -- the whole ``*/5`` tick hangs while holding pg_advisory_xact_lock, stalling every
    later drain tick + every reconcile (they take the same lock). After: A's probe times out to
    available=False / remaining=0 (logged), so both FIFO candidates route to healthy backend B. The outer
    ``asyncio.wait_for`` here is the test's own dead-man's-switch: a still-unbounded drain would trip it.
    ``_PROBE_TIMEOUT_SEC`` is shrunk so the bounded path resolves fast.
    """
    monkeypatch.setattr(release_awaiting_cloud, "_PROBE_TIMEOUT_SEC", 0.05)
    hung = _StubBackend(id="kueue-a", rank=10, cap=5, hang_on="is_available")
    healthy = _StubBackend(id="kueue-b", rank=20, cap=5)
    _patch_backends(monkeypatch, [hung, healthy])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(2)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    # Dead-man's-switch: a still-unbounded drain would hang here and trip the outer timeout.
    result = await asyncio.wait_for(stage_cloud_window(_make_ctx(async_engine, router)), 5.0)

    assert result == {"staged": 2, "skipped": 0}
    # Both candidates landed on the HEALTHY backend; the hung one contributed 0 slots and was never selected.
    assert set((await _backend_ids_for(session, ids)).values()) == {"kueue-b"}
    assert set((await _states_for(session, ids)).values()) == {CloudJobStatus.SUBMITTED.value}
    assert hung.dispatch_calls == 0
