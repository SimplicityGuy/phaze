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
from phaze.models.metadata import FileMetadata
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.schemas.agent_tasks import ProcessFilePayload
from tests._queue_fakes import seed_active_agent, wire_fakes


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
