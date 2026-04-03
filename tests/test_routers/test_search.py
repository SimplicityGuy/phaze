"""Integration tests for unified search UI endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist


if TYPE_CHECKING:
    from datetime import date

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def create_searchable_file(
    session: AsyncSession,
    *,
    original_filename: str = "deadmau5 - Strobe.mp3",
    artist: str | None = "deadmau5",
    genre: str | None = "progressive house",
    bpm: float | None = 128.0,
    state: str = FileState.APPROVED,
) -> FileRecord:
    """Create FileRecord + FileMetadata + AnalysisResult for search testing."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{original_filename}",
        original_filename=original_filename,
        current_path=f"/music/{original_filename}",
        file_type="music",
        file_size=5_000_000,
        state=state,
    )
    session.add(file_record)
    await session.flush()

    metadata = FileMetadata(
        id=uuid.uuid4(),
        file_id=file_id,
        artist=artist,
        title=original_filename.rsplit(".", 1)[0],
        genre=genre,
    )
    session.add(metadata)
    await session.flush()

    analysis = AnalysisResult(
        id=uuid.uuid4(),
        file_id=file_id,
        bpm=bpm,
    )
    session.add(analysis)
    await session.commit()
    return file_record


async def create_searchable_tracklist(
    session: AsyncSession,
    *,
    artist: str = "deadmau5",
    event: str = "Coachella 2024",
    status: str = "approved",
    tracklist_date: date | None = None,
) -> Tracklist:
    """Create Tracklist for search testing."""
    tracklist = Tracklist(
        id=uuid.uuid4(),
        external_id=f"tl-{uuid.uuid4().hex[:8]}",
        source_url=f"https://1001tracklists.com/{uuid.uuid4().hex[:8]}",
        artist=artist,
        event=event,
        status=status,
        date=tracklist_date,
        source="1001tracklists",
    )
    session.add(tracklist)
    await session.commit()
    return tracklist


@pytest.mark.asyncio
async def test_search_page_loads(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/ returns 200 with Search heading and summary counts."""
    await create_searchable_file(session)
    await create_searchable_tracklist(session)
    response = await client.get("/search/")
    assert response.status_code == 200
    assert "Search" in response.text
    assert "files" in response.text
    assert "tracklists" in response.text


@pytest.mark.asyncio
async def test_search_with_query_returns_results(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=deadmau5 returns 200 with results table."""
    await create_searchable_file(session, original_filename="deadmau5 - Strobe.mp3", artist="deadmau5")
    response = await client.get("/search/", params={"q": "deadmau5"})
    assert response.status_code == 200
    assert "deadmau5" in response.text
    assert "<table" in response.text.lower()


@pytest.mark.asyncio
async def test_search_returns_file_and_tracklist_results(client: AsyncClient, session: AsyncSession) -> None:
    """Results contain both File and Tracklist type badges."""
    await create_searchable_file(session, original_filename="deadmau5 - Strobe.mp3", artist="deadmau5")
    await create_searchable_tracklist(session, artist="deadmau5", event="deadmau5 Coachella 2024")
    response = await client.get("/search/", params={"q": "deadmau5"})
    assert response.status_code == 200
    assert "bg-blue-100 text-blue-700" in response.text  # File badge
    assert "bg-green-100 text-green-700" in response.text  # Tracklist badge


@pytest.mark.asyncio
async def test_search_no_results_message(client: AsyncClient) -> None:
    """GET /search/?q=nonexistent returns No results found message (D-07)."""
    response = await client.get("/search/", params={"q": "xyznonexistent123"})
    assert response.status_code == 200
    assert "No results found" in response.text
    assert "xyznonexistent123" in response.text


@pytest.mark.asyncio
async def test_search_htmx_partial(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=test with HX-Request header returns partial (no base.html wrapper)."""
    await create_searchable_file(session, original_filename="test track.mp3", artist="test")
    response = await client.get("/search/", params={"q": "test"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text


@pytest.mark.asyncio
async def test_search_artist_filter(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=Strobe&artist=deadmau5 narrows results."""
    await create_searchable_file(session, original_filename="deadmau5 - Strobe.mp3", artist="deadmau5")
    await create_searchable_file(session, original_filename="Daft Punk - Strobe Remix.mp3", artist="Daft Punk")
    response = await client.get("/search/", params={"q": "Strobe", "artist": "deadmau5"})
    assert response.status_code == 200
    assert "deadmau5" in response.text


@pytest.mark.asyncio
async def test_search_bpm_filter(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=deadmau5&bpm_min=120&bpm_max=130 narrows results."""
    await create_searchable_file(session, original_filename="deadmau5 - Strobe.mp3", artist="deadmau5", bpm=128.0)
    await create_searchable_file(session, original_filename="deadmau5 - Raise Your Weapon.mp3", artist="deadmau5", bpm=140.0)
    response = await client.get("/search/", params={"q": "deadmau5", "bpm_min": "120", "bpm_max": "130"})
    assert response.status_code == 200
    assert "Strobe" in response.text


@pytest.mark.asyncio
async def test_search_file_state_filter(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=deadmau5&file_state=approved narrows results."""
    await create_searchable_file(session, original_filename="deadmau5 - Strobe.mp3", state=FileState.APPROVED)
    await create_searchable_file(session, original_filename="deadmau5 - FML.mp3", state=FileState.DISCOVERED)
    response = await client.get("/search/", params={"q": "deadmau5", "file_state": "approved"})
    assert response.status_code == 200
    assert "Strobe" in response.text


@pytest.mark.asyncio
async def test_search_pagination(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=track&page=1&page_size=25 returns paginated results."""
    for i in range(30):
        await create_searchable_file(
            session,
            original_filename=f"track {i:03d}.mp3",
            artist=f"artist {i:03d}",
        )
    response = await client.get("/search/", params={"q": "track", "page": "1", "page_size": "25"})
    assert response.status_code == 200
    assert "Showing 1-25 of 30" in response.text


@pytest.mark.asyncio
async def test_search_nav_tab_first(client: AsyncClient) -> None:
    """GET /search/ response contains Search link before Pipeline link in HTML."""
    response = await client.get("/search/")
    assert response.status_code == 200
    search_pos = response.text.index('href="/search/"')
    pipeline_pos = response.text.index('href="/pipeline/"')
    assert search_pos < pipeline_pos


@pytest.mark.asyncio
async def test_search_filter_panel_collapsed(client: AsyncClient) -> None:
    """Response contains x-data='{ showFilters: false }' indicating collapsed by default (D-04)."""
    response = await client.get("/search/")
    assert response.status_code == 200
    assert 'x-data="{ showFilters: false }"' in response.text
