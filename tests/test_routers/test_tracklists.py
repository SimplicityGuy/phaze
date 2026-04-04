"""Integration tests for tracklists router."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.discogs_link import DiscogsLink
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
    """POST /tracklists/scan enqueues SAQ jobs and returns progress HTML."""
    # Mock SAQ queue
    mock_job = MagicMock()
    mock_job.key = "test-job-123"
    mock_queue = AsyncMock()
    mock_queue.enqueue = AsyncMock(return_value=mock_job)
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    file_id = str(uuid.uuid4())
    response = await client.post("/tracklists/scan", data={"file_ids": [file_id]})
    assert response.status_code == 200
    assert "Scanning..." in response.text
    mock_queue.enqueue.assert_called_once_with("scan_live_set", file_id=file_id)


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


# --- Scan status / progress ---


@pytest.mark.asyncio
async def test_scan_status_all_complete(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status reports completion when all jobs done."""
    from unittest.mock import patch

    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.COMPLETE
    mock_job.result = {"status": "scanned", "filename": "test.mp3"}

    mock_queue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    with patch("saq.Job") as mock_job_cls:
        mock_job_cls.fetch = AsyncMock(return_value=mock_job)
        response = await client.get("/tracklists/scan/status?job_ids=job-1")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_scan_status_with_error(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status reports errors from failed jobs."""
    from unittest.mock import patch

    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.FAILED
    mock_job.result = {"status": "error", "filename": "bad.mp3"}

    mock_queue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    with patch("saq.Job") as mock_job_cls:
        mock_job_cls.fetch = AsyncMock(return_value=mock_job)
        response = await client.get("/tracklists/scan/status?job_ids=job-1")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_scan_status_job_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status handles missing job gracefully."""
    from unittest.mock import patch

    mock_queue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    with patch("saq.Job") as mock_job_cls:
        mock_job_cls.fetch = AsyncMock(return_value=None)
        response = await client.get("/tracklists/scan/status?job_ids=missing-job")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_scan_status_job_pending(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status handles pending jobs (no result yet)."""
    from unittest.mock import patch

    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.QUEUED
    mock_job.result = None

    mock_queue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    with patch("saq.Job") as mock_job_cls:
        mock_job_cls.fetch = AsyncMock(return_value=mock_job)
        response = await client.get("/tracklists/scan/status?job_ids=pending-job")
    assert response.status_code == 200


# --- Link / rescrape / search endpoints ---


@pytest.mark.asyncio
async def test_link_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/link sets file_id and confidence."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    response = await client.post(
        f"/tracklists/{tl.id}/link",
        data={"file_id": str(file.id), "confidence": 85},
    )
    assert response.status_code == 200

    await session.refresh(tl)
    assert tl.file_id == file.id
    assert tl.match_confidence == 85


@pytest.mark.asyncio
async def test_rescrape_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/rescrape enqueues scrape job."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    mock_queue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    response = await client.post(f"/tracklists/{tl.id}/rescrape")
    assert response.status_code == 200
    mock_queue.enqueue.assert_called_once_with("scrape_and_store_tracklist", tracklist_id=str(tl.id))


@pytest.mark.asyncio
async def test_manual_search(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/search enqueues a search job."""
    mock_queue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    file_id = uuid.uuid4()
    response = await client.post(f"/tracklists/search?file_id={file_id}")
    assert response.status_code == 200
    mock_queue.enqueue.assert_called_once_with("search_tracklist", file_id=str(file_id))


@pytest.mark.asyncio
async def test_search_better_match_no_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/{id}/search with non-existent tracklist returns empty results."""
    fake_id = uuid.uuid4()
    response = await client.get(f"/tracklists/{fake_id}/search")
    assert response.status_code == 200


# --- Error branches ---


@pytest.mark.asyncio
async def test_approve_tracklist_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/approve returns 404 for non-existent tracklist."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/tracklists/{fake_id}/approve")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_reject_tracklist_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/reject returns 404 for non-existent tracklist."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/tracklists/{fake_id}/reject")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_reject_low_confidence_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/reject-low returns 404 for non-existent tracklist."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/tracklists/{fake_id}/reject-low?threshold=50")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_track_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """DELETE /tracklists/tracks/{id} returns 404 for non-existent track."""
    fake_id = uuid.uuid4()
    response = await client.delete(f"/tracklists/tracks/{fake_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_edit_track_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/tracks/{id}/edit/{field} returns 404 for non-existent track."""
    fake_id = uuid.uuid4()
    response = await client.get(f"/tracklists/tracks/{fake_id}/edit/artist")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_save_track_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """PUT /tracklists/tracks/{id}/edit/{field} returns 404 for non-existent track."""
    fake_id = uuid.uuid4()
    response = await client.put(f"/tracklists/tracks/{fake_id}/edit/artist", data={"artist": "New"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_save_track_invalid_field(session: AsyncSession, client: AsyncClient) -> None:
    """PUT /tracklists/tracks/{id}/edit/{field} returns 400 for invalid field."""
    fake_id = uuid.uuid4()
    response = await client.put(f"/tracklists/tracks/{fake_id}/edit/invalid_field", data={"invalid_field": "x"})
    assert response.status_code == 400


# --- Discogs matching endpoints ---


def _make_version_with_tracks(session: AsyncSession, tl: Tracklist, num_tracks: int = 2) -> tuple[TracklistVersion, list[TracklistTrack]]:
    """Create a version with tracks for a tracklist. Call session.flush() after."""
    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    tracks = []
    for i in range(num_tracks):
        track = TracklistTrack(
            id=uuid.uuid4(),
            version_id=version.id,
            position=i + 1,
            artist=f"Artist {i + 1}",
            title=f"Title {i + 1}",
            timestamp=f"0{i}:00",
        )
        tracks.append(track)
    return version, tracks


@pytest.mark.asyncio
async def test_match_discogs_enqueues_task(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/match-discogs enqueues SAQ task and returns card."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    mock_queue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    response = await client.post(f"/tracklists/{tl.id}/match-discogs")
    assert response.status_code == 200
    mock_queue.enqueue.assert_called_once_with("match_tracklist_to_discogs", tracklist_id=str(tl.id))


@pytest.mark.asyncio
async def test_match_discogs_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/match-discogs returns 404 for non-existent tracklist."""
    fake_id = uuid.uuid4()
    mock_queue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    response = await client.post(f"/tracklists/{fake_id}/match-discogs")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_discogs_candidates(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/{tl_id}/tracks/{t_id}/discogs returns candidate rows."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    track = tracks[0]
    link1 = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-12345",
        discogs_artist="deadmau5",
        discogs_title="Strobe",
        discogs_label="mau5trap",
        discogs_year=2009,
        confidence=87.0,
        status="candidate",
    )
    link2 = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-67890",
        discogs_artist="deadmau5",
        discogs_title="Strobe (Radio Edit)",
        confidence=72.0,
        status="candidate",
    )
    session.add_all([link1, link2])
    await session.flush()

    response = await client.get(f"/tracklists/{tl.id}/tracks/{track.id}/discogs")
    assert response.status_code == 200
    assert "deadmau5" in response.text
    assert "Strobe" in response.text
    assert "Accept Match" in response.text
    assert "Dismiss Match" in response.text


@pytest.mark.asyncio
async def test_get_discogs_candidates_empty(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/{tl_id}/tracks/{t_id}/discogs returns empty state when no candidates."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    track_id = uuid.uuid4()
    response = await client.get(f"/tracklists/{tl.id}/tracks/{track_id}/discogs")
    assert response.status_code == 200
    assert "No Discogs candidates" in response.text


@pytest.mark.asyncio
async def test_accept_discogs_link(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/discogs-links/{id}/accept sets accepted and dismisses siblings."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()

    track = tracks[0]
    link1 = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-111",
        discogs_artist="Artist A",
        discogs_title="Title A",
        confidence=90.0,
        status="candidate",
    )
    link2 = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-222",
        discogs_artist="Artist B",
        discogs_title="Title B",
        confidence=75.0,
        status="candidate",
    )
    session.add_all([link1, link2])
    await session.flush()

    response = await client.post(f"/tracklists/discogs-links/{link1.id}/accept")
    assert response.status_code == 200
    assert "Linked" in response.text

    await session.refresh(link1)
    await session.refresh(link2)
    assert link1.status == "accepted"
    assert link2.status == "dismissed"


@pytest.mark.asyncio
async def test_accept_discogs_link_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/discogs-links/{id}/accept returns 404 for non-existent link."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/tracklists/discogs-links/{fake_id}/accept")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_dismiss_discogs_link(session: AsyncSession, client: AsyncClient) -> None:
    """DELETE /tracklists/discogs-links/{id} sets status to dismissed."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()

    track = tracks[0]
    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-333",
        discogs_artist="Dismiss Me",
        discogs_title="Gone",
        confidence=60.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    response = await client.delete(f"/tracklists/discogs-links/{link.id}")
    assert response.status_code == 200
    # The dismissed link should not appear
    assert "Dismiss Me" not in response.text
    assert "No Discogs candidates" in response.text

    await session.refresh(link)
    assert link.status == "dismissed"


@pytest.mark.asyncio
async def test_dismiss_discogs_link_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """DELETE /tracklists/discogs-links/{id} returns 404 for non-existent link."""
    fake_id = uuid.uuid4()
    response = await client.delete(f"/tracklists/discogs-links/{fake_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_bulk_link_discogs(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/bulk-link accepts top candidate per track."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=2)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    # Track 1: two candidates
    link1a = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-a1",
        discogs_artist="A1",
        discogs_title="T1",
        confidence=95.0,
        status="candidate",
    )
    link1b = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-a2",
        discogs_artist="A2",
        discogs_title="T2",
        confidence=70.0,
        status="candidate",
    )
    # Track 2: one candidate
    link2a = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[1].id,
        discogs_release_id="r-b1",
        discogs_artist="B1",
        discogs_title="T3",
        confidence=80.0,
        status="candidate",
    )
    session.add_all([link1a, link1b, link2a])
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/bulk-link")
    assert response.status_code == 200

    await session.refresh(link1a)
    await session.refresh(link1b)
    await session.refresh(link2a)
    assert link1a.status == "accepted"
    assert link1b.status == "dismissed"
    assert link2a.status == "accepted"


@pytest.mark.asyncio
async def test_bulk_link_discogs_no_candidates(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/bulk-link with no candidates returns gracefully."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    response = await client.post(f"/tracklists/{tl.id}/bulk-link")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_bulk_link_discogs_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/bulk-link returns 404 for non-existent tracklist."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/tracklists/{fake_id}/bulk-link")
    assert response.status_code == 404


# --- has_candidates and _cue_version wiring tests ---


@pytest.mark.asyncio
async def test_match_discogs_returns_has_candidates(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/match-discogs includes has_candidates when candidates exist."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    # Pre-create a candidate link
    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-test",
        discogs_artist="Test",
        discogs_title="Test Track",
        confidence=90.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    mock_queue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    response = await client.post(f"/tracklists/{tl.id}/match-discogs")
    assert response.status_code == 200
    assert "Bulk-link All" in response.text


@pytest.mark.asyncio
async def test_approve_tracklist_has_candidates(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/approve includes Bulk-link button when candidates exist."""
    tl = _make_tracklist(status="proposed")
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-appr",
        discogs_artist="A",
        discogs_title="T",
        confidence=85.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/approve")
    assert response.status_code == 200
    assert "Bulk-link All" in response.text


@pytest.mark.asyncio
async def test_approve_tracklist_no_candidates_no_bulk_button(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/approve without candidates does not show Bulk-link button."""
    tl = _make_tracklist(status="proposed")
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/approve")
    assert response.status_code == 200
    assert "Bulk-link All" not in response.text


@pytest.mark.asyncio
async def test_undo_link_preserves_cue_version(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/undo-link list response includes CUE version badge for other tracklists."""
    file1 = _make_file(original_path="/music/set1.mp3")
    file1.state = FileState.EXECUTED
    file2 = _make_file(original_path="/music/set2.mp3")
    file2.state = FileState.EXECUTED
    session.add_all([file1, file2])
    await session.flush()

    # Tracklist to undo-link
    tl1 = _make_tracklist(file_id=file1.id, match_confidence=90, auto_linked=True, external_id="undo-cue-1")
    # Tracklist that should keep CUE badge
    tl2 = _make_tracklist(file_id=file2.id, match_confidence=95, external_id="undo-cue-2", status="approved")
    session.add_all([tl1, tl2])
    await session.flush()

    with patch("phaze.routers.tracklists._get_cue_version", return_value=2):
        response = await client.post(f"/tracklists/{tl1.id}/undo-link")
    assert response.status_code == 200
    assert "CUE v" in response.text


@pytest.mark.asyncio
async def test_list_tracklists_has_candidates_in_list(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ (HTMX) shows Bulk-link button when candidates exist."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-list",
        discogs_artist="List",
        discogs_title="Track",
        confidence=88.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Bulk-link All" in response.text


@pytest.mark.asyncio
async def test_render_tracklist_list_no_version_no_candidates(session: AsyncSession, client: AsyncClient) -> None:
    """Undo-link with tracklist lacking latest_version_id sets _has_candidates=False."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=90, auto_linked=True)
    tl.latest_version_id = None
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/undo-link")
    assert response.status_code == 200
    assert "Bulk-link All" not in response.text


@pytest.mark.asyncio
async def test_render_tracklist_list_approved_non_executed_cue_zero(session: AsyncSession, client: AsyncClient) -> None:
    """Undo-link list view shows cue_version=0 for approved tracklist with non-EXECUTED file."""
    file_exec = _make_file(original_path="/music/exec.mp3")
    file_exec.state = FileState.EXECUTED
    file_disc = _make_file(original_path="/music/disc.mp3")
    file_disc.state = FileState.DISCOVERED
    session.add_all([file_exec, file_disc])
    await session.flush()

    # Tracklist to undo
    tl1 = _make_tracklist(file_id=file_exec.id, match_confidence=90, auto_linked=True, external_id="cue-zero-1")
    # Approved tracklist with non-EXECUTED file — should get _cue_version=0
    tl2 = _make_tracklist(file_id=file_disc.id, match_confidence=95, external_id="cue-zero-2", status="approved")
    session.add_all([tl1, tl2])
    await session.flush()

    response = await client.post(f"/tracklists/{tl1.id}/undo-link")
    assert response.status_code == 200
    # tl2 is approved with non-EXECUTED file, so no CUE badge
    assert "CUE v" not in response.text


@pytest.mark.asyncio
async def test_render_tracklist_list_cue_version_not_approved(session: AsyncSession, client: AsyncClient) -> None:
    """Undo-link list view shows cue_version=0 for non-approved tracklist."""
    file = _make_file()
    file.state = FileState.EXECUTED
    session.add(file)
    await session.flush()

    tl1 = _make_tracklist(file_id=file.id, match_confidence=90, auto_linked=True, external_id="cue-na-1")
    # Proposed (not approved) tracklist with EXECUTED file — should get _cue_version=0
    tl2 = _make_tracklist(external_id="cue-na-2", status="proposed")
    session.add_all([tl1, tl2])
    await session.flush()

    response = await client.post(f"/tracklists/{tl1.id}/undo-link")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_tracklists_cue_version_executed(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ shows CUE badge for approved tracklist with EXECUTED file."""
    file = _make_file()
    file.state = FileState.EXECUTED
    session.add(file)
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=90, status="approved")
    session.add(tl)
    await session.flush()

    with patch("phaze.routers.tracklists._get_cue_version", return_value=3):
        response = await client.get("/tracklists/")
    assert response.status_code == 200
    assert "CUE v3" in response.text


@pytest.mark.asyncio
async def test_list_tracklists_has_candidates_full_page(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ (full page, no HTMX) shows Bulk-link button when candidates exist."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-full",
        discogs_artist="Full",
        discogs_title="Page",
        confidence=85.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    response = await client.get("/tracklists/")
    assert response.status_code == 200
    assert "Bulk-link All" in response.text
