"""Tests for `services/pipeline/pending.py` (split from test_pipeline.py, phaze-7l8jh).

get_discovered_files_with_duration, get_metadata_activity_summary, get_pending_files_page -- `services/pipeline/pending.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.services.pipeline._shared import (
    UTC,
    FileMetadata,
    FileRecord,
    _file,
    _make_pipeline_file,
    _metadata_for,
    _NullSavepoint,
    datetime,
    get_discovered_files_with_duration,
    get_metadata_activity_summary,
    get_metadata_pending_files,
    pytest,
    timedelta,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_get_metadata_activity_summary_reports_recent_successes(session: AsyncSession) -> None:
    recent = FileRecord(
        agent_id="test-fileserver",
        sha256_hash="a" * 64,
        original_path="/test/music/recent.mp3",
        original_filename="recent.mp3",
        current_path="/test/music/recent.mp3",
        file_type="mp3",
        file_size=1,
    )
    session.add(recent)
    await session.flush()
    completed_at = datetime.now(UTC) - timedelta(hours=2)
    session.add(FileMetadata(file_id=recent.id, failed_at=None, updated_at=completed_at))
    await session.flush()

    summary = await get_metadata_activity_summary(session)

    assert summary.unique_files_24h == 1
    assert summary.latest_successful_at == completed_at
    assert summary.available is True


@pytest.mark.asyncio
async def test_get_metadata_pending_files_returns_only_music_video(session: AsyncSession) -> None:
    """Metadata pending = every music/video file (any state); a non-music file_type is excluded."""
    music = _make_pipeline_file(file_type="mp3")
    other = _make_pipeline_file(file_type="txt")
    session.add_all([music, other])
    await session.flush()

    result = await get_metadata_pending_files(session)
    ids = {f.id for f in result}
    assert music.id in ids
    assert other.id not in ids


@pytest.mark.asyncio
async def test_get_discovered_files_with_duration_joins_duration(session: AsyncSession) -> None:
    """Each DISCOVERED file is paired with its joined FileMetadata.duration."""
    f = _file(0)
    session.add(f)
    await session.flush()
    session.add(_metadata_for(f.id, 6000.0))
    await session.commit()

    rows = await get_discovered_files_with_duration(session)

    assert len(rows) == 1
    record, duration = rows[0]
    assert record.id == f.id
    assert duration == 6000.0


@pytest.mark.asyncio
async def test_get_discovered_files_with_duration_outerjoin_null(session: AsyncSession) -> None:
    """A DISCOVERED file with no metadata row still appears, with duration None (LEFT JOIN)."""
    f = _file(1)
    session.add(f)
    await session.commit()

    rows = await get_discovered_files_with_duration(session)

    assert len(rows) == 1
    record, duration = rows[0]
    assert record.id == f.id
    assert duration is None


@pytest.mark.asyncio
async def test_get_pending_files_page_degrades_to_empty_page_on_db_error() -> None:
    """A DB error on the pending-set read degrades to an empty page -- never 500s the enrich workspace."""
    from phaze.enums.stage import Stage
    from phaze.services.pipeline import get_pending_files_page

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            msg = 'relation "metadata" does not exist'
            raise RuntimeError(msg)

    page = await get_pending_files_page(_ExplodingSession(), Stage.METADATA)  # type: ignore[arg-type]

    assert page.rows == []
    assert page.has_next is False
    assert page.page == 1
