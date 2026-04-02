"""Tests for unified search query service -- cross-entity FTS with pagination and facet filters."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist
from phaze.services.search_queries import SearchResult, get_summary_counts, search


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def create_test_file(
    session: AsyncSession,
    *,
    original_filename: str = "test_file.mp3",
    artist: str | None = None,
    title: str | None = None,
    album: str | None = None,
    genre: str | None = None,
    bpm: float | None = None,
    state: str = FileState.DISCOVERED,
) -> FileRecord:
    """Create a FileRecord with optional FileMetadata and AnalysisResult."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{original_filename}",
        original_filename=original_filename,
        current_path=f"/music/{original_filename}",
        file_type="music",
        file_size=1_000_000,
        state=state,
    )
    session.add(file_record)
    await session.flush()

    if artist or title or album or genre:
        metadata = FileMetadata(
            id=uuid.uuid4(),
            file_id=file_id,
            artist=artist,
            title=title,
            album=album,
            genre=genre,
        )
        session.add(metadata)
        await session.flush()

    if bpm is not None:
        analysis = AnalysisResult(
            id=uuid.uuid4(),
            file_id=file_id,
            bpm=bpm,
        )
        session.add(analysis)
        await session.flush()

    await session.commit()
    return file_record


async def create_test_tracklist(
    session: AsyncSession,
    *,
    artist: str | None = None,
    event: str | None = None,
    status: str = "approved",
    tl_date: date | None = None,
) -> Tracklist:
    """Create a Tracklist record for testing."""
    tracklist = Tracklist(
        id=uuid.uuid4(),
        external_id=f"ext-{uuid.uuid4().hex[:12]}",
        source_url=f"https://example.com/{uuid.uuid4().hex[:8]}",
        artist=artist,
        event=event,
        date=tl_date,
        status=status,
    )
    session.add(tracklist)
    await session.commit()
    return tracklist


# ---------------------------------------------------------------------------
# SearchResult dataclass
# ---------------------------------------------------------------------------


class TestSearchResult:
    def test_fields(self):
        r = SearchResult(
            id="abc",
            result_type="file",
            title="test.mp3",
            artist="DJ Test",
            genre="house",
            state="discovered",
            date="2026-01-01",
            rank=1.0,
        )
        assert r.result_type == "file"
        assert r.artist == "DJ Test"


# ---------------------------------------------------------------------------
# search() — file results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_files_matching_query(session: AsyncSession) -> None:
    """Search for 'deadmau5' returns file results with result_type='file'."""
    await create_test_file(session, original_filename="deadmau5_strobe.mp3", artist="deadmau5", title="Strobe")
    results, pagination = await search(session, "deadmau5")
    assert len(results) >= 1
    assert all(r.result_type == "file" for r in results)
    assert pagination.total >= 1


@pytest.mark.asyncio
async def test_search_returns_tracklists_matching_query(session: AsyncSession) -> None:
    """Search for 'coachella' returns tracklist results with result_type='tracklist'."""
    await create_test_tracklist(session, artist="Disclosure", event="Coachella 2026")
    results, pagination = await search(session, "coachella")
    assert len(results) >= 1
    assert all(r.result_type == "tracklist" for r in results)
    assert pagination.total >= 1


@pytest.mark.asyncio
async def test_search_returns_mixed_results(session: AsyncSession) -> None:
    """Query matching both files and tracklists returns both types."""
    await create_test_file(session, original_filename="disclosure_set.mp3", artist="Disclosure")
    await create_test_tracklist(session, artist="Disclosure", event="Ultra 2026")
    results, _pagination = await search(session, "disclosure")
    result_types = {r.result_type for r in results}
    assert "file" in result_types
    assert "tracklist" in result_types


@pytest.mark.asyncio
async def test_search_ranks_by_relevance(session: AsyncSession) -> None:
    """Results ordered by ts_rank descending."""
    await create_test_file(session, original_filename="random_track.mp3", artist="Unknown")
    await create_test_file(session, original_filename="tiesto_live.mp3", artist="tiesto", title="tiesto live set")
    results, _pagination = await search(session, "tiesto")
    assert len(results) >= 1
    # All results should have a rank > 0
    for r in results:
        assert r.rank > 0
    # Results should be in descending rank order
    for i in range(len(results) - 1):
        assert results[i].rank >= results[i + 1].rank


# ---------------------------------------------------------------------------
# search() — facet filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_artist_filter(session: AsyncSession) -> None:
    """Passing artist='deadmau5' narrows results to matching artist."""
    await create_test_file(session, original_filename="strobe.mp3", artist="deadmau5", title="Strobe")
    await create_test_file(session, original_filename="levels.mp3", artist="Avicii", title="Levels")
    results, _pagination = await search(session, "strobe", artist="deadmau5")
    # Should only return the deadmau5 file
    assert len(results) >= 1
    assert all("deadmau5" in (r.artist or "").lower() for r in results if r.result_type == "file")


@pytest.mark.asyncio
async def test_search_genre_filter(session: AsyncSession) -> None:
    """Passing genre='house' narrows results to matching genre."""
    await create_test_file(session, original_filename="house_track.mp3", artist="DJ House", genre="house", title="Deep Vibes")
    await create_test_file(session, original_filename="techno_track.mp3", artist="DJ Techno", genre="techno", title="Dark Energy")
    results, _pagination = await search(session, "dj", genre="house")
    assert len(results) >= 1
    assert all(r.genre == "house" for r in results if r.result_type == "file")


@pytest.mark.asyncio
async def test_search_bpm_filter(session: AsyncSession) -> None:
    """Passing bpm_min=120, bpm_max=130 narrows results to files with BPM in range."""
    await create_test_file(session, original_filename="fast_track.mp3", artist="DJ Fast", bpm=150.0)
    await create_test_file(session, original_filename="mid_track.mp3", artist="DJ Mid", bpm=125.0)
    results, _pagination = await search(session, "dj", bpm_min=120.0, bpm_max=130.0)
    assert len(results) >= 1
    # Only the mid-BPM track should match
    assert any("mid" in r.title.lower() for r in results)
    assert not any("fast" in r.title.lower() for r in results)


@pytest.mark.asyncio
async def test_search_file_state_filter(session: AsyncSession) -> None:
    """Passing file_state='approved' narrows to that state."""
    await create_test_file(session, original_filename="approved_track.mp3", artist="DJ App", state=FileState.APPROVED)
    await create_test_file(session, original_filename="discovered_track.mp3", artist="DJ Disc", state=FileState.DISCOVERED)
    results, _pagination = await search(session, "dj", file_state="approved")
    assert len(results) >= 1
    assert all(r.state == "approved" for r in results)


@pytest.mark.asyncio
async def test_search_date_filter(session: AsyncSession) -> None:
    """Passing date_from and date_to narrows results."""
    today = date.today()
    await create_test_tracklist(session, artist="DJ Recent", event="Recent Fest", tl_date=today)
    await create_test_tracklist(session, artist="DJ Old", event="Old Fest", tl_date=today - timedelta(days=365))
    results, _pagination = await search(session, "fest", date_from=today - timedelta(days=30), date_to=today + timedelta(days=1))
    # Should only return the recent tracklist
    tracklist_results = [r for r in results if r.result_type == "tracklist"]
    assert len(tracklist_results) >= 1
    assert all("recent" in r.title.lower() for r in tracklist_results)


# ---------------------------------------------------------------------------
# search() — pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_pagination(session: AsyncSession) -> None:
    """Results paginated correctly, Pagination object has correct total."""
    for i in range(5):
        await create_test_file(session, original_filename=f"searchable_track_{i}.mp3", artist="Searchable")
    results, pagination = await search(session, "searchable", page=2, page_size=2)
    assert len(results) == 2
    assert pagination.total == 5
    assert pagination.page == 2
    assert pagination.total_pages == 3


# ---------------------------------------------------------------------------
# search() — empty query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_empty_query_returns_empty(session: AsyncSession) -> None:
    """Empty string returns empty list."""
    await create_test_file(session, original_filename="some_track.mp3", artist="Some Artist")
    results, pagination = await search(session, "")
    assert results == []
    assert pagination.total == 0


# ---------------------------------------------------------------------------
# get_summary_counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_summary_counts(session: AsyncSession) -> None:
    """Returns dict with file_count and tracklist_count."""
    await create_test_file(session, original_filename="count1.mp3")
    await create_test_file(session, original_filename="count2.mp3")
    await create_test_tracklist(session, artist="Count DJ", event="Count Fest")

    counts = await get_summary_counts(session)
    assert counts["file_count"] == 2
    assert counts["tracklist_count"] == 1


# ---------------------------------------------------------------------------
# UNION result_type discriminator (SRCH-03)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_union_has_result_type(session: AsyncSession) -> None:
    """Every result has result_type of 'file' or 'tracklist' (SRCH-03)."""
    await create_test_file(session, original_filename="union_file.mp3", artist="Union Artist")
    await create_test_tracklist(session, artist="Union Artist", event="Union Fest")
    results, _pagination = await search(session, "union")
    assert len(results) >= 2
    for r in results:
        assert r.result_type in ("file", "tracklist")
