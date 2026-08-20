"""phaze-1i0h6.9: the drain's per-tick QUERY BUDGET and TRANSACTION BOUNDARY, pinned as assertions.

Repowise flags ``stage_cloud_window`` for "database I/O in a loop" and, read as an instruction, that
finding says to bulk the per-candidate work away. It is wrong here, and this module is the evidence:
the loop's round trips are two DELIBERATE shapes, and both are already minimal.

* **The keyset candidate pages** are the phaze-9sqa head-of-line starvation guard. A healthy tick issues
  exactly ONE candidate query -- the N>1 case exists only when a page leaves free slots unfilled, i.e.
  precisely when the alternative is the >24 h production stall that bead came from. Collapsing the walk
  into one query is not an optimization; it is the defect.
* **The backend probes** (``is_available`` + ``in_flight_count``) are once per backend per TICK, never
  per candidate -- the SCHED-02 snapshot IS the tick's atomicity boundary. These tests pin that: the
  probe counts are identical for a 3-row healthy page and a 19-row starvation walk.

What remains is ONE genuine per-candidate query, ``_cloud_budget_for`` -- already a single round trip
carrying both halves of the D-04 budget. The bead permits bulking it only if per-row routing, fairness,
locks and persisted outcomes are proven identical; these counts are the "before" any such change must
be measured against, and the transaction-boundary assertions are what it must not disturb.

The shapes are imported from ``test_drain_head_of_line`` on purpose: the numbers below describe the
SAME production cell that regression pins (3 free cloud slots, a full local backend, 14 attempt-capped
heads), not a synthetic re-creation of it that could drift away from it.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.tasks.release_awaiting_cloud import stage_cloud_window
from tests._queue_fakes import DedupFakeTaskRouter, seed_active_agent
from tests.analyze.tasks.test_drain_head_of_line import (
    _BASE_TIME,
    _FREE_CLOUD_SLOTS,
    _POISONED_HEADS,
    _ROUTABLE_BEHIND,
    _CloudStub,
    _FullLocalBackend,
    _make_ctx,
    _make_file,
    _patch_backends,
    _seed,
    _seed_poisoned_lane,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from phaze.models.file import FileRecord


# The keyset walk over the 14-poisoned-head cell: page 1 is the free-slot count (3), page 2 is the
# ``_MIN_CANDIDATE_PAGE`` floor (20) and comes back short with the 11 remaining poisoned heads plus the
# 5 routable files behind them. 3 + 16 = 19 rows examined, then the filled window ends the walk.
_STARVATION_ROWS_SCANNED = 19
_STARVATION_CANDIDATE_PAGES = 2


class _ProbeCountingCloud(_CloudStub):
    """``_CloudStub`` that counts its two snapshot probes, to prove they are per-TICK not per-candidate."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.availability_probes = 0
        self.in_flight_probes = 0

    async def is_available(self, session: AsyncSession) -> bool:
        self.availability_probes += 1
        return await super().is_available(session)

    async def in_flight_count(self, session: AsyncSession) -> int:
        self.in_flight_probes += 1
        return await super().in_flight_count(session)


class _ProbeCountingFullLocal(_FullLocalBackend):
    """The saturated local half of the production cell, with the same two probe counters."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.availability_probes = 0
        self.in_flight_probes = 0

    async def is_available(self, session: AsyncSession) -> bool:
        self.availability_probes += 1
        return await super().is_available(session)

    async def in_flight_count(self, session: AsyncSession) -> int:
        self.in_flight_probes += 1
        return await super().in_flight_count(session)


def _classify(statement: str) -> str:
    """Bin one emitted statement into the drain's query vocabulary.

    Ordered by specificity: the candidate page and the budget lookup both touch ``cloud_job``, and only
    the candidate page carries ``FOR UPDATE ... SKIP LOCKED`` (D-06), so that test must come first.
    """
    low = " ".join(statement.lower().split())
    if "pg_advisory_xact_lock" in low:
        return "advisory_lock"
    if low.startswith(("savepoint", "release savepoint", "rollback to savepoint")):
        return "savepoint"
    if "route_control" in low:
        return "route_control"
    if "skip locked" in low:
        return "candidate_page"
    if "cloud_budget" in low:
        return "budget_lookup"
    if "from agents" in low:
        return "agent_lookup"
    return f"other: {low[:80]}"


async def _run_recorded_tick(
    async_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    backends: list[Any],
) -> tuple[dict[str, int], Counter[str], list[str]]:
    """Run ONE ``stage_cloud_window`` tick, returning ``(result, query counts, ordered event log)``.

    Two recorders, because the two questions are different. ``before_cursor_execute`` on the engine
    answers "how many round trips, of which kind"; patching ``AsyncSession.commit``/``rollback`` answers
    "where are the transaction boundaries" -- the connection-level COMMIT never fires under the test
    harness's ``join_transaction_mode="create_savepoint"``, so the logical call is the only honest
    signal. Both are installed for the duration of the tick ONLY, so the test's own seeding is excluded.

    The event log interleaves both plus a ``dispatch`` marker, which is what makes the ordering claims
    (lock before the first fetch, commit after the last dispatch, exactly one of each) checkable.
    """
    events: list[str] = []
    counts: Counter[str] = Counter()

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        kind = _classify(statement)
        counts[kind] += 1
        events.append(kind)

    real_commit, real_rollback = AsyncSession.commit, AsyncSession.rollback

    async def _commit(self: AsyncSession) -> None:
        events.append("commit")
        await real_commit(self)

    async def _rollback(self: AsyncSession) -> None:
        events.append("rollback")
        await real_rollback(self)

    for backend in backends:
        real_dispatch = backend.dispatch

        def _dispatch(file: FileRecord, session: AsyncSession, task_router: Any, _real: Any = real_dispatch) -> Any:
            events.append("dispatch")
            return _real(file, session, task_router)

        backend.dispatch = _dispatch  # type: ignore[method-assign]

    monkeypatch.setattr(AsyncSession, "commit", _commit)
    monkeypatch.setattr(AsyncSession, "rollback", _rollback)
    event.listen(async_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        result = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", _capture)
    return result, counts, events


def _assert_one_transaction(events: list[str]) -> None:
    """The load-bearing boundary claims, asserted off the ordered log (SCHED-02 / Landmine L1).

    ONE advisory lock, taken before any candidate row is fetched or locked; ONE commit, after the LAST
    dispatch (so no dispatch ever commits mid-loop -- that would release the lock and the row locks and
    re-open the over-stage class); and no rollback at all on a healthy tick.
    """
    assert events.count("advisory_lock") == 1, events
    assert events.count("commit") == 1, events
    assert events.count("rollback") == 0, events
    assert events.index("advisory_lock") < events.index("candidate_page"), events
    assert "dispatch" in events, events
    last_dispatch = len(events) - 1 - events[::-1].index("dispatch")
    assert last_dispatch < events.index("commit"), events


@pytest.mark.asyncio
async def test_healthy_full_capacity_page_is_one_candidate_query_and_one_transaction(
    session: AsyncSession,
    async_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy tick that fills its window: ONE candidate query, one budget lookup per row, one commit.

    This is the shape the "I/O in a loop" finding would be measured against, and it is already the
    single-query path phaze-9sqa promised to preserve: because the first page fills every free slot,
    ``_next_candidate_page`` stops without issuing a second fetch. The only per-candidate round trip is
    ``_cloud_budget_for`` -- exactly one per row routed, never more.
    """
    cloud = _ProbeCountingCloud()
    local = _ProbeCountingFullLocal(id="local", rank=99, cap=1)
    _patch_backends(monkeypatch, [cloud, local])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    # A queue whose head is routable and deeper than the window: the window, not the queue, is the bound.
    await _seed(session, [_make_file(_BASE_TIME) for _ in range(_FREE_CLOUD_SLOTS * 3)], attempts=0)

    result, counts, events = await _run_recorded_tick(async_engine, monkeypatch, [cloud, local])

    assert result == {"staged": _FREE_CLOUD_SLOTS, "skipped": 0}
    # THE MEASUREMENT. One page fetch, one budget lookup per candidate examined, one lock, one commit.
    assert counts["candidate_page"] == 1
    assert counts["budget_lookup"] == _FREE_CLOUD_SLOTS
    assert counts["advisory_lock"] == 1
    _assert_one_transaction(events)
    # Probes are per-TICK, per backend -- never re-probed inside the candidate loop (SCHED-02 snapshot).
    assert [b.availability_probes for b in (cloud, local)] == [1, 1]
    assert [b.in_flight_probes for b in (cloud, local)] == [1, 1]


@pytest.mark.asyncio
async def test_poisoned_head_walk_pages_twice_and_still_runs_in_one_transaction(
    session: AsyncSession,
    async_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 14-poisoned-head cell: TWO candidate queries total, and the probe count does not move.

    The extra query is the whole point -- it is what steps the drain past the unroutable prefix in the
    same tick instead of re-reading it every 5 min forever. Note what does NOT scale with the walk: the
    backend probes stay at one apiece even though the tick examined 19 rows across two pages, because
    the snapshot is taken once and the loop only reads it.
    """
    cloud = _ProbeCountingCloud()
    local = _ProbeCountingFullLocal(id="local", rank=99, cap=1)
    _patch_backends(monkeypatch, [cloud, local])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await _seed_poisoned_lane(session)

    result, counts, events = await _run_recorded_tick(async_engine, monkeypatch, [cloud, local])

    assert result == {"staged": _FREE_CLOUD_SLOTS, "skipped": _POISONED_HEADS + (_ROUTABLE_BEHIND - _FREE_CLOUD_SLOTS)}
    # THE MEASUREMENT. Page 1 = the 3 free slots; page 2 = the _MIN_CANDIDATE_PAGE floor, returned short.
    assert counts["candidate_page"] == _STARVATION_CANDIDATE_PAGES
    assert counts["budget_lookup"] == _STARVATION_ROWS_SCANNED
    assert counts["advisory_lock"] == 1
    _assert_one_transaction(events)
    # Unchanged from the healthy page: the walk costs candidate pages + budget lookups, NOT probes.
    assert [b.availability_probes for b in (cloud, local)] == [1, 1]
    assert [b.in_flight_probes for b in (cloud, local)] == [1, 1]
