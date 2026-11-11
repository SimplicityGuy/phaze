"""The ORM half of phaze-f6eeb: ``AnalysisSubwindow`` and ``AnalysisBeatGrid``.

The migration half is ``tests/integration/test_migrations/test_095_subwindow_mood_and_beat_grid.py``.
The two are independent declarations of one schema; the baseline suite's autogenerate-drift test is
what fails if they disagree.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models import AnalysisBeatGrid, AnalysisResult, AnalysisSubwindow, AnalysisWindow, FileRecord
from phaze.services.analysis_windows import iter_subwindows
from phaze.services.beat_grid import stored_beat_grid
from phaze.services.set_projection import MOOD_ORDER


_ANALYSIS_COLUMNS_BEFORE = {
    "id", "file_id", "bpm", "musical_key", "mood", "style", "dominant_style", "features", "fine_windows_analyzed",
    "fine_windows_total", "coarse_windows_analyzed", "coarse_windows_total", "coarse_work_percent", "coarse_work_done",
    "coarse_work_total", "analysis_completed_at", "failed_at", "error_message", "created_at", "updated_at",
}  # fmt: skip
_WINDOW_COLUMNS_BEFORE = {
    "id", "file_id", "tier", "window_index", "start_sec", "end_sec", "bpm", "musical_key", "mood", "style", "danceability",
    "features", "energy", "mood_scores", "created_at", "updated_at",
}  # fmt: skip


def test_existing_analysis_tables_keep_exactly_their_columns() -> None:
    """Additive only: the two existing analysis tables gain nothing and lose nothing."""
    assert set(AnalysisResult.__table__.columns.keys()) == _ANALYSIS_COLUMNS_BEFORE
    assert set(AnalysisWindow.__table__.columns.keys()) == _WINDOW_COLUMNS_BEFORE


def test_subwindow_is_keyed_by_file_coarse_window_and_slice_and_cascades_with_files() -> None:
    table = AnalysisSubwindow.__table__
    assert [column.name for column in table.primary_key.columns] == ["file_id", "window_index", "sub_index"]
    assert set(table.columns.keys()) == {"file_id", "window_index", "sub_index", "start_sec", "end_sec", "mood_scores"}
    assert table.columns["mood_scores"].nullable
    assert not any(table.columns[name].nullable for name in ("start_sec", "end_sec"))
    (foreign_key,) = table.columns["file_id"].foreign_keys
    assert (foreign_key.column.table.name, foreign_key.ondelete) == ("files", "CASCADE")
    assert "set_projection.MOOD_ORDER" in (AnalysisSubwindow.__doc__ or "")


def test_beat_grid_is_one_row_per_file_and_cascades_with_files() -> None:
    table = AnalysisBeatGrid.__table__
    assert [column.name for column in table.primary_key.columns] == ["file_id"]
    assert set(table.columns.keys()) == {"file_id", "beats", "beat_count", "gaps", "created_at", "updated_at"}
    (foreign_key,) = table.columns["file_id"].foreign_keys
    assert (foreign_key.column.table.name, foreign_key.ondelete) == ("files", "CASCADE")


@pytest.mark.asyncio
async def test_sub_window_mood_round_trips_positionally_including_a_recorded_gap(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    record = await make_file()
    slices = iter_subwindows(180.0, 196.5)
    vector = [0.01 * (i + 1) + 1e-12 for i in range(len(MOOD_ORDER))]
    session.add_all(
        [
            AnalysisSubwindow(file_id=record.id, window_index=1, sub_index=0, start_sec=slices[0][1], end_sec=slices[0][2], mood_scores=vector),
            AnalysisSubwindow(file_id=record.id, window_index=1, sub_index=1, start_sec=slices[1][1], end_sec=slices[1][2], mood_scores=None),
        ]
    )
    file_id = record.id
    await session.commit()
    session.expire_all()

    rows = (
        (await session.execute(select(AnalysisSubwindow).where(AnalysisSubwindow.file_id == file_id).order_by(AnalysisSubwindow.sub_index)))
        .scalars()
        .all()
    )
    assert [(row.start_sec, row.end_sec, row.mood_scores) for row in rows] == [(180.0, 195.0, vector), (195.0, 196.5, None)]
    # float8[]: the full float64 the writer computed, not a float32 or a text rounding of it.
    assert rows[0].mood_scores[-1] == vector[-1]


@pytest.mark.asyncio
async def test_a_time_range_selects_the_sub_windows_it_overlaps_in_sql(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    record = await make_file()
    for window_index, (start, end) in enumerate(((0.0, 180.0), (180.0, 360.0))):
        session.add_all(
            AnalysisSubwindow(file_id=record.id, window_index=window_index, sub_index=i, start_sec=s, end_sec=e, mood_scores=[0.5] * len(MOOD_ORDER))
            for i, s, e in iter_subwindows(start, end)
        )
    await session.commit()

    overlapping = (
        await session.execute(
            select(AnalysisSubwindow.window_index, AnalysisSubwindow.sub_index)
            .where(AnalysisSubwindow.file_id == record.id, AnalysisSubwindow.start_sec < 200.0, AnalysisSubwindow.end_sec > 170.0)
            .order_by(AnalysisSubwindow.start_sec)
        )
    ).all()
    assert [tuple(row) for row in overlapping] == [(0, 11), (1, 0), (1, 1)]


@pytest.mark.asyncio
async def test_beat_grid_round_trips_the_writers_float32_values_bit_for_bit_with_its_gaps(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    record = await make_file()
    # 0.3715 is not a float32 value: what reads back is exactly the writer's float32 rounding of it.
    beats = stored_beat_grid([0.3715 + i * 0.46875 for i in range(64)] + [43200.0 + i * 0.46875 for i in range(64)])
    session.add(AnalysisBeatGrid(file_id=record.id, beats=beats, beat_count=len(beats), gaps=[[30.0, 43200.0]]))
    file_id = record.id
    await session.commit()
    session.expire_all()

    grid = (await session.execute(select(AnalysisBeatGrid).where(AnalysisBeatGrid.file_id == file_id))).scalar_one()
    assert grid.beats == beats
    assert (grid.beat_count, grid.gaps) == (128, [[30.0, 43200.0]])
    assert grid.created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_a_beat_count_that_disagrees_with_the_blob_is_refused(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    record = await make_file()
    session.add(AnalysisBeatGrid(file_id=record.id, beats=[0.5, 1.0], beat_count=3))
    with pytest.raises(IntegrityError, match="ck_analysis_beat_grid_beats_match_count"):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_deleting_a_file_removes_its_sub_windows_and_beat_grid(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    record = await make_file()
    neighbour = await make_file()
    for file_id in (record.id, neighbour.id):
        session.add(AnalysisSubwindow(file_id=file_id, window_index=0, sub_index=0, start_sec=0.0, end_sec=15.0, mood_scores=None))
        session.add(AnalysisBeatGrid(file_id=file_id, beats=[0.5], beat_count=1))
    await session.commit()

    await session.execute(FileRecord.__table__.delete().where(FileRecord.id == record.id))
    await session.commit()

    async def _count(model: type, file_id: uuid.UUID) -> int:
        return len((await session.execute(select(model).where(model.file_id == file_id))).scalars().all())  # type: ignore[attr-defined]

    assert (await _count(AnalysisSubwindow, record.id), await _count(AnalysisBeatGrid, record.id)) == (0, 0)
    assert (await _count(AnalysisSubwindow, neighbour.id), await _count(AnalysisBeatGrid, neighbour.id)) == (1, 1)
