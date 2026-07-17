"""Integration tests for tracklists router."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.schemas.agent_tasks import ScanLiveSetPayload
from tests._queue_fakes import install_fake_queues, seed_active_agent


def _make_file(original_path: str = "/music/test.mp3", file_type: str = "mp3") -> FileRecord:
    """Create a test FileRecord."""
    filename = original_path.rsplit("/", 1)[-1]
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash="a" * 64,
        original_path=original_path,
        original_filename=filename,
        current_path=original_path,
        file_type=file_type,
        file_size=1000,
    )


def _make_executed_proposal(file_id: uuid.UUID) -> RenameProposal:
    """Seed an ``executed`` RenameProposal so ``applied()`` (READ-05/D-01) admits the file.

    The cue-version guards now read ``await is_applied(session, fr.id)`` (an executed proposal),
    NOT a scalar ``fr.state``. Fixtures that expect a CUE badge must carry an executed
    proposal; the file is left at ``state='moved'`` so the badge proves the guard reads the proposal.
    """
    return RenameProposal(
        id=uuid.uuid4(),
        file_id=file_id,
        proposed_filename="applied.mp3",
        status=ProposalStatus.EXECUTED,
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
    """Phase 57 (SHELL-05): a plain GET /tracklists/ 302-redirects into the shell.

    The "Tracklists" page heading + stats header are full-page chrome that move to the
    tracklist workspace node (a Phase-57 placeholder; real content lands in 58-61). The
    in-page HX list partial stays usable (covered by the with-data / filter tests below).
    """
    response = await client.get("/tracklists/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/tracklist"


@pytest.mark.asyncio
async def test_list_tracklists_empty_state(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ with no data shows empty state message."""
    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
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

    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
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

    response = await client.get("/tracklists/?filter=matched", headers={"HX-Request": "true"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_tracklists_filter_unmatched(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/?filter=unmatched returns only unmatched tracklists."""
    response = await client.get("/tracklists/?filter=unmatched", headers={"HX-Request": "true"})
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
    """Phase 57 (SHELL-03/05): the legacy top-nav Tracklists link is replaced by the DAG rail.

    Plan 57-03 retired the base.html tab-bar (the rail node ``hx-get="/s/tracklist"`` is the
    new nav affordance), so a plain GET /tracklists/ 302-redirects into the shell rather than
    rendering a nav bar with ``href="/tracklists/"``.
    """
    response = await client.get("/tracklists/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/tracklist"


@pytest.mark.asyncio
async def test_stats_header_values(session: AsyncSession, client: AsyncClient) -> None:
    """Phase 57 (SHELL-05): the tracklists stats header moved to the shell workspace node.

    The "Total Tracklists"/"Matched"/"Unmatched" stats header is full-page chrome rendered
    by the tracklist workspace node (a Phase-57 placeholder; real content lands in 58-61),
    so a plain GET /tracklists/ now 302-redirects into the shell.
    """
    response = await client.get("/tracklists/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/tracklist"


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
    """POST /tracklists/scan enqueues a full ScanLiveSetPayload onto the agent queue."""
    await seed_active_agent(session, "nox")
    _controller, task_router = install_fake_queues(client)

    file = _make_file()
    session.add(file)
    await session.flush()

    response = await client.post("/tracklists/scan", data={"file_ids": [str(file.id)]})
    assert response.status_code == 200
    assert "Scanning..." in response.text

    # scan_live_set captured on the per-agent queue, never the controller.
    agent_queue = task_router.queues["nox-meta"]
    assert agent_queue.name == "phaze-agent-nox-meta"
    assert len(agent_queue.captured) == 1
    task_name, payload = agent_queue.captured[0]
    assert task_name == "scan_live_set"
    assert payload["file_id"] == str(file.id)
    assert payload["original_path"] == file.original_path
    assert payload["agent_id"] == "nox"
    # The enqueued payload must validate against the strict ScanLiveSetPayload so the
    # worker no longer dead-letters it (the v4.0.8 payload-incident class).
    assert ScanLiveSetPayload.model_validate(payload)

    # The progress partial's poll URL carries agent_id so the status poll targets
    # the same per-agent queue.
    assert "agent_id=nox" in response.text


@pytest.mark.asyncio
async def test_trigger_scan_skips_file_id_without_record(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/scan skips a file_id with no FileRecord -- nothing enqueued, no dead-letter."""
    await seed_active_agent(session, "nox")
    _controller, task_router = install_fake_queues(client)

    missing_id = str(uuid.uuid4())
    response = await client.post("/tracklists/scan", data={"file_ids": [missing_id]})
    assert response.status_code == 200

    # No FileRecord for the submitted id -> nothing enqueued for it.
    assert task_router.queues["nox-meta"].captured == []


@pytest.mark.asyncio
async def test_trigger_scan_skips_malformed_file_id(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/scan skips a non-UUID file_id -- no 500, nothing enqueued."""
    await seed_active_agent(session, "nox")
    _controller, task_router = install_fake_queues(client)

    response = await client.post("/tracklists/scan", data={"file_ids": ["not-a-uuid"]})
    assert response.status_code == 200

    # A malformed id is dropped before the DB query, never enqueued, never a 500.
    assert task_router.queues["nox-meta"].captured == []


@pytest.mark.asyncio
async def test_trigger_scan_no_active_agent(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/scan with zero active agents enqueues nothing, shows empty-state."""
    # Only the conftest legacy agent exists (last_seen_at is None -> excluded).
    _controller, task_router = install_fake_queues(client)

    file_id = str(uuid.uuid4())
    response = await client.post("/tracklists/scan", data={"file_ids": [file_id]})
    assert response.status_code == 200
    assert "No active agent" in response.text
    # Nothing enqueued anywhere.
    assert task_router.queues == {}
    assert task_router.queue_for_calls == []


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

    response = await client.get("/tracklists/?filter=proposed", headers={"HX-Request": "true"})
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

    # Regression (phaze-dyvt/phaze-5fc2): a matched track carries DiscogsLink children whose FK has no
    # ON DELETE. Before the fix, deleting the track raised IntegrityError -> unhandled 500. The endpoint
    # must clear the referencing links first.
    dl = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="999",
        discogs_artist="A",
        discogs_title="T",
        confidence=0.9,
        status="candidate",
    )
    session.add(dl)
    await session.flush()

    response = await client.delete(f"/tracklists/tracks/{track.id}")
    assert response.status_code == 200
    assert response.text == ""

    remaining_links = await session.execute(select(func.count(DiscogsLink.id)).where(DiscogsLink.track_id == track.id))
    assert remaining_links.scalar_one() == 0, "the matched track's DiscogsLink children are removed with it"


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

    # Regression (phaze-5fc2): a matched low-confidence track carries a DiscogsLink child (FK has no
    # ON DELETE, Core bulk delete fires no cascade). Before the fix ONE such row raised IntegrityError,
    # rolling back the single-statement bulk delete and rendering reject-low wholly inoperable on any
    # matched tracklist. Match Discogs creates candidate links confidence-indifferently, so a low-conf
    # track can absolutely carry one.
    dl = DiscogsLink(
        id=uuid.uuid4(),
        track_id=low_conf.id,
        discogs_release_id="777",
        discogs_artist="Bad Discogs",
        discogs_title="Bad Discogs Title",
        confidence=0.3,
        status="candidate",
    )
    session.add(dl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/reject-low?threshold=50")
    assert response.status_code == 200
    assert "Good" in response.text
    assert "Bad" not in response.text

    # The low-confidence track AND its referencing link are both gone; the high-confidence track survives.
    remaining_links = await session.execute(select(func.count(DiscogsLink.id)).where(DiscogsLink.track_id == low_conf.id))
    assert remaining_links.scalar_one() == 0, "referencing DiscogsLink rows are cleared before the bulk track delete"
    remaining_tracks = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == version.id))
    assert remaining_tracks.scalar_one() == 1, "only the high-confidence track survives"


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

    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Proposed" in response.text


# --- Scan status / progress ---


@pytest.mark.asyncio
async def test_scan_status_all_complete(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status polls the per-agent queue and reports completion."""
    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.COMPLETE
    mock_job.result = {"status": "scanned", "filename": "test.mp3"}

    _controller, task_router = install_fake_queues(client)
    task_router.queue_for("nox", "meta").job = AsyncMock(return_value=mock_job)

    response = await client.get("/tracklists/scan/status?job_ids=job-1&agent_id=nox")
    assert response.status_code == 200
    # The poll resolved the per-agent queue via task_router.queue_for("nox").
    assert "nox" in task_router.queue_for_calls
    assert "Scan complete" in response.text


@pytest.mark.asyncio
async def test_scan_status_with_error(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status reports errors from failed jobs on the per-agent queue."""
    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.FAILED
    mock_job.result = {"status": "error", "filename": "bad.mp3"}

    _controller, task_router = install_fake_queues(client)
    task_router.queue_for("nox", "meta").job = AsyncMock(return_value=mock_job)

    response = await client.get("/tracklists/scan/status?job_ids=job-1&agent_id=nox")
    assert response.status_code == 200
    assert "bad.mp3" in response.text


@pytest.mark.asyncio
async def test_scan_status_job_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status handles a missing job gracefully (counts complete)."""
    _controller, task_router = install_fake_queues(client)
    task_router.queue_for("nox", "meta").job = AsyncMock(return_value=None)

    response = await client.get("/tracklists/scan/status?job_ids=missing-job&agent_id=nox")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_scan_status_job_pending(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status handles pending jobs (no result yet)."""
    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.QUEUED
    mock_job.result = None

    _controller, task_router = install_fake_queues(client)
    task_router.queue_for("nox", "meta").job = AsyncMock(return_value=mock_job)

    response = await client.get("/tracklists/scan/status?job_ids=pending-job&agent_id=nox")
    assert response.status_code == 200
    # Job still queued -> not done -> poll partial keeps emitting agent_id.
    assert "agent_id=nox" in response.text


# --- HARD-03 (AR-30-03 / Phase-30 REVIEW IN-01): agent_id boundary validation ---
# A malformed agent_id must 422 at the HTTP boundary instead of a silently-empty
# 200 poll. Pattern + max_length mirror the Agent.id DB CHECK (models/agent.py:36)
# and the CLI AGENT_ID_RE (cli/__init__.py:44).


@pytest.mark.asyncio
async def test_scan_status_malformed_agent_id_returns_422(session: AsyncSession, client: AsyncClient) -> None:
    """HARD-03: a malformed agent_id -> 422 (was a silent empty 200 poll)."""
    response = await client.get("/tracklists/scan/status?job_ids=job-1&agent_id=Bad_ID!")
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_scan_status_well_formed_agent_id_passes_validation(session: AsyncSession, client: AsyncClient) -> None:
    """HARD-03: a well-formed agent_id still reaches the handler (not a 422)."""
    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.COMPLETE
    mock_job.result = {"status": "scanned", "filename": "test.mp3"}

    _controller, task_router = install_fake_queues(client)
    task_router.queue_for("test-agent-01", "meta").job = AsyncMock(return_value=mock_job)

    response = await client.get("/tracklists/scan/status?job_ids=job-1&agent_id=test-agent-01")
    assert response.status_code != 422
    assert response.status_code == 200, response.text


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

    controller_queue, task_router = install_fake_queues(client)

    response = await client.post(f"/tracklists/{tl.id}/rescrape")
    assert response.status_code == 200
    # Controller task lands on the controller queue, never a per-agent queue.
    assert controller_queue.captured == [("scrape_and_store_tracklist", {"tracklist_id": str(tl.id)})]
    assert task_router.queues == {}


@pytest.mark.asyncio
async def test_rescrape_tracklist_has_candidates_in_context(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/rescrape includes has_candidates when candidates exist."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    # Create a version and track
    version = TracklistVersion(
        id=uuid.uuid4(),
        tracklist_id=tl.id,
        version_number=1,
    )
    session.add(version)
    await session.flush()

    tl.latest_version_id = version.id
    await session.flush()

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Track Artist",
        title="Track Title",
    )
    session.add(track)
    await session.flush()

    # Create a candidate DiscogsLink for the track
    dl = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="12345",
        discogs_artist="Discogs Artist",
        discogs_title="Discogs Title",
        confidence=0.85,
        status="candidate",
    )
    session.add(dl)
    await session.flush()

    install_fake_queues(client)

    response = await client.post(f"/tracklists/{tl.id}/rescrape")
    assert response.status_code == 200
    # The bulk-link button text should appear when has_candidates is True
    assert "Bulk" in response.text


@pytest.mark.asyncio
async def test_manual_search(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/search enqueues a search job onto the controller queue."""
    controller_queue, task_router = install_fake_queues(client)

    file_id = uuid.uuid4()
    response = await client.post(f"/tracklists/search?file_id={file_id}")
    assert response.status_code == 200
    assert controller_queue.captured == [("search_tracklist", {"file_id": str(file_id)})]
    assert task_router.queues == {}


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

    controller_queue, task_router = install_fake_queues(client)

    response = await client.post(f"/tracklists/{tl.id}/match-discogs")
    assert response.status_code == 200
    assert controller_queue.captured == [("match_tracklist_to_discogs", {"tracklist_id": str(tl.id)})]
    assert task_router.queues == {}


@pytest.mark.asyncio
async def test_match_discogs_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/match-discogs returns 404 for non-existent tracklist."""
    fake_id = uuid.uuid4()
    install_fake_queues(client)

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

    install_fake_queues(client)

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
    # READ-05/D-01: applied-ness comes from an executed proposal, not file.state (kept at 'moved').
    file1 = _make_file(original_path="/music/set1.mp3")
    file2 = _make_file(original_path="/music/set2.mp3")
    session.add_all([file1, file2])
    await session.flush()
    session.add_all([_make_executed_proposal(file1.id), _make_executed_proposal(file2.id)])
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
    file_disc = _make_file(original_path="/music/disc.mp3")
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
    """GET /tracklists/ shows CUE badge for approved tracklist with an applied (executed-proposal) file.

    READ-05/D-01: the cue-version guard reads ``is_applied`` (an executed proposal), so the file is
    left at ``state='moved'`` and made applied via an executed proposal -- proving the badge derives
    from ``proposals.status``, not ``files.state``.
    """
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(_make_executed_proposal(file.id))
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=90, status="approved")
    session.add(tl)
    await session.flush()

    with patch("phaze.routers.tracklists._get_cue_version", return_value=3):
        response = await client.get("/tracklists/", headers={"HX-Request": "true"})
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

    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Bulk-link All" in response.text
