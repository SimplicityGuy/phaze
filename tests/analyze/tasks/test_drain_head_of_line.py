"""phaze-9sqa: head-of-line starvation in the ``stage_cloud_window`` drain, and the paged fix.

THE BUG. The drain fetched exactly ``sum(free slots)`` candidates, FIFO by the immutable
``FileRecord.created_at``. That makes the fetch window a hostage of its own oldest rows: when every one
of them is unroutable -- ``select_backend`` returns ``None``, no state is written, nothing moves -- the
next tick fetches the SAME rows, and every routable file behind them is never even considered. Because a
hold is deliberately silent (it must not touch the parked row's ``updated_at`` staleness clock), the only
externally visible symptom was a steady ``staged: 0, skipped: <free slots>``.

THE PRODUCTION SHAPE, reproduced below: a cloud backend with 3 free slots, a local backend at cap, and a
run of 14 oldest candidates all at the D-04 ``cloud_submit_max_attempts`` cap. Attempt-capped files are
cloud-INELIGIBLE and can only go local, and local was full -- so all 14 were permanently unroutable, and
~7,000 awaiting rows behind them starved for >24 h while the cloud lane sat idle with free capacity.

THE FIX. The drain PAGES: after routing a page it asks whether free slots are still open and, if so,
fetches the next keyset page (``get_cloud_staging_candidates(..., after=(created_at, id))``) until the
slots are filled or the ``_MAX_CANDIDATE_SCAN`` budget is spent. Routability is NOT re-derived in SQL --
it stays in the pure ``select_backend`` policy, which is the only place that can see the per-tick backend
snapshot, the staleness gate and the attempts cap at once.

Fixed, the tick below stages routable files behind ALL 14 poisoned heads *in the same tick*, and a
sustained all-held run escalates from the silent per-candidate hold to a WARNING that names the reason.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services.backends import LocalBackend
from phaze.services.pipeline import get_cloud_staging_candidates
from phaze.tasks import release_awaiting_cloud
from phaze.tasks.release_awaiting_cloud import _note_all_held_tick, stage_cloud_window
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# The production cell: a cloud lane with 3 free slots, 14 attempt-capped heads, routable work behind them.
_FREE_CLOUD_SLOTS = 3
_POISONED_HEADS = 14
_ROUTABLE_BEHIND = 5

# AWARE by design (phaze-cz3m): ``TimestampMixin`` now declares ``timezone=True`` and ``files.created_at``
# is timestamptz, so a naive seed would be resolved against the session's zone rather than pinned. This
# comment previously recorded the opposite as intentional -- it was describing the defect 049 removed.
_BASE_TIME = datetime(2026, 7, 27, 3, 0, 0, tzinfo=UTC)


class _CloudStub:
    """A duck-typed cloud ``Backend`` (NOT a ``LocalBackend``, so the real policy treats it as cloud).

    ``dispatch`` promotes the file's existing awaiting ``cloud_job`` row to ``submitted`` in the drain's
    session WITHOUT committing (the drain owns the single post-loop commit), mirroring the real backends.
    """

    def __init__(self, *, id: str = "compute-1", rank: int = 10, cap: int = _FREE_CLOUD_SLOTS) -> None:
        self.id = id
        self.rank = rank
        self.cap = cap
        self.dispatched: list[uuid.UUID] = []

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- protocol signature
        # Every tick starts with the full cap free: the point of these tests is head-of-line blocking,
        # not window accounting (which SCHED-02's own tests cover).
        return 0

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: Any) -> bool:  # noqa: ARG002 -- protocol signature
        self.dispatched.append(file.id)
        job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
        job.status = CloudJobStatus.SUBMITTED.value
        job.backend_id = self.id
        return True

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:  # noqa: ARG002
        return None


class _FullLocalBackend(LocalBackend):
    """A real ``LocalBackend`` (so ``isinstance`` local-detection holds) pinned at its cap -- zero free slots.

    This is the other half of the production shape: attempt-capped files are cloud-ineligible and can ONLY
    route local, so a saturated local backend is what turns "spent its cloud budget" into "unroutable".
    """

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- deliberately no DB read
        return self.cap


class _Cfg:
    """The registry-derived reads the drain + the pure policy make. Cloud on; the two policy knobs."""

    cloud_enabled = True
    cloud_submit_max_attempts = 3
    # Large enough that no test file ages into a local spill by accident -- the holds under test are the
    # attempts cap, not the staleness gate.
    cloud_spill_to_local_after_seconds = 86_400


def _patch_backends(monkeypatch: pytest.MonkeyPatch, backends: list[Any]) -> None:
    """Pin the drain's ``get_settings`` + (deferred-imported) ``resolve_backends`` seams."""
    monkeypatch.setattr("phaze.tasks.release_awaiting_cloud.get_settings", lambda: _Cfg())
    monkeypatch.setattr("phaze.services.backends.resolve_backends", lambda cfg: list(backends))  # noqa: ARG005


def _make_ctx(router: DedupFakeTaskRouter) -> dict[str, Any]:
    """Controller-shaped ctx bound to the per-test connection (see tests/conftest ``_route_stats_fanout``)."""
    from phaze.database import async_session

    return {"async_session": async_session, "queue": DedupFakeQueue("controller"), "task_router": router}


def _make_file(created_at: datetime) -> FileRecord:
    """A FileRecord with an EXPLICIT ``created_at`` -- the drain's FIFO key, so the tests must own it."""
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
        created_at=created_at,
    )


async def _seed(session: AsyncSession, files: list[FileRecord], *, attempts: int) -> list[uuid.UUID]:
    """Persist files + their held ``cloud_job(status='awaiting')`` sidecars at the given attempt count."""
    session.add_all(files)
    await session.commit()
    for f in files:
        session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, status=CloudJobStatus.AWAITING.value, attempts=attempts))
    await session.commit()
    return [f.id for f in files]


async def _statuses(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    session.expire_all()
    rows = (await session.execute(select(CloudJob.file_id, CloudJob.status).where(CloudJob.file_id.in_(ids)))).all()
    return dict(rows)


async def _seed_poisoned_lane(session: AsyncSession, *, routable: int = _ROUTABLE_BEHIND) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Seed ``_POISONED_HEADS`` attempt-capped OLDEST files, then ``routable`` fresh ones behind them."""
    poisoned = [_make_file(_BASE_TIME + timedelta(seconds=i)) for i in range(_POISONED_HEADS)]
    poisoned_ids = await _seed(session, poisoned, attempts=_Cfg.cloud_submit_max_attempts)
    behind = [_make_file(_BASE_TIME + timedelta(hours=1, seconds=i)) for i in range(routable)]
    behind_ids = await _seed(session, behind, attempts=0)
    return poisoned_ids, behind_ids


# --- the query-level primitive: a keyset cursor that can page past the poisoned prefix ------


@pytest.mark.asyncio
async def test_keyset_cursor_pages_past_the_head_of_line_run(session: AsyncSession) -> None:
    """``after=(created_at, id)`` returns the rows AFTER the cursor -- the primitive the drain walks on.

    Without it the drain can only ever ask for "the oldest N", which is exactly the query shape that let
    N unroutable rows hide the entire rest of the queue.
    """
    poisoned_ids, behind_ids = await _seed_poisoned_lane(session)

    first = await get_cloud_staging_candidates(session, _FREE_CLOUD_SLOTS)
    assert [f.id for f, _ in first] == poisoned_ids[:_FREE_CLOUD_SLOTS]

    # Page forward from the last row of the first page: the poisoned tail, then the routable files.
    last = first[-1][0]
    rest = await get_cloud_staging_candidates(session, 100, after=(last.created_at, last.id))
    assert [f.id for f, _ in rest] == poisoned_ids[_FREE_CLOUD_SLOTS:] + behind_ids


@pytest.mark.asyncio
async def test_keyset_cursor_is_exact_across_a_created_at_tie(session: AsyncSession) -> None:
    """A whole batch sharing one ``created_at`` still pages exactly once each -- no skips, no repeats.

    ``created_at`` is NOT unique: a scan batch inserts many rows in one transaction and
    ``server_default=func.now()`` is transaction time, so ties are the norm, not an edge case. The
    composite ``(created_at, id)`` sort + row-value cursor is what makes the walk exact over them; a
    cursor on ``created_at`` alone would either re-serve the whole tied block forever or jump past it.
    """
    tied = [_make_file(_BASE_TIME) for _ in range(9)]
    await _seed(session, tied, attempts=0)

    seen: list[uuid.UUID] = []
    cursor: tuple[datetime, uuid.UUID] | None = None
    for _ in range(3):
        page = await get_cloud_staging_candidates(session, 3, after=cursor)
        seen.extend(f.id for f, _ in page)
        cursor = (page[-1][0].created_at, page[-1][0].id)
    assert len(seen) == len(set(seen)) == 9
    assert set(seen) == {f.id for f in tied}
    # And the walk is genuinely exhausted -- no fourth page.
    assert await get_cloud_staging_candidates(session, 3, after=cursor) == []


# --- the regression: routable work behind 14 poisoned heads drains ------------------------


@pytest.mark.asyncio
async def test_poisoned_heads_do_not_starve_routable_files_behind_them(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bead's shape: 3 free cloud slots, 14 attempt-capped heads, local full -> the tick STILL stages 3.

    Pre-fix this tick returned ``{"staged": 0, "skipped": 3}`` -- it fetched exactly the 3 oldest rows,
    all poisoned, held them, and committed nothing, forever. Post-fix it walks past all 14 and fills every
    free slot from the routable files behind them, in ONE tick.
    """
    cloud = _CloudStub()
    _patch_backends(monkeypatch, [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    poisoned_ids, behind_ids = await _seed_poisoned_lane(session)

    result = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))

    # Every free slot filled, from the OLDEST routable files (FIFO is preserved among routable work).
    assert result["staged"] == _FREE_CLOUD_SLOTS
    assert cloud.dispatched == behind_ids[:_FREE_CLOUD_SLOTS]
    statuses = await _statuses(session, poisoned_ids + behind_ids)
    assert [statuses[i] for i in behind_ids[:_FREE_CLOUD_SLOTS]] == [CloudJobStatus.SUBMITTED.value] * _FREE_CLOUD_SLOTS
    # The poisoned heads are untouched -- still awaiting, still first in line for the local safety net.
    assert {statuses[i] for i in poisoned_ids} == {CloudJobStatus.AWAITING.value}
    # Held rows are reported: 14 poisoned + the 2 routable files that found no free slot.
    assert result["skipped"] == _POISONED_HEADS + (_ROUTABLE_BEHIND - _FREE_CLOUD_SLOTS)


@pytest.mark.asyncio
async def test_repeated_ticks_drain_the_whole_backlog_behind_the_poisoned_heads(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successive ticks keep draining: 5 routable files behind 14 poisoned heads are gone in 2 ticks.

    Drainage, not just a single lucky tick -- the pre-fix lane made NO progress no matter how many ticks
    ran, which is why it stayed stuck for >24 h.
    """
    cloud = _CloudStub()
    _patch_backends(monkeypatch, [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    poisoned_ids, behind_ids = await _seed_poisoned_lane(session)

    first = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))
    second = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))

    assert first["staged"] + second["staged"] == _ROUTABLE_BEHIND
    assert cloud.dispatched == behind_ids
    statuses = await _statuses(session, poisoned_ids + behind_ids)
    assert {statuses[i] for i in behind_ids} == {CloudJobStatus.SUBMITTED.value}
    assert {statuses[i] for i in poisoned_ids} == {CloudJobStatus.AWAITING.value}


@pytest.mark.asyncio
async def test_walk_ends_cleanly_when_the_cursor_runs_off_the_end_of_the_queue(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final page that is exactly full still terminates: the follow-up fetch comes back empty, and stops.

    The "short page means the end" check cannot catch this case -- 15 rows walked in pages of 3 ends on a
    FULL page with free slots still open, so the drain asks for one more and must handle the empty answer
    rather than spinning.
    """
    cloud = _CloudStub()
    _patch_backends(monkeypatch, [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])
    monkeypatch.setattr(release_awaiting_cloud, "_MIN_CANDIDATE_PAGE", _FREE_CLOUD_SLOTS)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    # 14 poisoned + 1 routable = 15 rows = exactly five pages of 3, the last of them full.
    poisoned_ids, behind_ids = await _seed_poisoned_lane(session, routable=1)

    result = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))

    assert result == {"staged": 1, "skipped": _POISONED_HEADS}
    assert cloud.dispatched == behind_ids
    assert set((await _statuses(session, poisoned_ids)).values()) == {CloudJobStatus.AWAITING.value}


@pytest.mark.asyncio
async def test_scan_budget_bounds_a_pathologically_long_poisoned_prefix(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The walk is BOUNDED: with the scan budget shrunk below the poisoned run, the tick stops at the budget.

    Locks held, per-candidate attempt lookups and advisory-lock hold time all scale with the walk, so it
    cannot be unbounded. A lane whose unroutable prefix outruns the budget still starves -- but it now says
    so every tick (see the WARNING tests below) instead of silently.
    """
    cloud = _CloudStub()
    _patch_backends(monkeypatch, [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])
    monkeypatch.setattr(release_awaiting_cloud, "_MAX_CANDIDATE_SCAN", 6)
    monkeypatch.setattr(release_awaiting_cloud, "_MIN_CANDIDATE_PAGE", 3)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    _, behind_ids = await _seed_poisoned_lane(session)

    result = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))

    # 3 (first page) + 3 (one more page, capped by the budget) poisoned rows examined, none routable.
    assert result == {"staged": 0, "skipped": 6}
    assert cloud.dispatched == []
    assert set((await _statuses(session, behind_ids)).values()) == {CloudJobStatus.AWAITING.value}


# --- observability: a sustained all-held run escalates to WARNING --------------------------


@pytest.mark.asyncio
async def test_repeated_all_held_ticks_warn_and_name_the_hold_reason(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ticks that hold 100% of what they scan stay quiet twice, then WARN naming ``cloud_attempts_exhausted``.

    The silent per-candidate hold is what hid this bug for a day: ``staged: 0, skipped: 3`` reads exactly
    like a healthy idle lane. The warning is the signal that distinguishes them, and the reason label is
    what makes it actionable -- ``cloud_attempts_exhausted`` is a PERMANENT hold (an attempt counter only
    grows), so it means operator action, not patience.
    """
    monkeypatch.setattr(release_awaiting_cloud, "_consecutive_all_held_ticks", 0)
    cloud = _CloudStub()
    _patch_backends(monkeypatch, [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    # EVERY candidate poisoned -- nothing this lane can route at all.
    await _seed_poisoned_lane(session, routable=0)

    ctx = _make_ctx(DedupFakeTaskRouter())
    with caplog.at_level("WARNING", logger="phaze.tasks.release_awaiting_cloud"):
        for _ in range(release_awaiting_cloud._ALL_HELD_WARN_AFTER_TICKS - 1):
            assert await stage_cloud_window(ctx) == {"staged": 0, "skipped": _POISONED_HEADS}
        # One transient all-held tick is unremarkable; only a sustained run is a stall.
        assert caplog.records == []
        assert await stage_cloud_window(ctx) == {"staged": 0, "skipped": _POISONED_HEADS}

    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert "the cloud lane is not draining" in caplog.text
    assert "cloud_attempts_exhausted" in caplog.text


@pytest.mark.asyncio
async def test_a_tick_that_stages_anything_resets_the_all_held_streak(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tick that stages even one file resets the streak -- a draining lane never trips the alarm.

    This is the false-positive guard: the drain now WALKS past held rows, so a healthy lane routinely
    reports a large ``skipped`` alongside its stages. Only a tick that moves NOTHING counts as all-held.
    """
    monkeypatch.setattr(release_awaiting_cloud, "_consecutive_all_held_ticks", 0)
    cloud = _CloudStub()
    _patch_backends(monkeypatch, [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await _seed_poisoned_lane(session, routable=_FREE_CLOUD_SLOTS * 4)

    ctx = _make_ctx(DedupFakeTaskRouter())
    with caplog.at_level("WARNING", logger="phaze.tasks.release_awaiting_cloud"):
        for _ in range(release_awaiting_cloud._ALL_HELD_WARN_AFTER_TICKS + 1):
            result = await stage_cloud_window(ctx)
            assert result["staged"] == _FREE_CLOUD_SLOTS
            # Held rows every tick (the 14 poisoned heads) -- but the lane is demonstrably draining.
            assert result["skipped"] >= _POISONED_HEADS

    assert caplog.records == []


def test_all_held_streak_counts_consecutively_and_resets_on_progress() -> None:
    """``_note_all_held_tick`` is a plain consecutive counter: it accumulates on True, zeroes on False."""
    release_awaiting_cloud._consecutive_all_held_ticks = 0
    try:
        assert [_note_all_held_tick(True) for _ in range(3)] == [1, 2, 3]
        assert _note_all_held_tick(False) == 0
        assert _note_all_held_tick(True) == 1
    finally:
        release_awaiting_cloud._consecutive_all_held_ticks = 0
