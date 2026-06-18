"""Tests for the pipeline orchestration router."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.config import settings
from phaze.models.agent import LEGACY_AGENT_ID
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.tracklist import Tracklist
from phaze.schemas.agent_tasks import ExtractMetadataPayload, ProcessFilePayload, ScanLiveSetPayload
from tests._queue_fakes import install_fake_queues, seed_active_agent, wire_fakes


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Phase 30 Plan 02: fake named-queue capture harness
#
# The lifespan is NOT run for the test client, so handlers read whatever we
# attach to ``app.state``. ``wire_fakes`` (tests/_queue_fakes.py) attaches a fake
# ``controller_queue`` (named "controller") and a fake ``task_router`` whose
# ``queue_for(agent_id)`` returns a queue named ``phaze-agent-<id>``; every
# ``enqueue`` appends ``(queue_name, task_name, kwargs)`` to a shared capture list
# so tests can assert the exact destination queue per endpoint -- proving the
# v4.0.6 default-queue misrouting is gone.
# ---------------------------------------------------------------------------


async def _drain_background() -> None:
    """Yield until the router's background enqueue tasks have drained."""
    import phaze.routers.pipeline as pipeline_mod

    for _ in range(500):
        if not pipeline_mod._background_tasks:
            return
        await asyncio.sleep(0)


def _make_file(*, state: str = FileState.DISCOVERED) -> FileRecord:
    """Create a FileRecord with the given state."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=state,
    )


def _make_file_with_convergence(*, state: str = FileState.ANALYZED) -> tuple[FileRecord, AnalysisResult, FileMetadata]:
    """Create a FileRecord with both AnalysisResult and FileMetadata for convergence gate."""
    uid = uuid.uuid4()
    file_rec = FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=state,
    )
    analysis = AnalysisResult(file_id=uid, bpm=128.0, musical_key="Cm")
    metadata = FileMetadata(file_id=uid, artist="Test", title="Track")
    return file_rec, analysis, metadata


@pytest.mark.asyncio
async def test_dashboard_page(client: AsyncClient) -> None:
    """GET /pipeline/ returns 200 with Pipeline Dashboard heading."""
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Pipeline Dashboard" in response.text


@pytest.mark.asyncio
async def test_dashboard_links_to_saq_ui(client: AsyncClient) -> None:
    """GET /pipeline/ renders an anchor to the SAQ dashboard mounted at /saq (plan 33-02).

    Operator request 2026-06-11: the SAQ queue monitor must be reachable from the pipeline
    page, not only by typing the /saq URL directly. The link points at the mounted full-page
    SAQ app, so it opens in a new tab with rel="noopener".
    """
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    assert 'href="/saq"' in response.text


@pytest.mark.asyncio
async def test_dashboard_includes_settings_batch_size(client: AsyncClient) -> None:
    """GET /pipeline/ dashboard context includes settings_batch_size (default 10)."""
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    # The batch size value (10) should appear in the rendered template
    assert "10" in response.text


@pytest.mark.asyncio
async def test_analyze_enqueues_discovered(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/analyze enqueues process_file onto phaze-agent-nox (not default)."""
    session.add_all([_make_file(state=FileState.DISCOVERED) for _ in range(3)])
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 3

    await _drain_background()
    assert len(capture) == 3
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-nox", "process_file")}
    assert all(q != "default" for q, _, _ in capture)


@pytest.mark.asyncio
async def test_analyze_enqueues_complete_process_file_payload(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (run-analysis-payload-invalid): /api/v1/analyze must enqueue a COMPLETE ProcessFilePayload.

    Before the fix, ``_enqueue_analysis_jobs`` passed only ``file_id``; the agent
    worker's ``ProcessFilePayload.model_validate(kwargs)`` (``extra="forbid"``) then
    raised four "Field required" errors and dead-lettered every job, stranding all
    files in DISCOVERED. This asserts all five required fields are present, carry the
    FileRecord / selected-agent / settings.models_path values, and that the exact
    kwargs the worker receives validate cleanly against ``ProcessFilePayload``.
    """
    file_rec = _make_file(state=FileState.DISCOVERED)
    session.add(file_rec)
    await session.commit()
    # expire_on_commit=False (conftest) -- these stay readable after commit.
    expected_id = str(file_rec.id)
    expected_path = file_rec.original_path
    expected_type = file_rec.file_type
    agent = await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    assert response.json()["enqueued"] == 1

    await _drain_background()
    assert len(capture) == 1
    queue_name, task_name, kwargs = capture[0]
    assert (queue_name, task_name) == ("phaze-agent-nox", "process_file")

    # All five required fields present -- not just file_id (the pre-fix bug).
    assert set(kwargs) == {"file_id", "original_path", "file_type", "agent_id", "models_path"}
    assert kwargs["file_id"] == expected_id
    assert kwargs["original_path"] == expected_path
    assert kwargs["file_type"] == expected_type
    assert kwargs["agent_id"] == agent.id
    assert kwargs["models_path"] == settings.models_path

    # The exact kwargs the agent worker receives validate against ProcessFilePayload.
    validated = ProcessFilePayload.model_validate(kwargs)
    assert str(validated.file_id) == expected_id


@pytest.mark.asyncio
async def test_extract_metadata_enqueues_complete_payload(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (35-REVIEW CR-01): /api/v1/extract-metadata must enqueue a COMPLETE ExtractMetadataPayload.

    D-06 removed the agent file-upsert auto-enqueue -- the only producer that built the full
    payload -- making this manual trigger the SOLE metadata producer. The surviving path passed
    only ``file_id``; the agent worker's ``ExtractMetadataPayload.model_validate(kwargs)``
    (``extra="forbid"``) then raised "Field required" and dead-lettered every job (the same
    class as the v4.0.8 payload incident). This pins all four required fields and that the
    exact kwargs validate cleanly.
    """
    file_rec = _make_file(state=FileState.DISCOVERED)
    session.add(file_rec)
    await session.commit()
    expected_id = str(file_rec.id)
    expected_path = file_rec.original_path
    expected_type = file_rec.file_type
    agent = await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/extract-metadata")
    assert response.status_code == 200
    assert response.json()["enqueued"] == 1

    await _drain_background()
    assert len(capture) == 1
    queue_name, task_name, kwargs = capture[0]
    assert (queue_name, task_name) == ("phaze-agent-nox", "extract_file_metadata")

    # All four required fields present -- not just file_id (the CR-01 bug).
    assert set(kwargs) == {"file_id", "original_path", "file_type", "agent_id"}
    assert kwargs["file_id"] == expected_id
    assert kwargs["original_path"] == expected_path
    assert kwargs["file_type"] == expected_type
    assert kwargs["agent_id"] == agent.id

    # The exact kwargs the agent worker receives validate against ExtractMetadataPayload.
    validated = ExtractMetadataPayload.model_validate(kwargs)
    assert str(validated.file_id) == expected_id


@pytest.mark.asyncio
async def test_analyze_enqueues_bounded_timeout_and_retries(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 43: POST /api/v1/analyze enqueues process_file with timeout=7200 + retries=2.

    The outer SAQ timeout was lowered from the Phase 31 4h bound (14400) to a 2h net
    (7200): Phase 43 caps per-file cost (even-stride), so the inner pebble per-task
    timeout (analysis_inner_timeout_sec, 6600 < 7200) does the real killing and the
    outer net only reclaims a dead/restarted worker's slot. retries=2 stays in the
    locked 1-2 band so apply_project_job_defaults does NOT clobber it to
    worker_max_retries (the retries==1 -> 4 churn).
    """
    file_rec = _make_file(state=FileState.DISCOVERED)
    session.add(file_rec)
    await session.commit()
    expected_key = f"process_file:{file_rec.id}"
    await seed_active_agent(session)
    _, task_router = install_fake_queues(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    assert response.json()["enqueued"] == 1

    await _drain_background()
    queue = task_router.queues["nox"]
    assert len(queue.captured_policy) == 1
    # Phase 32: the shared helper now also sets the deterministic dedup key.
    assert queue.captured_policy[0] == {"key": expected_key, "timeout": 7200, "retries": 2}
    # retries is explicitly NOT 1 (which apply_project_job_defaults would override to 4).
    assert queue.captured_policy[0]["retries"] != 1
    # Payload still complete (job-control keys are split out, not part of the payload).
    task_name, payload = queue.captured[0]
    assert task_name == "process_file"
    assert set(payload) == {"file_id", "original_path", "file_type", "agent_id", "models_path"}


@pytest.mark.asyncio
async def test_analyze_enqueues_deterministic_key_per_file(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 32: the dashboard "Run Analysis" path now emits ``process_file:<file_id>`` per file.

    Proves both producers (this dashboard path + the Wave-2 reboot re-enqueue) emit the
    IDENTICAL deterministic key so SAQ's per-queue dedup can collapse a re-trigger of an
    in-flight file to a no-op (32-CONTEXT "Dedup"; 32-RESEARCH §Q4). Each enqueue's
    ``captured_policy["key"]`` must equal ``process_file:`` + that enqueue's payload file_id.
    """
    files = [_make_file(state=FileState.DISCOVERED) for _ in range(3)]
    session.add_all(files)
    await session.commit()
    expected_keys = {f"process_file:{f.id}" for f in files}
    await seed_active_agent(session)
    _, task_router = install_fake_queues(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    assert response.json()["enqueued"] == 3

    await _drain_background()
    queue = task_router.queues["nox"]
    assert len(queue.captured_policy) == 3
    # Every enqueue carries a key, and it matches that same enqueue's payload file_id.
    for (task_name, payload), policy in zip(queue.captured, queue.captured_policy, strict=True):
        assert task_name == "process_file"
        assert policy["key"] == f"process_file:{payload['file_id']}"
    assert {p["key"] for p in queue.captured_policy} == expected_keys


@pytest.mark.asyncio
async def test_analyze_ui_enqueues_bounded_timeout_and_retries(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 43: the HTMX /pipeline/analyze path also enqueues with timeout=7200 + retries=2."""
    file_rec = _make_file(state=FileState.DISCOVERED)
    session.add(file_rec)
    await session.commit()
    expected_key = f"process_file:{file_rec.id}"
    await seed_active_agent(session)
    _, task_router = install_fake_queues(client)

    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200

    await _drain_background()
    queue = task_router.queues["nox"]
    assert len(queue.captured_policy) == 1
    # Phase 32: the shared helper now also sets the deterministic dedup key.
    assert queue.captured_policy[0] == {"key": expected_key, "timeout": 7200, "retries": 2}


@pytest.mark.asyncio
async def test_process_file_enqueue_policy_survives_project_defaults_hook() -> None:
    """The before_enqueue hook leaves the explicit timeout=7200 / retries=2 intact.

    apply_project_job_defaults only overrides a Job still at the SAQ defaults
    (timeout==10, retries==1). An explicit retries=2 (and timeout=7200) is honored,
    proving the process_file enqueue escapes the retries==1 -> worker_max_retries clobber.
    """
    from saq import Job

    from phaze.tasks._shared.queue_defaults import apply_project_job_defaults

    job = Job(function="process_file", timeout=7200, retries=2)
    await apply_project_job_defaults(job)
    assert job.timeout == 7200
    assert job.retries == 2


@pytest.mark.asyncio
async def test_analyze_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/analyze with files but no active agent surfaces a visible empty-state."""
    session.add_all([_make_file(state=FileState.DISCOVERED) for _ in range(3)])
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0
    assert "no active agent" in data["message"].lower()

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_analyze_no_files(client: AsyncClient) -> None:
    """POST /api/v1/analyze with no DISCOVERED files returns enqueued=0."""
    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0


@pytest.mark.asyncio
async def test_proposals_generate_batches(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/proposals/generate enqueues generate_proposals onto the controller queue."""
    files = []
    related = []
    for _ in range(15):
        file_rec, analysis, metadata = _make_file_with_convergence(state=FileState.ANALYZED)
        files.append(file_rec)
        related.extend([analysis, metadata])
    session.add_all(files)
    await session.flush()
    session.add_all(related)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/api/v1/proposals/generate")
    assert response.status_code == 200
    data = response.json()
    assert data["total_files"] == 15
    assert data["enqueued_batches"] == 2  # 15 files / 10 batch_size = 2 batches

    await _drain_background()
    assert len(capture) == 2
    assert {(q, t) for q, t, _ in capture} == {("controller", "generate_proposals")}


@pytest.mark.asyncio
async def test_proposals_generate_no_files(client: AsyncClient) -> None:
    """POST /api/v1/proposals/generate with no ANALYZED files returns zero counts."""
    response = await client.post("/api/v1/proposals/generate")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued_batches"] == 0
    assert data["total_files"] == 0


# ---------------------------------------------------------------------------
# Phase 39 (REQ-39-1/REQ-39-4): bulk search-tracklist trigger routes to the controller queue
# ---------------------------------------------------------------------------


def _link_tracklist(file_rec: FileRecord) -> Tracklist:
    """Build a Tracklist row linked to ``file_rec`` (marks the file as already-matched)."""
    uid = uuid.uuid4()
    return Tracklist(
        external_id=uid.hex,
        source_url=f"https://1001.tl/{uid.hex}",
        file_id=file_rec.id,
    )


@pytest.mark.asyncio
async def test_search_tracklists_routes_to_controller_queue(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/search-tracklists enqueues search_tracklist on the controller queue (never default).

    search_tracklist is a CONTROLLER task (Phase-30 rule). The capture must be exactly
    {("controller","search_tracklist")} — a routing regression that sent it to the consumer-less
    default queue is caught here.
    """
    files = [_make_file(state=FileState.DISCOVERED) for _ in range(3)]
    session.add_all(files)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/pipeline/search-tracklists")
    assert response.status_code == 200

    await _drain_background()
    assert len(capture) == 3
    assert {(q, t) for q, t, _ in capture} == {("controller", "search_tracklist")}
    assert all(q != "default" for q, _, _ in capture)
    # Each enqueue carries the file_id the deterministic key dedups on.
    assert {c[2]["file_id"] for c in capture} == {str(f.id) for f in files}


@pytest.mark.asyncio
async def test_search_tracklists_excludes_files_with_existing_tracklist(client: AsyncClient, session: AsyncSession) -> None:
    """A file that already has a linked tracklist is skipped from the eligible set (idempotent re-run)."""
    matched = _make_file(state=FileState.DISCOVERED)
    unmatched = _make_file(state=FileState.DISCOVERED)
    session.add_all([matched, unmatched])
    await session.flush()
    session.add(_link_tracklist(matched))
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/pipeline/search-tracklists")
    assert response.status_code == 200

    await _drain_background()
    # Only the unmatched file is enqueued; the matched file is excluded.
    assert len(capture) == 1
    assert capture[0][2]["file_id"] == str(unmatched.id)


@pytest.mark.asyncio
async def test_search_tracklists_no_eligible_files_returns_200(client: AsyncClient) -> None:
    """A zero-eligible POST returns 200 and enqueues nothing (renders the 'No files ready' copy)."""
    capture = wire_fakes(client)
    response = await client.post("/pipeline/search-tracklists")
    assert response.status_code == 200
    assert "No files ready for tracklist search" in response.text

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_dashboard_renders_search_trigger_end_to_end(client: AsyncClient) -> None:
    """GET /pipeline/ exposes the Search trigger + 'Needs metadata' gate copy end-to-end (REQ-39-2).

    On an empty DB metadataDone == 0, so the Search node is gated 'Needs metadata' by default. The
    rendered dashboard must carry the bulk Search trigger's hx-post target and the LOCKED gate copy,
    proving the Phase-39 trigger surface reaches the page (not just the partial render tests)."""
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    body = response.text
    assert 'hx-post="/pipeline/search-tracklists"' in body
    assert "Needs metadata" in body


# ---------------------------------------------------------------------------
# Phase 40 (REQ-40-1/REQ-40-4): bulk fingerprint-scan trigger routes per-agent with the
# COMPLETE ScanLiveSetPayload (never default/controller), surfaces a no-agent empty-state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_live_sets_routes_to_per_agent_queue_with_complete_payload(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/scan-live-sets enqueues scan_live_set on the PER-AGENT queue (never default/controller).

    scan_live_set is a PER-AGENT task (Phase-30 rule). The capture must be exactly
    {("phaze-agent-nox","scan_live_set")} — a routing regression that sent it to the consumer-less
    default queue (or the controller queue) is caught here (T-40-04). Every enqueue must carry the
    COMPLETE ScanLiveSetPayload (file_id, original_path, agent_id) so no job dead-letters on the
    extra="forbid" validation (T-40-DL, the v4.0.8 payload-incident class).
    """
    files = [_make_file(state=FileState.DISCOVERED) for _ in range(3)]
    session.add_all(files)
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/pipeline/scan-live-sets")
    assert response.status_code == 200
    assert "fingerprint scan" in response.text

    await _drain_background()
    assert len(capture) == 3
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-nox", "scan_live_set")}
    assert all(q != "default" for q, _, _ in capture)
    assert all(q != "controller" for q, _, _ in capture)
    # Every enqueue carries the COMPLETE payload — model_validate (extra="forbid") must accept it.
    for _q, _t, kwargs in capture:
        ScanLiveSetPayload.model_validate(kwargs)
    assert {c[2]["file_id"] for c in capture} == {str(f.id) for f in files}


@pytest.mark.asyncio
async def test_scan_live_sets_excludes_files_with_existing_tracklist(client: AsyncClient, session: AsyncSession) -> None:
    """A file that already has a linked tracklist is skipped from the eligible set (idempotent re-run)."""
    matched = _make_file(state=FileState.DISCOVERED)
    unmatched = _make_file(state=FileState.DISCOVERED)
    session.add_all([matched, unmatched])
    await session.flush()
    session.add(_link_tracklist(matched))
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/pipeline/scan-live-sets")
    assert response.status_code == 200

    await _drain_background()
    # Only the unmatched file is enqueued; the matched file is excluded.
    assert len(capture) == 1
    assert capture[0][2]["file_id"] == str(unmatched.id)


@pytest.mark.asyncio
async def test_scan_live_sets_no_active_agent_renders_empty_state(client: AsyncClient, session: AsyncSession) -> None:
    """Eligible files but NO online agent → 200, nothing enqueued, no-active-agent copy (never 500)."""
    session.add_all([_make_file(state=FileState.DISCOVERED) for _ in range(2)])
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/pipeline/scan-live-sets")
    assert response.status_code == 200
    assert "No active agent available" in response.text

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_scan_live_sets_no_eligible_files_returns_200(client: AsyncClient) -> None:
    """A zero-eligible POST returns 200, enqueues nothing, and never resolves a queue (no agent needed)."""
    capture = wire_fakes(client)
    response = await client.post("/pipeline/scan-live-sets")
    assert response.status_code == 200
    assert "No files ready for fingerprint scan" in response.text

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_dashboard_renders_scan_trigger_end_to_end(client: AsyncClient) -> None:
    """GET /pipeline/ exposes the Fingerprint-Scan trigger + 'Needs agent' gate copy end-to-end (REQ-40-1/2).

    On an empty DB agentOnline == 0, so the Fingerprint-Scan node is gated 'Needs agent' by default
    (the literal lives in the node getter regardless of state). The rendered dashboard must carry the
    bulk Scan trigger's hx-post target and the LOCKED gate copy, proving the Phase-40 trigger surface
    reaches the page (not just the partial render tests)."""
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    body = response.text
    assert 'hx-post="/pipeline/scan-live-sets"' in body
    assert "Needs agent" in body


# ---------------------------------------------------------------------------
# Phase 41 (REQ-41-1/REQ-41-2/REQ-41-4): bulk scrape + match triggers route to the controller
# queue (never default), skip already-done rows, render the tracklist-unit empty-state.
# ---------------------------------------------------------------------------


def _make_tracklist(n: int) -> Tracklist:
    """Build a bare Tracklist row (no version, no discogs chain) — scrape AND match pending."""
    uid = uuid.uuid4()
    return Tracklist(id=uid, external_id=f"tl-{n}-{uid.hex}", source_url=f"http://x/{n}")


@pytest.mark.asyncio
async def test_scrape_tracklists_routes_to_controller_queue(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/scrape-tracklists enqueues scrape_and_store_tracklist on the controller queue.

    scrape_and_store_tracklist is a CONTROLLER task (Phase-30 rule). The capture must be exactly
    {("controller","scrape_and_store_tracklist")} — a routing regression that sent it to the
    consumer-less default queue is caught here (T-41-04).
    """
    tracklists = [_make_tracklist(i) for i in range(3)]
    session.add_all(tracklists)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/pipeline/scrape-tracklists")
    assert response.status_code == 200

    await _drain_background()
    assert len(capture) == 3
    assert {(q, t) for q, t, _ in capture} == {("controller", "scrape_and_store_tracklist")}
    assert all(q != "default" for q, _, _ in capture)
    # Each enqueue carries the tracklist_id the deterministic key dedups on.
    assert {c[2]["tracklist_id"] for c in capture} == {str(tl.id) for tl in tracklists}


@pytest.mark.asyncio
async def test_scrape_tracklists_excludes_versioned(client: AsyncClient, session: AsyncSession) -> None:
    """A tracklist that already has a scraped version is skipped from the scrape pending set."""
    from phaze.models.tracklist import TracklistVersion

    pending = _make_tracklist(1)
    scraped = _make_tracklist(2)
    session.add_all([pending, scraped])
    await session.flush()
    session.add(TracklistVersion(id=uuid.uuid4(), tracklist_id=scraped.id, version_number=1))
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/pipeline/scrape-tracklists")
    assert response.status_code == 200

    await _drain_background()
    assert len(capture) == 1
    assert capture[0][2]["tracklist_id"] == str(pending.id)


@pytest.mark.asyncio
async def test_scrape_tracklists_no_pending_returns_200(client: AsyncClient) -> None:
    """A zero-pending POST returns 200 and enqueues nothing (renders the tracklist-unit empty-state)."""
    capture = wire_fakes(client)
    response = await client.post("/pipeline/scrape-tracklists")
    assert response.status_code == 200
    assert "No tracklists ready for scraping" in response.text

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_match_tracklists_routes_to_controller_queue(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/match-tracklists enqueues match_tracklist_to_discogs on the controller queue.

    match_tracklist_to_discogs is a CONTROLLER task (Phase-30 rule). The capture must be exactly
    {("controller","match_tracklist_to_discogs")} — never the consumer-less default queue (T-41-04).
    """
    tracklists = [_make_tracklist(i) for i in range(3)]
    session.add_all(tracklists)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/pipeline/match-tracklists")
    assert response.status_code == 200

    await _drain_background()
    assert len(capture) == 3
    assert {(q, t) for q, t, _ in capture} == {("controller", "match_tracklist_to_discogs")}
    assert all(q != "default" for q, _, _ in capture)
    assert {c[2]["tracklist_id"] for c in capture} == {str(tl.id) for tl in tracklists}


@pytest.mark.asyncio
async def test_match_tracklists_excludes_discogs_reachable(client: AsyncClient, session: AsyncSession) -> None:
    """A tracklist already reachable from discogs_links is skipped from the match pending set."""
    from phaze.models.discogs_link import DiscogsLink
    from phaze.models.tracklist import TracklistTrack, TracklistVersion

    pending = _make_tracklist(1)
    linked = _make_tracklist(2)
    session.add_all([pending, linked])
    await session.flush()
    linked_version = TracklistVersion(id=uuid.uuid4(), tracklist_id=linked.id, version_number=1)
    session.add(linked_version)
    await session.flush()
    track = TracklistTrack(id=uuid.uuid4(), version_id=linked_version.id, position=1)
    session.add(track)
    await session.flush()
    session.add(DiscogsLink(id=uuid.uuid4(), track_id=track.id, discogs_release_id="r1", confidence=0.9))
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/pipeline/match-tracklists")
    assert response.status_code == 200

    await _drain_background()
    assert len(capture) == 1
    assert capture[0][2]["tracklist_id"] == str(pending.id)


@pytest.mark.asyncio
async def test_match_tracklists_no_pending_returns_200(client: AsyncClient) -> None:
    """A zero-pending POST returns 200 and enqueues nothing (renders the tracklist-unit empty-state)."""
    capture = wire_fakes(client)
    response = await client.post("/pipeline/match-tracklists")
    assert response.status_code == 200
    assert "No tracklists ready for matching" in response.text

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_dashboard_renders_scrape_and_match_triggers_end_to_end(client: AsyncClient) -> None:
    """GET /pipeline/ exposes BOTH Scrape + Match triggers + the 'Needs tracklist' gate copy (REQ-41-4).

    On an empty DB scrapeTotal/matchTotal == 0, so both nodes are gated 'Needs tracklist' by default
    (the literal lives in the node getter regardless of state). The rendered dashboard must carry both
    bulk triggers' hx-post targets, proving the Phase-41 trigger surface reaches the page end-to-end
    (not just the partial render tests)."""
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    body = response.text
    assert 'hx-post="/pipeline/scrape-tracklists"' in body
    assert 'hx-post="/pipeline/match-tracklists"' in body
    assert "Needs tracklist" in body


# ---------------------------------------------------------------------------
# Phase 42 (REQ-42-1/REQ-42-4/REQ-42-5): the manual /pipeline/recover endpoint calls the
# SAME gated recover_orphaned_work producer (force=True) the controller startup runs, on a
# worker-shaped ctx built from app state; the global DAG "Recover" button renders end-to-end.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_invokes_recover_orphaned_work_forced(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /pipeline/recover schedules recover_orphaned_work(force=True) on a worker-shaped ctx.

    The endpoint must call the SAME producer as controller startup (D-03 — manual and automatic
    paths cannot drift), forced (D-05 cold-boot safety net), with a ctx wired from app state: the
    lifespan ``controller_queue`` (controller stages) + ``task_router`` (per-agent stages) + the
    module-level ``async_session`` sessionmaker. The producer is patched so no real DB/queue work
    runs — we only assert the wiring and force flag.
    """
    import phaze.routers.pipeline as pipeline_mod

    captured: dict[str, object] = {}

    async def fake_recover(ctx: dict[str, object], *, force: bool = False) -> dict[str, object]:
        captured["ctx"] = ctx
        captured["force"] = force
        return {"detected_loss": True, "forced": force, "stages": {}}

    monkeypatch.setattr(pipeline_mod, "recover_orphaned_work", fake_recover)
    controller_queue, task_router = install_fake_queues(client)

    response = await client.post("/pipeline/recover")
    assert response.status_code == 200
    assert "Recovery started" in response.text
    assert "nothing will double-enqueue" in response.text

    await _drain_background()
    assert captured["force"] is True, "manual Recover must force=True (bypass the no-op detect gate, not the dedup)"
    ctx = captured["ctx"]
    assert isinstance(ctx, dict)
    assert ctx["queue"] is controller_queue, "ctx['queue'] must be the lifespan controller queue (controller stages)"
    assert ctx["task_router"] is task_router, "ctx['task_router'] must be the lifespan task_router (per-agent stages)"
    assert "async_session" in ctx, "ctx must carry the async_session sessionmaker for the worker-shaped recovery"


@pytest.mark.asyncio
async def test_recover_returns_200_when_producer_raises_is_isolated(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing background recovery never reaches the HTTP response — the endpoint still returns 200.

    The producer runs fire-and-forget in a background task, so even a raising recover_orphaned_work
    cannot 500 the request (T-42-06): the operator always gets the "recovery started" fragment.
    """
    import phaze.routers.pipeline as pipeline_mod

    async def boom(ctx: dict[str, object], *, force: bool = False) -> dict[str, object]:
        raise RuntimeError("recovery boom")

    monkeypatch.setattr(pipeline_mod, "recover_orphaned_work", boom)
    install_fake_queues(client)

    response = await client.post("/pipeline/recover")
    assert response.status_code == 200
    assert "Recovery started" in response.text

    await _drain_background()


@pytest.mark.asyncio
async def test_dashboard_renders_recover_button_end_to_end(client: AsyncClient) -> None:
    """GET /pipeline/ exposes the GLOBAL Recover button posting to /pipeline/recover (REQ-42-5).

    The recovery affordance is a pipeline-level action in the DAG header (not a per-stage node), so
    the rendered dashboard must carry its hx-post target + label, proving the Phase-42 manual recovery
    surface reaches the page end-to-end (not just the partial render test)."""
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    body = response.text
    assert 'hx-post="/pipeline/recover"' in body
    assert "Recover orphaned work" in body


@pytest.mark.asyncio
async def test_pipeline_stats_partial(client: AsyncClient, session: AsyncSession) -> None:
    """GET /pipeline/stats returns 200 with HTML containing count values."""
    session.add(_make_file(state=FileState.DISCOVERED))
    await session.commit()

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # Stats bar should contain the count
    assert "Discovered" in response.text
    assert "Analyzed" in response.text


@pytest.mark.asyncio
async def test_trigger_analysis_ui_with_files(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/analyze enqueues process_file onto phaze-agent-nox + renders the fragment."""
    session.add_all([_make_file(state=FileState.DISCOVERED) for _ in range(2)])
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "analysis" in response.text

    await _drain_background()
    assert len(capture) == 2
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-nox", "process_file")}
    # UI path enqueues a complete payload too (every job carries all five fields).
    for _q, _t, kwargs in capture:
        ProcessFilePayload.model_validate(kwargs)


@pytest.mark.asyncio
async def test_trigger_analysis_ui_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/analyze with files but no active agent renders the no-active-agent copy."""
    session.add_all([_make_file(state=FileState.DISCOVERED) for _ in range(2)])
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200
    assert "No active agent available" in response.text

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_trigger_analysis_ui_no_files(client: AsyncClient) -> None:
    """POST /pipeline/analyze with no DISCOVERED files returns HTML with zero count."""
    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_trigger_proposals_ui_with_files(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/proposals enqueues generate_proposals onto the controller queue."""
    files = []
    related = []
    for _ in range(5):
        file_rec, analysis, metadata = _make_file_with_convergence(state=FileState.ANALYZED)
        files.append(file_rec)
        related.extend([analysis, metadata])
    session.add_all(files)
    await session.flush()
    session.add_all(related)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/pipeline/proposals")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "proposal generation" in response.text

    await _drain_background()
    assert len(capture) == 1
    assert {(q, t) for q, t, _ in capture} == {("controller", "generate_proposals")}


@pytest.mark.asyncio
async def test_trigger_proposals_ui_no_files(client: AsyncClient) -> None:
    """POST /pipeline/proposals with no ANALYZED files returns HTML with zero count."""
    response = await client.post("/pipeline/proposals")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_enqueue_analysis_background(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/analyze enqueues a complete ProcessFilePayload in the background."""
    session.add(_make_file(state=FileState.DISCOVERED))
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    # Verify the enqueue was called (background task may complete by now)
    assert response.json()["enqueued"] == 1

    await _drain_background()
    assert len(capture) == 1
    queue_name, task_name, kwargs = capture[0]
    assert queue_name == "phaze-agent-nox"
    assert task_name == "process_file"
    # Complete payload -- all five ProcessFilePayload fields, not just file_id.
    assert set(kwargs) == {"file_id", "original_path", "file_type", "agent_id", "models_path"}
    ProcessFilePayload.model_validate(kwargs)


@pytest.mark.asyncio
async def test_enqueue_proposals_background(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/proposals/generate enqueues batched jobs in background."""
    files = []
    related = []
    for _ in range(5):
        file_rec, analysis, metadata = _make_file_with_convergence(state=FileState.ANALYZED)
        files.append(file_rec)
        related.extend([analysis, metadata])
    session.add_all(files)
    await session.flush()
    session.add_all(related)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/api/v1/proposals/generate")
    assert response.status_code == 200
    data = response.json()
    assert data["total_files"] == 5
    assert data["enqueued_batches"] == 1  # 5 files / 10 batch_size = 1 batch

    await _drain_background()
    assert [(q, t) for q, t, _ in capture] == [("controller", "generate_proposals")]


@pytest.mark.asyncio
async def test_extract_metadata_enqueues(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/extract-metadata enqueues extract_file_metadata onto phaze-agent-nox."""
    session.add_all([_make_file(state=FileState.DISCOVERED) for _ in range(3)])
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/extract-metadata")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 3

    await _drain_background()
    assert len(capture) == 3
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-nox", "extract_file_metadata")}


@pytest.mark.asyncio
async def test_extract_metadata_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/extract-metadata with files but no active agent surfaces empty-state."""
    session.add_all([_make_file(state=FileState.DISCOVERED) for _ in range(3)])
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/api/v1/extract-metadata")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0
    assert "no active agent" in data["message"].lower()

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_extract_metadata_no_files(client: AsyncClient) -> None:
    """POST /api/v1/extract-metadata with no music files returns enqueued=0."""
    response = await client.post("/api/v1/extract-metadata")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0


@pytest.mark.asyncio
async def test_trigger_extraction_ui_with_files(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/extract-metadata enqueues extract_file_metadata onto phaze-agent-nox."""
    session.add_all([_make_file(state=FileState.DISCOVERED) for _ in range(2)])
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/pipeline/extract-metadata")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "metadata extraction" in response.text

    await _drain_background()
    assert len(capture) == 2
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-nox", "extract_file_metadata")}


@pytest.mark.asyncio
async def test_trigger_extraction_ui_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/extract-metadata with files but no active agent renders the empty-state."""
    session.add_all([_make_file(state=FileState.DISCOVERED) for _ in range(2)])
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/pipeline/extract-metadata")
    assert response.status_code == 200
    assert "No active agent available" in response.text

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_trigger_extraction_ui_no_files(client: AsyncClient) -> None:
    """POST /pipeline/extract-metadata with no music files returns HTML with zero count."""
    response = await client.post("/pipeline/extract-metadata")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_trigger_fingerprint_ui_with_files(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/fingerprint enqueues fingerprint_file onto phaze-agent-nox."""
    session.add_all([_make_file(state=FileState.METADATA_EXTRACTED) for _ in range(2)])
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/pipeline/fingerprint")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "fingerprinting" in response.text

    await _drain_background()
    assert len(capture) == 2
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-nox", "fingerprint_file")}


@pytest.mark.asyncio
async def test_trigger_fingerprint_ui_enqueues_failed_retry_file(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 42: /pipeline/fingerprint now ALSO enqueues a failed-fingerprint-retry file (D-03 align).

    Previously trigger_fingerprint_ui queried ONLY METADATA_EXTRACTED; routing it through the shared
    get_fingerprint_pending_files helper aligns it with the API endpoint -- it GAINS the failed-retry
    scope. A file in ANALYZED state (NOT METADATA_EXTRACTED, NOT FINGERPRINTED) carrying a failed
    FingerprintResult must now be enqueued. This locks the intended consistency fix.
    """
    failed = _make_file(state=FileState.ANALYZED)
    session.add(failed)
    await session.flush()
    session.add(FingerprintResult(id=uuid.uuid4(), file_id=failed.id, engine="audfprint", status="failed"))
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/pipeline/fingerprint")
    assert response.status_code == 200

    await _drain_background()
    assert len(capture) == 1
    queue_name, task_name, payload = capture[0]
    assert (queue_name, task_name) == ("phaze-agent-nox", "fingerprint_file")
    assert payload["file_id"] == str(failed.id)


@pytest.mark.asyncio
async def test_trigger_fingerprint_ui_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/fingerprint with files but no active agent renders the empty-state."""
    session.add_all([_make_file(state=FileState.METADATA_EXTRACTED) for _ in range(2)])
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/pipeline/fingerprint")
    assert response.status_code == 200
    assert "No active agent available" in response.text

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_trigger_fingerprint_ui_no_files(client: AsyncClient) -> None:
    """POST /pipeline/fingerprint with no eligible files returns HTML with zero count."""
    response = await client.post("/pipeline/fingerprint")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# PR4: dashboard activity indicator (green pulse / amber "stalled?")
# ---------------------------------------------------------------------------


async def _seed_running_scan(session: AsyncSession, *, seconds_quiet: int, scan_path: str) -> uuid.UUID:
    """Seed a RUNNING ScanBatch whose heartbeat is `seconds_quiet` seconds old."""
    from datetime import timedelta

    batch_id = uuid.uuid4()
    batch = ScanBatch(
        id=batch_id,
        agent_id=LEGACY_AGENT_ID,
        scan_path=scan_path,
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
        last_progress_at=datetime.now(UTC) - timedelta(seconds=seconds_quiet),
    )
    session.add(batch)
    await session.commit()
    return batch_id


@pytest.mark.asyncio
async def test_dashboard_renders_green_pulse_for_progressing_running_scan(client: AsyncClient, session: AsyncSession) -> None:
    """A fresh RUNNING scan renders the green pulsing dot + '·Ns ago' affordance."""
    await _seed_running_scan(session, seconds_quiet=5, scan_path="/music/fresh")
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    assert "animate-pulse" in response.text
    assert "s ago" in response.text
    # Not stalled -> no amber warning label.
    assert "stalled?" not in response.text


@pytest.mark.asyncio
async def test_dashboard_renders_amber_stalled_for_quiet_running_scan(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RUNNING scan quiet past the UI warn threshold renders 'stalled?'.

    The default scan_stall_seconds is now 86400 (24h); this test pins it to 600
    for determinism so the warn threshold is half of 600 -> 300s (400s quiet > 300s).
    """
    from phaze.config import get_settings
    from phaze.routers import pipeline_scans

    pinned = get_settings().model_copy(update={"scan_stall_seconds": 600})
    monkeypatch.setattr(pipeline_scans, "get_settings", lambda: pinned)

    await _seed_running_scan(session, seconds_quiet=400, scan_path="/music/quiet")
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    assert "stalled?" in response.text
    assert "text-amber-600" in response.text


@pytest.mark.asyncio
async def test_dashboard_attaches_activity_attrs(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard handler attaches _seconds_since_progress and _is_stalled per row.

    The default scan_stall_seconds is now 86400 (24h); this test pins it to 600
    for determinism so the warn threshold is half of 600 -> 300s (400s quiet > 300s).
    """
    from phaze.config import get_settings
    from phaze.routers import pipeline_scans
    from phaze.routers.pipeline import dashboard

    pinned = get_settings().model_copy(update={"scan_stall_seconds": 600})
    monkeypatch.setattr(pipeline_scans, "get_settings", lambda: pinned)

    await _seed_running_scan(session, seconds_quiet=400, scan_path="/music/attrs")
    # Invoke the handler body directly via a tiny request stub is heavy; instead
    # assert through the rendered output that both transient attrs were consumed:
    # _seconds_since_progress drives the "Ns ago" text and _is_stalled drives the
    # amber label. Their presence proves the attach loop ran.
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    assert "stalled?" in response.text  # _is_stalled True path
    assert dashboard is not None  # handler import smoke-check


# ---------------------------------------------------------------------------
# Phase 34 Plan 02: queue-activity surfaced through both contexts + degrade-to-200
# (VALIDATION 34-02-01). The client fixture skips the lifespan, so app.state queue
# handles are ABSENT until a test wires fakes — proving get_queue_activity's
# missing-attr degrade keeps BOTH the 5s poll and the full-page render alive.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_stats_degrades_without_queues(client: AsyncClient, session: AsyncSession) -> None:
    """No fakes wired (app.state queues absent) → /pipeline/stats AND /pipeline/ stay 200.

    Proves the get_queue_activity AttributeError degrade path keeps both the poll and the
    full-page render from 500ing when the queue handles are missing (a Redis outage degrades
    identically). This is the no-500-regression guard for the new wiring.
    """
    stats_response = await client.get("/pipeline/stats")
    assert stats_response.status_code == 200

    dashboard_response = await client.get("/pipeline/")
    assert dashboard_response.status_code == 200


@pytest.mark.asyncio
async def test_pipeline_stats_surfaces_agent_busy(client: AsyncClient, session: AsyncSession) -> None:
    """/pipeline/stats re-seeds $store.pipeline.agentBusy/controllerBusy from live queue depth.

    Wires fake queues, seeds the agent queue depth (4 queued + 1 active = 5) and the
    controller queue (2 queued + 0 active = 2), then asserts the OOB store-write substrings
    carry the SUMMED busy counts the buttons gate on.
    """
    await seed_active_agent(session, "nox")
    controller_queue, task_router = install_fake_queues(client)
    task_router.set_counts("nox", queued=4, active=1)
    controller_queue.set_counts(queued=2, active=0)

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    assert "$store.pipeline.agentBusy = 5" in response.text
    assert "$store.pipeline.controllerBusy = 2" in response.text
    # The Fingerprint button's ready-count gate (metadataExtracted) must ALSO re-seed on
    # each poll like discovered/analyzed, so it un-disables live instead of only on full reload.
    assert 'id="fingerprint-files-ready" hx-swap-oob="true"' in response.text
    assert "$store.pipeline.metadataExtracted = 0" in response.text


@pytest.mark.asyncio
async def test_dashboard_seeds_busy_on_first_load(client: AsyncClient, session: AsyncSession) -> None:
    """/pipeline/ initial render does not 500 with queues wired (seeds counts on first load)."""
    await seed_active_agent(session, "nox")
    controller_queue, task_router = install_fake_queues(client)
    task_router.set_counts("nox", queued=4, active=1)
    controller_queue.set_counts(queued=2, active=0)

    response = await client.get("/pipeline/")
    assert response.status_code == 200


def test_queue_progress_percent_formula() -> None:
    """queue_progress_percent is analyzed / (analyzed + agent_busy) * 100, divide-by-zero guarded.

    (30, 10) → 75 proves the numerator is analyzed and the denominator is analyzed+agent_busy
    (a reversed ratio would yield 25). (0, 0) → 0 proves the divide-by-zero guard. (11428, 0)
    → 100 proves a fully-analyzed archive reports complete.
    """
    from phaze.services.pipeline import queue_progress_percent

    assert queue_progress_percent(30, 10) == 75
    assert queue_progress_percent(0, 0) == 0
    assert queue_progress_percent(11428, 0) == 100
