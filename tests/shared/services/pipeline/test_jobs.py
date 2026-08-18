"""Tests for `services/pipeline/jobs.py` (split from test_pipeline.py, phaze-7l8jh).

get_stage_busy_counts, get_live_job_keys, get_match_busy_count, get_proposal_busy_count, count_inflight_jobs -- `services/pipeline/jobs.py`.
"""

from __future__ import annotations

from tests.shared.services.pipeline._shared import *


@pytest.mark.asyncio
async def test_get_stage_busy_counts_buckets_by_function_prefix() -> None:
    """Rows are bucketed by the deterministic-key function prefix; non-stage functions are ignored.

    saq_jobs has NO function column — the key is ``<function>:<file_id>`` (Phase 35), so the SELECT
    groups by ``split_part(key, ':', 1)`` and each agent-stage function prefix maps back to its stage.
    ``generate_proposals`` / ``scan_directory`` are NOT agent stages, so they fall through and are
    absent from the returned dict.
    """

    class _FakeResult:
        def __init__(self, rows: list[tuple[str, str, int]]) -> None:
            self._rows = rows

        def all(self) -> list[tuple[str, str, int]]:
            return self._rows

    class _FakeSession:
        def __init__(self, rows: list[tuple[str, str, int]]) -> None:
            self._rows = rows

        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
            return _FakeResult(self._rows)

    rows = [
        ("extract_file_metadata", "queued", 3),
        ("extract_file_metadata", "active", 1),
        ("process_file", "active", 2),
        ("generate_proposals", "queued", 9),  # not an agent stage → ignored
        ("scan_directory", "active", 3),  # not an agent stage → ignored
    ]
    counts = await get_stage_busy_counts(_FakeSession(rows))  # type: ignore[arg-type]
    assert counts == {"metadata": 4, "analyze": 2}


@pytest.mark.asyncio
async def test_get_stage_busy_counts_degrades_on_db_error() -> None:
    """get_stage_busy_counts returns all-zeros and never raises when the saq_jobs read fails.

    A missing ``saq_jobs`` table or a DB hiccup must degrade to
    ``{"metadata":0,"analyze":0}`` (T-t7k-02) so the hot 5s /pipeline/stats poll
    keeps serving instead of 500ing. The read runs inside a SAVEPOINT (``begin_nested``); the
    exception propagates out of the nested scope and is caught by the degrade ``except``.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "saq_jobs" does not exist')

    counts = await get_stage_busy_counts(_ExplodingSession())  # type: ignore[arg-type]
    assert counts == {"metadata": 0, "analyze": 0}


@pytest.mark.asyncio
async def test_get_stage_busy_counts_degrade_does_not_poison_session(session: AsyncSession) -> None:
    """The SAVEPOINT degrade leaves the outer transaction usable (no ORM-expiring rollback).

    ``saq_jobs`` may already exist in the shared test DB (a prior real-broker integration test
    creates it via SAQ ``init_db``), so DROP it inside this test's uncommitted transaction to
    deterministically force the absent-table degrade — the only branch that exercises the SAVEPOINT
    rollback recovery. The DROP is rolled back when the session closes, so it never leaks. A
    follow-up query on the SAME session must still succeed — proving the dashboard's later ORM
    lazy-loads are not poisoned (the bug a plain ``session.rollback()`` would cause: a 500 on the
    next access).
    """
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    counts = await get_stage_busy_counts(session)
    assert counts == {"metadata": 0, "analyze": 0}
    # The outer transaction is intact after the SAVEPOINT rollback: a normal query still runs.
    follow_up = await get_stage_progress(session)
    assert follow_up["discovery"]["done"] == 0


@pytest.mark.asyncio
async def test_get_match_busy_count_buckets_by_match_prefix() -> None:
    """Returns ONLY the ``match_tracklist_to_discogs`` in-flight count; other prefixes are ignored."""
    rows = [
        ("match_tracklist_to_discogs", 6),
        ("generate_proposals", 4),  # not match → ignored
        ("process_file", 7),  # not match → ignored
    ]
    assert await get_match_busy_count(_BusySession(rows)) == 6  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_match_busy_count_zero_when_no_match_rows() -> None:
    """With no ``match_tracklist_to_discogs`` rows the in-flight count is 0 (not an error)."""
    assert await get_match_busy_count(_BusySession([("generate_proposals", 4)])) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_match_busy_count_degrades_on_db_error() -> None:
    """get_match_busy_count returns 0 and never raises when the saq_jobs read fails (T-41-03)."""

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "saq_jobs" does not exist')

    assert await get_match_busy_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_match_busy_count_degrade_does_not_poison_session(session: AsyncSession) -> None:
    """The SAVEPOINT degrade leaves the outer transaction usable (mirrors the search-busy guard)."""
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    assert await get_match_busy_count(session) == 0
    follow_up = await get_stage_progress(session)
    assert follow_up["discovery"]["done"] == 0


# ---------------------------------------------------------------------------
# get_proposal_busy_count (phaze-8qheu) — the generate_proposals in-flight gate over the
# saq_jobs table, degrade-safe. Mirrors get_match_busy_count's shape verbatim.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_proposal_busy_count_buckets_by_generate_proposals_prefix() -> None:
    """Returns ONLY the ``generate_proposals`` in-flight count; other prefixes are ignored."""
    rows = [
        ("generate_proposals", 5),
        ("match_tracklist_to_discogs", 6),  # not proposals → ignored
        ("process_file", 7),  # not proposals → ignored
    ]
    assert await get_proposal_busy_count(_BusySession(rows)) == 5  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_proposal_busy_count_zero_when_no_proposal_rows() -> None:
    """With no ``generate_proposals`` rows the in-flight count is 0 (not an error)."""
    assert await get_proposal_busy_count(_BusySession([("match_tracklist_to_discogs", 6)])) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_proposal_busy_count_degrades_on_db_error() -> None:
    """get_proposal_busy_count returns 0 and never raises when the saq_jobs read fails."""

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "saq_jobs" does not exist')

    assert await get_proposal_busy_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_proposal_busy_count_degrade_does_not_poison_session(session: AsyncSession) -> None:
    """The SAVEPOINT degrade leaves the outer transaction usable (mirrors the match-busy guard)."""
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    assert await get_proposal_busy_count(session) == 0
    follow_up = await get_stage_progress(session)
    assert follow_up["discovery"]["done"] == 0


@pytest.mark.asyncio
async def test_count_inflight_jobs_counts_queued_and_active() -> None:
    """count_inflight_jobs returns the scalar COUNT(*) of in-flight saq_jobs rows."""

    class _ScalarResult:
        def __init__(self, value: int) -> None:
            self._value = value

        def scalar(self) -> int:
            return self._value

    class _FakeSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> _ScalarResult:
            return _ScalarResult(7)

    assert await count_inflight_jobs(_FakeSession()) == 7  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_count_inflight_jobs_degrades_to_zero_on_db_error() -> None:
    """A missing saq_jobs table or DB hiccup degrades count_inflight_jobs to 0 (never raises, T-42-04)."""

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "saq_jobs" does not exist')

    assert await count_inflight_jobs(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_live_job_keys_degrades_to_empty_set_on_db_error() -> None:
    """A missing saq_jobs table or DB hiccup degrades get_live_job_keys to the empty set, never raises.

    Mirrors count_inflight_jobs's SAVEPOINT degrade discipline over the same ``_LIVE_KEYS_SQL`` read
    (recovery's live-key exclusion set, consumed by the degrade-tolerant recovery producer).
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            msg = 'relation "saq_jobs" does not exist'
            raise RuntimeError(msg)

    assert await pipeline_mod.get_live_job_keys(_ExplodingSession()) == set()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_count_inflight_jobs_degrade_does_not_poison_session(session: AsyncSession) -> None:
    """The SAVEPOINT degrade leaves the outer transaction usable (no ORM-expiring rollback)."""
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    assert await count_inflight_jobs(session) == 0
    # A follow-up query on the SAME session still succeeds (the nested rollback did not poison it).
    follow_up = await get_analysis_failed_files(session)
    assert follow_up == []
