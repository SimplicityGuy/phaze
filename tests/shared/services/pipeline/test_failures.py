"""Tests for `services/pipeline/failures.py` (split from test_pipeline.py, phaze-7l8jh).

get_analysis_failed_files/_count, get_analysis_stalled_count -- `services/pipeline/failures.py`.
"""

from __future__ import annotations

from tests.shared.services.pipeline._shared import *


@pytest.mark.asyncio
async def test_get_analysis_failed_count_happy_path(session: AsyncSession) -> None:
    """DERIVED count (Phase 90 D-09): counts files with an analyze-failure marker, not ``files.state``.

    Seeds a consistent corpus (legacy ``state`` AND the derived ``analysis.failed_at`` marker agree),
    then asserts the derived count equals the legacy count of 2 -- proving the reader now sources from
    ``failed_clause(Stage.ANALYZE)``. The ANALYZED file carries a completed marker (no ``failed_at``)
    and is excluded.
    """
    a, b, done = _failed_file(0), _failed_file(1), _failed_file(2)
    session.add_all([a, b, done])
    await session.flush()
    session.add_all([_failed_analysis_for(a.id), _failed_analysis_for(b.id), _completed_analysis_for(done.id)])
    await session.commit()
    assert await get_analysis_failed_count(session) == 2


@pytest.mark.asyncio
async def test_get_analysis_failed_files_returns_failed_rows(session: AsyncSession) -> None:
    """DERIVED (Phase 90 D-09): returns files with an analyze-failure marker, not ``files.state``.

    Seeds two files with the ``analysis.failed_at`` marker (the derived source ``failed_clause`` reads)
    and one non-failed file (no marker); asserts only the two marker-bearing files are returned.
    """
    a = _failed_file(0)
    b = _failed_file(1)
    c = _failed_file(2)
    session.add_all([a, b, c])
    await session.flush()
    session.add_all([_failed_analysis_for(a.id), _failed_analysis_for(b.id)])
    await session.commit()
    rows = await get_analysis_failed_files(session)
    assert {r.id for r in rows} == {a.id, b.id}


@pytest.mark.asyncio
async def test_get_analysis_failed_count_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades the count to 0 (poll-safe via _safe_count), never raising.

    Mirrors the _safe_count degrade discipline: the hot 5s /pipeline/stats poll must keep serving
    instead of 500ing when the files read fails.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("files table unavailable")

    assert await get_analysis_failed_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# STALLED bucket (Phase 44, D-02 follow-up; phaze-g84sk) -- a heartbeat-derived SUBSET of
# ANALYSIS_FAILED, replacing the removed running-age STRAGGLER bucket.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_analysis_stalled_count_counts_only_timeout_reason(session: AsyncSession) -> None:
    """Counts only ANALYSIS_FAILED files whose error_message carries the "timeout: " reason prefix.

    Mirrors report_analysis_failed's composed detail (routers/agent_analysis.py):
    `f"{body.reason}: {body.error}"` for `body.reason` in {"timeout", "crashed", "error"}. A
    stalled kill is the ONLY reason that should count here; crashed/error and a done file must not.
    """
    stalled, crashed, errored, done = _failed_file(0), _failed_file(1), _failed_file(2), _failed_file(3)
    session.add_all([stalled, crashed, errored, done])
    await session.flush()
    session.add_all(
        [
            _failed_analysis_for(stalled.id, error_message="timeout: no progress for 1800s (last stage: coarse window 12/40)"),
            _failed_analysis_for(crashed.id, error_message="crashed: essentia child exited 139"),
            _failed_analysis_for(errored.id, error_message="error: unexpected exception"),
            _completed_analysis_for(done.id),
        ]
    )
    await session.commit()
    assert await get_analysis_stalled_count(session) == 1
    # The stalled file is still counted in the broader ANALYSIS_FAILED bucket (subset, not a
    # separate live-job read) -- STALLED never exceeds FAILED.
    assert await get_analysis_failed_count(session) == 3


@pytest.mark.asyncio
async def test_get_analysis_stalled_count_zero_when_no_stalls(session: AsyncSession) -> None:
    """With only non-stall failures (or none at all) the stalled count is 0, not an error."""
    crashed = _failed_file(0)
    session.add(crashed)
    await session.flush()
    session.add(_failed_analysis_for(crashed.id, error_message="crashed: boom"))
    await session.commit()
    assert await get_analysis_stalled_count(session) == 0


@pytest.mark.asyncio
async def test_get_analysis_stalled_count_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades the count to 0 (poll-safe via _safe_count), never raising.

    Mirrors test_get_analysis_failed_count_degrades_to_zero_on_db_error -- the hot 5s
    /pipeline/stats poll must keep serving instead of 500ing when the read fails.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("files table unavailable")

    assert await get_analysis_stalled_count(_ExplodingSession()) == 0  # type: ignore[arg-type]
