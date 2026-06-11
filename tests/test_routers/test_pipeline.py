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
async def test_analyze_enqueues_bounded_timeout_and_retries(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 31: POST /api/v1/analyze enqueues process_file with timeout=14400 + retries=2.

    Restart-resilience amendment: a bounded-generous 4h timeout (exceeds the longest
    legitimate set, spike 31-01) lets SAQ reclaim a dead/restarted worker's in-flight
    job, while retries=2 stays in the locked 1-2 band so apply_project_job_defaults
    does NOT clobber it to worker_max_retries (the retries==1 -> 4 churn).
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
    assert queue.captured_policy[0] == {"key": expected_key, "timeout": 14400, "retries": 2}
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
    """Phase 31: the HTMX /pipeline/analyze path also enqueues with timeout=14400 + retries=2."""
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
    assert queue.captured_policy[0] == {"key": expected_key, "timeout": 14400, "retries": 2}


@pytest.mark.asyncio
async def test_process_file_enqueue_policy_survives_project_defaults_hook() -> None:
    """The before_enqueue hook leaves the explicit timeout=14400 / retries=2 intact.

    apply_project_job_defaults only overrides a Job still at the SAQ defaults
    (timeout==10, retries==1). An explicit retries=2 (and timeout=14400) is honored,
    proving the process_file enqueue escapes the retries==1 -> worker_max_retries clobber.
    """
    from saq import Job

    from phaze.tasks._shared.queue_defaults import apply_project_job_defaults

    job = Job(function="process_file", timeout=14400, retries=2)
    await apply_project_job_defaults(job)
    assert job.timeout == 14400
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
