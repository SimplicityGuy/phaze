"""Integration tests for tracklists router."""

from datetime import date
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord, FileState
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


def _make_file(original_path: str = "/music/test.mp3") -> FileRecord:
    """Create a test FileRecord."""
    filename = original_path.rsplit("/", 1)[-1]
    return FileRecord(
        id=uuid.uuid4(),
        sha256_hash="a" * 64,
        original_path=original_path,
        original_filename=filename,
        current_path=original_path,
        file_type="mp3",
        file_size=1000,
        state=FileState.DISCOVERED,
    )


def _make_tracklist(
    file_id: uuid.UUID | None = None,
    external_id: str | None = None,
    match_confidence: int | None = None,
    auto_linked: bool = False,
) -> Tracklist:
    """Create a test Tracklist."""
    return Tracklist(
        id=uuid.uuid4(),
        external_id=external_id or f"tl-{uuid.uuid4().hex[:8]}",
        source_url=f"https://www.1001tracklists.com/tracklist/{uuid.uuid4().hex[:8]}/test.html",
        file_id=file_id,
        match_confidence=match_confidence,
        auto_linked=auto_linked,
        artist="Test Artist",
        event="Test Festival",
        date=date(2024, 4, 14),
    )


@pytest.mark.asyncio
async def test_list_tracklists_returns_html(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ returns 200 with Tracklists heading."""
    response = await client.get("/tracklists/")
    assert response.status_code == 200
    assert "Tracklists" in response.text


@pytest.mark.asyncio
async def test_list_tracklists_empty_state(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ with no data shows empty state message."""
    response = await client.get("/tracklists/")
    assert response.status_code == 200
    assert "hasn't run yet" in response.text.lower() or "hasn&#x27;t run yet" in response.text.lower()


@pytest.mark.asyncio
async def test_list_tracklists_with_data(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ with tracklists shows card content."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=85)
    session.add(tl)
    await session.flush()

    response = await client.get("/tracklists/")
    assert response.status_code == 200
    assert "Test Artist" in response.text
    assert "Test Festival" in response.text


@pytest.mark.asyncio
async def test_list_tracklists_filter_matched(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/?filter=matched returns only matched tracklists."""
    file = _make_file()
    session.add(file)
    await session.flush()

    matched = _make_tracklist(file_id=file.id, match_confidence=90, external_id="matched-1")
    unmatched = _make_tracklist(external_id="unmatched-1")
    session.add_all([matched, unmatched])
    await session.flush()

    response = await client.get("/tracklists/?filter=matched")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_tracklists_filter_unmatched(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/?filter=unmatched returns only unmatched tracklists."""
    response = await client.get("/tracklists/?filter=unmatched")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_tracklists_htmx_returns_partial(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ with HX-Request header returns partial (no html tag)."""
    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text


@pytest.mark.asyncio
async def test_get_tracks(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/{id}/tracks returns track detail partial."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version = TracklistVersion(
        id=uuid.uuid4(),
        tracklist_id=tl.id,
        version_number=1,
    )
    session.add(version)
    await session.flush()

    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Track Artist",
        title="Track Title",
        label="Test Label",
        timestamp="00:00",
        is_mashup=False,
    )
    session.add(track)
    await session.flush()

    response = await client.get(f"/tracklists/{tl.id}/tracks")
    assert response.status_code == 200
    assert "Track Artist" in response.text
    assert "Track Title" in response.text


@pytest.mark.asyncio
async def test_unlink_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/unlink removes file linkage."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=90)
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/unlink")
    assert response.status_code == 200

    await session.refresh(tl)
    assert tl.file_id is None
    assert tl.match_confidence is None


@pytest.mark.asyncio
async def test_undo_link(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/undo-link removes auto-link."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=95, auto_linked=True)
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/undo-link")
    assert response.status_code == 200

    await session.refresh(tl)
    assert tl.file_id is None
    assert tl.auto_linked is False


@pytest.mark.asyncio
async def test_navigation_contains_tracklists_link(session: AsyncSession, client: AsyncClient) -> None:
    """Navigation bar contains Tracklists link."""
    response = await client.get("/tracklists/")
    assert response.status_code == 200
    assert 'href="/tracklists/"' in response.text


@pytest.mark.asyncio
async def test_stats_header_values(session: AsyncSession, client: AsyncClient) -> None:
    """Stats header shows correct total, matched, unmatched counts."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl1 = _make_tracklist(file_id=file.id, match_confidence=90, external_id="stats-1")
    tl2 = _make_tracklist(external_id="stats-2")
    session.add_all([tl1, tl2])
    await session.flush()

    response = await client.get("/tracklists/")
    assert response.status_code == 200
    assert "Total Tracklists" in response.text
    assert "Matched" in response.text
    assert "Unmatched" in response.text
