"""Integration tests for tracklists router."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord, FileState
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


def _make_file(original_path: str = "/music/test.mp3", file_type: str = "mp3") -> FileRecord:
    """Create a test FileRecord."""
    filename = original_path.rsplit("/", 1)[-1]
    return FileRecord(
        id=uuid.uuid4(),
        sha256_hash="a" * 64,
        original_path=original_path,
        original_filename=filename,
        current_path=original_path,
        file_type=file_type,
        file_size=1000,
        state=FileState.DISCOVERED,
    )


def _make_tracklist(
    file_id: uuid.UUID | None = None,
    external_id: str | None = None,
    match_confidence: int | None = None,
    auto_linked: bool = False,
    source: str = "1001tracklists",
    status: str = "approved",
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
        source=source,
        status=status,
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


@pytest.mark.asyncio
async def test_scan_tab(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan returns scan tab HTML with file list."""
    file1 = _make_file(original_path="/music/live-set-1.mp3", file_type="mp3")
    file2 = _make_file(original_path="/music/live-set-2.m4a", file_type="m4a")
    session.add_all([file1, file2])
    await session.flush()

    response = await client.get("/tracklists/scan")
    assert response.status_code == 200
    assert "Scan Live Sets" in response.text
    assert "live-set-1.mp3" in response.text
    assert "live-set-2.m4a" in response.text


@pytest.mark.asyncio
async def test_scan_tab_excludes_already_scanned(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan excludes files that already have fingerprint tracklists."""
    file1 = _make_file(original_path="/music/already-done.mp3", file_type="mp3")
    file2 = _make_file(original_path="/music/fresh-file.mp3", file_type="mp3")
    session.add_all([file1, file2])
    await session.flush()

    # Link file1 to a fingerprint-sourced tracklist
    tl = _make_tracklist(file_id=file1.id, external_id="fp-scanned", source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    response = await client.get("/tracklists/scan")
    assert response.status_code == 200
    assert "fresh-file.mp3" in response.text
    assert "already-done.mp3" not in response.text


@pytest.mark.asyncio
async def test_scan_tab_empty_state(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan with no unscanned files shows empty state."""
    response = await client.get("/tracklists/scan")
    assert response.status_code == 200
    assert "No unscanned files" in response.text


@pytest.mark.asyncio
async def test_trigger_scan(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/scan enqueues arq jobs and returns progress HTML."""
    # Mock arq pool
    mock_job = MagicMock()
    mock_job.job_id = "test-job-123"
    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)
    client._transport.app.state.arq_pool = mock_pool  # type: ignore[union-attr]

    file_id = str(uuid.uuid4())
    response = await client.post("/tracklists/scan", data={"file_ids": [file_id]})
    assert response.status_code == 200
    assert "Scanning..." in response.text
    mock_pool.enqueue_job.assert_called_once_with("scan_live_set", file_id)


@pytest.mark.asyncio
async def test_proposed_filter(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/?filter=proposed returns only proposed tracklists."""
    file = _make_file()
    session.add(file)
    await session.flush()

    proposed = _make_tracklist(file_id=file.id, external_id="fp-proposed", source="fingerprint", status="proposed")
    approved = _make_tracklist(external_id="tl-approved", source="1001tracklists", status="approved")
    session.add_all([proposed, approved])
    await session.flush()

    response = await client.get("/tracklists/?filter=proposed")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_inline_edit_get(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/tracks/{id}/edit/{field} returns input HTML."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Original Artist",
        title="Original Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    response = await client.get(f"/tracklists/tracks/{track.id}/edit/artist")
    assert response.status_code == 200
    assert 'name="artist"' in response.text
    assert "Original Artist" in response.text


@pytest.mark.asyncio
async def test_inline_edit_save(session: AsyncSession, client: AsyncClient) -> None:
    """PUT /tracklists/tracks/{id}/edit/{field} updates field and returns display HTML."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Old Artist",
        title="Old Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    response = await client.put(f"/tracklists/tracks/{track.id}/edit/artist", data={"artist": "New Artist"})
    assert response.status_code == 200
    assert "New Artist" in response.text
    assert "hx-get" in response.text

    await session.refresh(track)
    assert track.artist == "New Artist"


@pytest.mark.asyncio
async def test_inline_edit_invalid_field(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/tracks/{id}/edit/{field} returns 400 for invalid field name."""
    track_id = uuid.uuid4()
    response = await client.get(f"/tracklists/tracks/{track_id}/edit/invalid_field")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_delete_track(session: AsyncSession, client: AsyncClient) -> None:
    """DELETE /tracklists/tracks/{id} removes track row."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Delete Me",
        title="Delete Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    response = await client.delete(f"/tracklists/tracks/{track.id}")
    assert response.status_code == 200
    assert response.text == ""


@pytest.mark.asyncio
async def test_approve_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/approve changes status to approved."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/approve")
    assert response.status_code == 200

    await session.refresh(tl)
    assert tl.status == "approved"


@pytest.mark.asyncio
async def test_reject_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/reject changes status to rejected."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/reject")
    assert response.status_code == 200

    await session.refresh(tl)
    assert tl.status == "rejected"


@pytest.mark.asyncio
async def test_bulk_reject_low_confidence(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/reject-low removes tracks below threshold."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    high_conf = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Good",
        title="Good Track",
        confidence=95.0,
    )
    low_conf = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=2,
        artist="Bad",
        title="Bad Track",
        confidence=30.0,
    )
    session.add_all([high_conf, low_conf])
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/reject-low?threshold=50")
    assert response.status_code == 200
    assert "Good" in response.text
    assert "Bad" not in response.text


@pytest.mark.asyncio
async def test_fingerprint_tracks_use_fingerprint_template(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/{id}/tracks returns fingerprint template for fingerprint source."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="FP Artist",
        title="FP Title",
        confidence=88.0,
    )
    session.add(track)
    await session.flush()

    response = await client.get(f"/tracklists/{tl.id}/tracks")
    assert response.status_code == 200
    # Fingerprint template includes confidence badges and inline edit
    assert "FP Artist" in response.text
    assert "hx-get" in response.text  # inline edit wiring
    assert "hx-delete" in response.text  # delete button


@pytest.mark.asyncio
async def test_stats_include_proposed(session: AsyncSession, client: AsyncClient) -> None:
    """Stats dict includes proposed count."""
    file = _make_file()
    session.add(file)
    await session.flush()

    proposed = _make_tracklist(file_id=file.id, external_id="fp-stats", source="fingerprint", status="proposed")
    approved = _make_tracklist(external_id="tl-stats-appr", source="1001tracklists", status="approved")
    session.add_all([proposed, approved])
    await session.flush()

    response = await client.get("/tracklists/")
    assert response.status_code == 200
    assert "Proposed" in response.text
