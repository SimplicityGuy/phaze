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

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.services.enqueue_router import NoActiveAgentError
from phaze.tasks.release_awaiting_cloud import stage_cloud_window
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


# --- Stub backends whose snapshot/dispatch behavior is controllable ----------------------


class _StubBackend:
    """A duck-typed ``Backend`` (kueue role) whose per-tick probe + dispatch can be made to raise.

    NOT a ``LocalBackend`` subclass, so the pure ``select_backend`` policy treats it as a cloud
    backend (``isinstance(..., LocalBackend)`` is False). ``raise_on`` selects which lifecycle call
    blows up: ``"is_available"`` / ``"in_flight_count"`` (the snapshot legs), ``"dispatch"`` (a generic
    kube/S3 raise) or ``"dispatch_noagent"`` (a fileserver-vanish ``NoActiveAgentError``). A healthy
    stub flips its candidate to PUSHING + writes a ``backend_id``-scoped ``cloud_job`` row (no commit --
    the drain owns the single post-loop commit) and returns ``True`` (a genuine stage).
    """

    def __init__(self, *, id: str, rank: int, cap: int, raise_on: str | None = None) -> None:
        self.id = id
        self.rank = rank
        self.cap = cap
        self._raise_on = raise_on
        self.dispatch_calls = 0

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature
        if self._raise_on == "is_available":
            raise RuntimeError(f"{self.id}: cluster reachability probe blew up")
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- protocol signature
        if self._raise_on == "in_flight_count":
            raise RuntimeError(f"{self.id}: in_flight_count query blew up")
        return 0

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: Any) -> bool:  # noqa: ARG002 -- protocol signature
        self.dispatch_calls += 1
        # Fail (if configured) BEFORE any mutation -- mirrors real dispatch resolving the fileserver
        # before touching state, so a raising path leaves the file untouched (AWAITING_CLOUD).
        if self._raise_on == "dispatch":
            raise RuntimeError(f"{self.id}: kube submit / S3 stage blew up")
        if self._raise_on == "dispatch_noagent":
            raise NoActiveAgentError("fileserver")
        file.state = FileState.PUSHING
        session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, backend_id=self.id, s3_key=None, status=CloudJobStatus.SUBMITTED.value))
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
    """Build a controller-shaped ctx: async_session sessionmaker + controller queue + dedup router."""
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "queue": DedupFakeQueue("controller"), "task_router": router}


def _make_file() -> FileRecord:
    """Build a fully-populated AWAITING_CLOUD FileRecord row."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.flac",
        original_filename=f"{uid.hex}.flac",
        current_path=f"/music/{uid.hex}.flac",
        file_type="flac",
        file_size=1000,
        state=FileState.AWAITING_CLOUD,
    )


async def _states_for(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    session.expire_all()
    rows = (await session.execute(select(FileRecord).where(FileRecord.id.in_(ids)))).scalars().all()
    return {r.id: r.state for r in rows}


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
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    # MUST NOT raise -- one flaky cluster cannot poison the whole drain tick.
    result = await stage_cloud_window(_make_ctx(async_engine, router))

    assert result == {"staged": 2, "skipped": 0}
    # Both candidates landed on the HEALTHY backend; the flaky one contributed nothing.
    backend_ids = await _backend_ids_for(session, ids)
    assert set(backend_ids.values()) == {"kueue-b"}
    assert set((await _states_for(session, ids)).values()) == {FileState.PUSHING}
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
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router))

    assert result == {"staged": 2, "skipped": 0}
    assert set((await _backend_ids_for(session, ids)).values()) == {"kueue-b"}
    assert flaky.dispatch_calls == 0


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
    branch counts the candidate skipped, leaves it AWAITING_CLOUD (no state mutated -- dispatch resolves
    the fileserver before any mutation), and iterates to the NEXT candidate. With 2 candidates both
    routing to the raising backend, ``dispatch`` is invoked TWICE (the loop did NOT break) and the tick
    returns cleanly without raising.
    """
    raiser = _StubBackend(id="kueue-a", rank=10, cap=5, raise_on="dispatch")
    _patch_backends(monkeypatch, [raiser])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(2)]
    session.add_all(held)
    await session.commit()
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    # MUST NOT raise -- a generic dispatch failure is a clean per-candidate hold.
    result = await stage_cloud_window(_make_ctx(async_engine, router))

    assert result == {"staged": 0, "skipped": 2}
    # The loop CONTINUED past the first raise (both candidates attempted), never broke.
    assert raiser.dispatch_calls == 2
    # Both files stay AWAITING_CLOUD and grow NO cloud_job row (the raising path mutated nothing).
    assert set((await _states_for(session, ids)).values()) == {FileState.AWAITING_CLOUD}
    assert await _backend_ids_for(session, ids) == {}


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
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router))

    assert result == {"staged": 0, "skipped": 2}
    # BREAK semantics: the fileserver-vanish path holds ALL remaining after ONE failing dispatch.
    assert vanish.dispatch_calls == 1
    assert set((await _states_for(session, ids)).values()) == {FileState.AWAITING_CLOUD}
    assert await _backend_ids_for(session, ids) == {}


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
    assert set((await _states_for(session, ids)).values()) == {FileState.AWAITING_CLOUD}
    assert await _backend_ids_for(session, ids) == {}
