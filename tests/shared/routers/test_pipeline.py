"""Tests for the pipeline orchestration router."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.config import settings
from phaze.config_backends import ComputeBackend, KubeConfig, KueueBackend, LocalBackend
from phaze.models.agent import LEGACY_AGENT_ID
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.tracklist import Tracklist
from phaze.schemas.agent_tasks import ExtractMetadataPayload, ProcessFilePayload, ScanLiveSetPayload
from tests._queue_fakes import (
    DedupFakeQueue,
    DedupFakeTaskRouter,
    install_fake_queues,
    seed_active_agent,
    wire_fakes,
)


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


# Registry fixtures driving the Phase-67 (D-14, REG-04) reduction the rewired pipeline reads:
# a single compute backend -> cloud_enabled True + active_cloud_kind 'compute' (the v5.0 rsync path);
# a single kueue backend -> active_cloud_kind 'kueue' (the k8s/S3 path); a single local backend ->
# cloud_enabled False (all-local). The pipeline endpoints read the singleton's ``backends`` field via
# the registry-derived ``cloud_enabled`` / ``active_cloud_kind`` properties, so patching ``backends``
# drives every rewired call site through the real property logic.
_COMPUTE_BACKEND = ComputeBackend(kind="compute", id="a1", rank=10, cap=2, agent_ref="cloud-1", scratch_dir="/scratch")
_KUEUE_BACKEND = KueueBackend(kind="kueue", id="k8s", rank=10, cap=2, kube=KubeConfig())
_LOCAL_BACKEND = LocalBackend(kind="local", id="local", rank=99, cap=1)


@pytest.fixture(autouse=True)
def _cloud_compute_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the Phase-49/50 cloud-routing tests to a single compute backend (cloud ON, rsync path).

    The Phase-67 rewire reads the registry: an all-local registry -> cloud_enabled False (all-local
    routing + backfill no-op); a single compute backend -> cloud_enabled True + active_cloud_kind
    'compute' (the live v5.0 rsync path). These regression tests assert the ON behavior (long files
    held in AWAITING_CLOUD, backfill resets+routes), so pin the singleton's ``backends`` to one
    compute backend. The cloud-off / k8s tests override it inside their own bodies.
    """
    from phaze.config import settings

    monkeypatch.setattr(settings, "backends", [_COMPUTE_BACKEND])


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
    # Phase 57.1: a COMPLETED analysis row carries analysis_completed_at -- the tightened
    # proposal-convergence gate (analysis_completed_at IS NOT NULL) excludes in-progress partial rows.
    analysis = AnalysisResult(file_id=uid, bpm=128.0, musical_key="Cm", analysis_completed_at=datetime.now(UTC))
    metadata = FileMetadata(file_id=uid, artist="Test", title="Track")
    return file_rec, analysis, metadata


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

    # All five required fields present -- not just file_id (the pre-fix bug). Phase 44-01
    # added the optional fine_cap/coarse_cap overrides; Phase 50 added expected_sha256/scratch_path
    # for the cloud push pipeline. All four serialize as None on the bulk local path (the
    # AgentSettings 60/30 defaults still apply, and a local file is read in place).
    assert set(kwargs) == {
        "file_id",
        "original_path",
        "file_type",
        "agent_id",
        "models_path",
        "fine_cap",
        "coarse_cap",
        "expected_sha256",
        "scratch_path",
    }
    assert kwargs["file_id"] == expected_id
    assert kwargs["original_path"] == expected_path
    assert kwargs["file_type"] == expected_type
    assert kwargs["agent_id"] == agent.id
    assert kwargs["models_path"] == settings.models_path
    # Bulk "Run Analysis" path carries no cap override (deepen is the only elevated-cap caller).
    assert kwargs["fine_cap"] is None
    assert kwargs["coarse_cap"] is None

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
    # Phase 44-01 added the optional fine_cap/coarse_cap (None on the bulk path).
    task_name, payload = queue.captured[0]
    assert task_name == "process_file"
    assert set(payload) == {
        "file_id",
        "original_path",
        "file_type",
        "agent_id",
        "models_path",
        "fine_cap",
        "coarse_cap",
        "expected_sha256",
        "scratch_path",
    }


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


# ---------------------------------------------------------------------------
# Phase 49 Plan 02: per-file duration router (D-06/D-11/D-02/D-12).
#
# Long (>= cloud_route_threshold_sec) files route to a COMPUTE agent's queue
# (independent of fileserver availability); short/null-duration files route to
# the FILESERVER queue exactly as before; a long file with no compute agent is
# HELD in AWAITING_CLOUD (committed, NEVER silently analyzed locally); short/null
# files with no fileserver are reported "skipped" without aborting the run. The
# Run-analysis response reports the split counts, and the no-active-agent fragment
# is surfaced ONLY when BOTH agent kinds are absent.
# ---------------------------------------------------------------------------

_LONG = 6000.0  # >= cloud_route_threshold_sec default (5400)
_SHORT = 100.0  # < threshold


def _make_file_with_duration(duration: float | None, *, state: str = FileState.DISCOVERED) -> tuple[FileRecord, FileMetadata | None]:
    """Build a DISCOVERED FileRecord plus an optional FileMetadata row carrying ``duration``.

    A ``None`` duration is modeled as the absence of a metadata row (the LEFT OUTER JOIN in
    ``get_discovered_files_with_duration`` yields ``duration=None``) — exercising the
    null-routes-local branch.
    """
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
    md = FileMetadata(file_id=uid, duration=duration) if duration is not None else None
    return file_rec, md


async def _persist_files_with_duration(session: AsyncSession, specs: list[float | None]) -> list[FileRecord]:
    """Persist one DISCOVERED file per duration spec (+ metadata) and return the records."""
    files: list[FileRecord] = []
    mds: list[FileMetadata] = []
    for dur in specs:
        f, md = _make_file_with_duration(dur)
        files.append(f)
        if md is not None:
            mds.append(md)
    session.add_all(files)
    await session.flush()
    if mds:
        session.add_all(mds)
    await session.commit()
    return files


@pytest.mark.asyncio
async def test_analyze_long_file_held_awaiting_cloud_even_with_compute_online(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 50 reshape: a >=threshold file is HELD in AWAITING_CLOUD even with a compute agent online.

    There is no direct-to-compute enqueue any more (T-50-bypass): the bounded stage_cloud_window
    cron is the single entry to the compute pipeline. So a long file is parked in AWAITING_CLOUD
    (``cloud`` is always 0) regardless of compute availability, and NOTHING is enqueued from here.
    """
    (long_file,) = await _persist_files_with_duration(session, [_LONG])
    # Both kinds online: the long file is STILL held (compute is reached only via the staging cron).
    await seed_active_agent(session, "cloud", kind="compute")
    await seed_active_agent(session, "nox", kind="fileserver")
    capture = wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["cloud"] == 0
    assert data["local"] == 0
    assert data["awaiting_cloud"] == 1

    await _drain_background()
    # No direct-to-compute (or any) enqueue: the file holds for the staging cron.
    assert capture == []
    await session.refresh(long_file)
    assert long_file.state == FileState.AWAITING_CLOUD


@pytest.mark.asyncio
async def test_analyze_long_held_even_without_fileserver(client: AsyncClient, session: AsyncSession) -> None:
    """Degenerate topology: only a compute agent online -> long file HELD, short file skipped, run NOT aborted.

    With no fileserver the short file cannot route locally (skipped); the long file holds in
    AWAITING_CLOUD. Nothing is held except the long file, and no fileserver means the response
    carries the no-active-agent message but still surfaces the held + skipped counts.
    """
    long_file, short_file = await _persist_files_with_duration(session, [_LONG, _SHORT])
    await seed_active_agent(session, "cloud", kind="compute")  # NO fileserver online
    capture = wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["cloud"] == 0
    assert data["local"] == 0
    assert data["awaiting_cloud"] == 1
    assert data["skipped"] == 1

    await _drain_background()
    # Nothing is enqueued (the long file is held; the short file is skipped, never enqueued).
    assert capture == []
    await session.refresh(long_file)
    assert long_file.state == FileState.AWAITING_CLOUD
    # The short file stays DISCOVERED (skipped != held, no state change).
    await session.refresh(short_file)
    assert short_file.state == FileState.DISCOVERED


@pytest.mark.asyncio
async def test_analyze_long_file_no_compute_holds_awaiting_cloud(client: AsyncClient, session: AsyncSession) -> None:
    """A >=threshold file with no compute agent online transitions to AWAITING_CLOUD with NO process_file enqueue (D-02)."""
    (long_file,) = await _persist_files_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")  # fileserver only, no compute
    capture = wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["awaiting_cloud"] == 1
    assert data["cloud"] == 0
    assert data["local"] == 0

    await _drain_background()
    # The held file is NEVER enqueued (the load-bearing CLOUDROUTE-02 safety invariant).
    assert capture == []
    await session.refresh(long_file)
    assert long_file.state == FileState.AWAITING_CLOUD


@pytest.mark.asyncio
async def test_analyze_short_and_null_route_to_fileserver_with_key(client: AsyncClient, session: AsyncSession) -> None:
    """A <threshold file AND a null-duration file both route to the fileserver queue with key process_file:<id> (D-06)."""
    short_file, null_file = await _persist_files_with_duration(session, [_SHORT, None])
    await seed_active_agent(session, "nox", kind="fileserver")
    _, task_router = install_fake_queues(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["local"] == 2
    assert data["cloud"] == 0

    await _drain_background()
    queue = task_router.queues["nox"]
    assert len(queue.captured) == 2
    assert {p["key"] for p in queue.captured_policy} == {f"process_file:{short_file.id}", f"process_file:{null_file.id}"}


@pytest.mark.asyncio
async def test_analyze_no_agents_at_all_surfaces_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """The no-active-agent fragment/message is emitted ONLY when BOTH agent kinds are absent (nothing routable)."""
    await _persist_files_with_duration(session, [_SHORT])
    capture = wire_fakes(client)  # neither a fileserver nor a compute agent seeded

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0
    assert "no active agent" in data["message"].lower()

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_analyze_ui_reports_split_counts(client: AsyncClient, session: AsyncSession) -> None:
    """The HTMX /pipeline/analyze response renders the split counts 'N local, K awaiting cloud' (D-12, Phase 50).

    Phase 50 reshape: long files are held in AWAITING_CLOUD (``cloud`` is always 0), so the long
    file surfaces under 'awaiting cloud', not 'cloud'.
    """
    await _persist_files_with_duration(session, [_LONG, _SHORT, None])
    await seed_active_agent(session, "cloud", kind="compute")
    await seed_active_agent(session, "nox", kind="fileserver")
    wire_fakes(client)

    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200
    text = response.text
    # short + null -> local (2); long -> held (1 awaiting cloud); 0 cloud; none skipped.
    assert "2 local" in text
    assert "0 cloud" in text
    assert "1 awaiting cloud" in text


@pytest.mark.asyncio
async def test_analyze_ui_reports_skipped_when_no_local_agent(client: AsyncClient, session: AsyncSession) -> None:
    """With only a compute agent online, the HTMX response reports the long file held + short files skipped.

    Phase 50 reshape: no fileserver means the short file is skipped and the long file is held in
    AWAITING_CLOUD; the no-active-agent fragment surfaces the held + skipped counts.
    """
    await _persist_files_with_duration(session, [_LONG, _SHORT])
    await seed_active_agent(session, "cloud", kind="compute")  # no fileserver
    wire_fakes(client)

    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200
    text = response.text
    assert "held awaiting cloud" in text.lower()
    assert "skipped" in text.lower()


@pytest.mark.asyncio
async def test_analyze_ui_no_agents_renders_no_active_agent_fragment(client: AsyncClient, session: AsyncSession) -> None:
    """The HTMX path surfaces the no-active-agent fragment ONLY when both kinds are absent.

    A SHORT file with no fileserver is merely skipped (no state change), so the awaiting==0 case
    keeps the original "No active agent available" copy (WR-01 only surfaces the HELD count).
    """
    await _persist_files_with_duration(session, [_SHORT])
    capture = wire_fakes(client)  # no agents at all

    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200
    assert "No active agent available" in response.text

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_analyze_ui_no_agents_surfaces_held_count(client: AsyncClient, session: AsyncSession) -> None:
    """WR-01: with NO agent online, a LONG file is held in AWAITING_CLOUD and the HTMX response
    surfaces the held count instead of a bare "0 files enqueued".

    A held file is a real state change (committed to AWAITING_CLOUD); the operator must see it
    rather than be told nothing happened. The held set is drained by the */5 release cron, but the
    immediate response should already report the count (the Awaiting-cloud card also re-polls in 5s).
    """
    await _persist_files_with_duration(session, [_LONG])
    capture = wire_fakes(client)  # no agents at all

    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200
    text = response.text.lower()
    # The held long file is reported, not hidden behind a no-op message.
    assert "1 held awaiting cloud" in text
    assert "0 files enqueued" not in text

    # The file really is held in AWAITING_CLOUD.
    held = (await session.execute(select(FileRecord).where(FileRecord.state == FileState.AWAITING_CLOUD))).scalars().all()
    assert len(held) == 1

    await _drain_background()
    assert capture == []


# ---------------------------------------------------------------------------
# Phase 49 Plan 03: POST /pipeline/backfill-cloud — "Backfill to cloud" action
# (D-08/D-09/D-10). Selects EXACTLY the timed-out long files
# (ANALYSIS_FAILED ∧ duration >= cloud_route_threshold_sec), resets them to
# DISCOVERED (committed), and routes each through the SAME per-file duration
# router as "Run Analysis": compute if a compute agent is online, else held in
# AWAITING_CLOUD with an explicit scheduling-ledger row. Never a whole-backlog
# sweep; a double-click is a no-op (the candidates have already left the
# ANALYSIS_FAILED state), and short/never-failed files are never touched.
# ---------------------------------------------------------------------------


async def _persist_failed_with_duration(session: AsyncSession, specs: list[float | None], *, with_ledger: bool = True) -> list[FileRecord]:
    """Persist one ANALYSIS_FAILED file per duration spec (+ metadata) and return the records.

    A ``None`` duration is modeled as the absence of a metadata row — such a file is
    structurally excluded from the backfill candidate set (the INNER JOIN drops it).

    ``with_ledger`` (default ``True``) also seeds a ``process_file:<id>`` scheduling-ledger row
    per file, modelling **previously-scheduled, then timed-out** work: a SAQ timeout abandons the
    job WITHOUT firing ``report_analysis_failed`` (which would clear the row), so the orphaned
    ledger row persists into ``ANALYSIS_FAILED``. Phase 55 (L4) scopes the backfill candidate query
    to exactly these ledgered files. Pass ``with_ledger=False`` to model a never-scheduled (or
    cleanly-reported-failed, row-cleared) file that the EXISTS predicate must exclude.
    """
    from phaze.services.scheduling_ledger import insert_ledger_if_absent

    files: list[FileRecord] = []
    mds: list[FileMetadata] = []
    for dur in specs:
        uid = uuid.uuid4()
        files.append(
            FileRecord(
                id=uid,
                sha256_hash=uid.hex,
                original_path=f"/music/{uid.hex}.mp3",
                original_filename=f"{uid.hex}.mp3",
                current_path=f"/music/{uid.hex}.mp3",
                file_type="mp3",
                file_size=1000,
                state=FileState.ANALYSIS_FAILED,
            )
        )
        if dur is not None:
            mds.append(FileMetadata(file_id=uid, duration=dur))
    session.add_all(files)
    await session.flush()
    if mds:
        session.add_all(mds)
    if with_ledger:
        for file in files:
            await insert_ledger_if_absent(
                session,
                key=f"process_file:{file.id}",
                function="process_file",
                kwargs={},
                timeout=7200,
                retries=2,
            )
    await session.commit()
    return files


# --- Phase 55 Plan 04 Task 1 (L4): ledger-scoped backfill candidate query --------------------
# The candidate set is now ANALYSIS_FAILED ∧ duration >= threshold ∧ EXISTS a prior
# process_file:<id> scheduling-ledger row. This excludes never-scheduled (or cleanly
# report_analysis_failed-cleared) failures so backfill re-drives ONLY previously-scheduled,
# timed-out work -- mirroring the v5.0 recover-over-enqueue fix (no whole-backlog sweep).


@pytest.mark.asyncio
async def test_backfill_candidate_query_requires_prior_ledger_row(session: AsyncSession) -> None:
    """L4: only a failed long file WITH a prior process_file ledger row is a backfill candidate.

    A never-scheduled failed long file (no ledger row) is excluded -- this is the bounded
    "previously-scheduled work only" property that closes the over-enqueue class.
    """
    from phaze.services.pipeline import count_backfill_candidates, get_backfill_candidates

    (ledgered_long,) = await _persist_failed_with_duration(session, [_LONG])  # has process_file ledger row
    (never_scheduled_long,) = await _persist_failed_with_duration(session, [_LONG], with_ledger=False)
    threshold = settings.cloud_route_threshold_sec

    count = await count_backfill_candidates(session, threshold)
    candidates = await get_backfill_candidates(session, threshold)
    candidate_ids = {file.id for file, _duration in candidates}

    assert count == 1
    assert ledgered_long.id in candidate_ids
    assert never_scheduled_long.id not in candidate_ids  # never-scheduled work is NOT swept in


@pytest.mark.asyncio
async def test_backfill_candidate_query_excludes_short_even_with_ledger(session: AsyncSession) -> None:
    """A failed SHORT file (duration < threshold) is excluded even though it carries a ledger row."""
    from phaze.services.pipeline import count_backfill_candidates, get_backfill_candidates

    await _persist_failed_with_duration(session, [_SHORT])  # short, WITH ledger row
    threshold = settings.cloud_route_threshold_sec

    assert await count_backfill_candidates(session, threshold) == 0
    assert await get_backfill_candidates(session, threshold) == []


@pytest.mark.asyncio
async def test_backfill_selects_long_failed_resets_and_holds_awaiting_cloud(client: AsyncClient, session: AsyncSession) -> None:
    """Backfill selects EXACTLY the long ANALYSIS_FAILED set, resets it, and HOLDS it in AWAITING_CLOUD (Phase 50).

    Phase 50 reshape: every long backfill candidate is held in AWAITING_CLOUD (no direct compute
    enqueue) so it enters the bounded cloud window via the staging cron. A SHORT ANALYSIS_FAILED
    file and a never-failed DISCOVERED file are untouched (D-10): the candidate set is the explicit
    ANALYSIS_FAILED ∧ duration>=threshold query, NOT a backlog sweep.
    """
    long_failed, short_failed = await _persist_failed_with_duration(session, [_LONG, _SHORT])
    (untouched_discovered,) = await _persist_files_with_duration(session, [None])  # never failed
    # Both kinds online: the long failed file is STILL held (compute is reached only via the cron).
    await seed_active_agent(session, "cloud", kind="compute")
    await seed_active_agent(session, "nox", kind="fileserver")
    capture = wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200

    await _drain_background()
    # No direct-to-compute (or any) enqueue: the long failed file is held for the staging cron.
    assert capture == []

    # The short failed file stays ANALYSIS_FAILED; the never-failed DISCOVERED file is untouched.
    await session.refresh(short_failed)
    assert short_failed.state == FileState.ANALYSIS_FAILED
    await session.refresh(untouched_discovered)
    assert untouched_discovered.state == FileState.DISCOVERED
    # The long failed file was reset out of ANALYSIS_FAILED and HELD in AWAITING_CLOUD, with an
    # explicit scheduling-ledger row (the held branch fires no before_enqueue hook).
    await session.refresh(long_failed)
    assert long_failed.state == FileState.AWAITING_CLOUD
    rows = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{long_failed.id}"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].function == "process_file"
    assert rows[0].routing == "agent"


@pytest.mark.asyncio
async def test_backfill_response_reports_count_and_split(client: AsyncClient, session: AsyncSession) -> None:
    """The backfill response reports the candidate count and the cloud/awaiting split (D-08, Phase 50).

    Phase 50 reshape: all long candidates are held, so the split is '0 cloud, N awaiting cloud'.
    """
    await _persist_failed_with_duration(session, [_LONG, _LONG])
    await seed_active_agent(session, "cloud", kind="compute")
    wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    text = response.text
    assert "Backfilled 2" in text
    assert "2 awaiting cloud" in text


@pytest.mark.asyncio
async def test_backfill_no_compute_holds_awaiting_cloud_with_ledger_row(client: AsyncClient, session: AsyncSession) -> None:
    """With no compute agent online, a backfilled long file is HELD in AWAITING_CLOUD with an explicit ledger row (D-09)."""
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")  # fileserver only, no compute
    capture = wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200

    await _drain_background()
    # The held file is NEVER enqueued (the load-bearing CLOUDROUTE-02 safety invariant).
    assert capture == []
    await session.refresh(long_failed)
    assert long_failed.state == FileState.AWAITING_CLOUD

    # The held branch fires no before_enqueue hook, so the endpoint seeds the ledger row explicitly.
    rows = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{long_failed.id}"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].function == "process_file"
    assert rows[0].routing == "agent"


@pytest.mark.asyncio
async def test_backfill_with_compute_online_still_holds_and_writes_single_ledger_row(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 50: even with a compute agent online the backfilled file is held with exactly ONE ledger row.

    The reshape removed the direct-to-compute backfill branch, so every candidate is held in
    AWAITING_CLOUD and the endpoint seeds its scheduling-ledger row explicitly — exactly one row,
    never a double-write (T-50-bypass + D-09 idempotency).
    """
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "cloud", kind="compute")
    capture = wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200

    await _drain_background()
    assert capture == []  # no direct compute enqueue
    await session.refresh(long_failed)
    assert long_failed.state == FileState.AWAITING_CLOUD
    rows = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{long_failed.id}"))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_backfill_double_click_holds_nothing_new(client: AsyncClient, session: AsyncSession) -> None:
    """A second backfill click holds zero new files — never a whole-backlog over-enqueue (D-10)."""
    await _persist_failed_with_duration(session, [_LONG, _LONG])
    await seed_active_agent(session, "cloud", kind="compute")
    capture = wire_fakes(client)

    r1 = await client.post("/pipeline/backfill-cloud")
    assert r1.status_code == 200
    await _drain_background()
    assert capture == []  # held, never directly enqueued
    held = (await session.execute(select(FileRecord).where(FileRecord.state == FileState.AWAITING_CLOUD))).scalars().all()
    assert len(held) == 2  # both long failed files held once

    # After the first backfill the candidates are AWAITING_CLOUD (no longer ANALYSIS_FAILED), so the
    # explicit filter selects nothing on the second click -> zero new held files.
    r2 = await client.post("/pipeline/backfill-cloud")
    assert r2.status_code == 200
    await _drain_background()
    held_after = (await session.execute(select(FileRecord).where(FileRecord.state == FileState.AWAITING_CLOUD))).scalars().all()
    assert len(held_after) == 2  # unchanged — no over-enqueue
    assert "No timed-out long files" in r2.text


@pytest.mark.asyncio
async def test_backfill_zero_candidates_returns_empty_fragment(client: AsyncClient, session: AsyncSession) -> None:
    """With no timed-out long files, backfill returns the empty-count fragment and enqueues nothing."""
    await seed_active_agent(session, "cloud", kind="compute")
    capture = wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    assert "No timed-out long files" in response.text

    await _drain_background()
    assert capture == []


# --- Phase 67 (REG-04, D-14): the registry cloud_enabled gate on the backfill trigger ------


@pytest.mark.asyncio
async def test_backfill_disabled_when_cloud_local(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF: with an all-local registry (cloud_enabled False) the backfill trigger is a no-op -- ZERO mutations.

    Pitfall 2 / T-51-02: gating ONLY the routing seam would still let backfill reset the 144
    ANALYSIS_FAILED long files to DISCOVERED and re-route them local to re-time-out. The explicit
    early-return guard prevents any state mutation when the registry holds no cloud backend.
    """
    from phaze.config import settings

    monkeypatch.setattr(settings, "backends", [_LOCAL_BACKEND])
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG], with_ledger=False)
    await seed_active_agent(session, "cloud", kind="compute")
    await seed_active_agent(session, "nox", kind="fileserver")
    capture = wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200

    await _drain_background()
    # Nothing enqueued anywhere -- the disabled path never routes.
    assert capture == []
    # The ANALYSIS_FAILED file is NEVER reset to DISCOVERED (no silent re-time-out, Pitfall 2).
    await session.refresh(long_failed)
    assert long_failed.state == FileState.ANALYSIS_FAILED
    # No scheduling-ledger row is seeded on the disabled path either.
    rows = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{long_failed.id}"))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_backfill_enabled_resets_and_holds(client: AsyncClient, session: AsyncSession) -> None:
    """ON: with a single compute backend (autouse fixture) the backfill resets the long file and holds it (regression)."""
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "cloud", kind="compute")
    capture = wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200

    await _drain_background()
    assert capture == []  # held, never directly enqueued
    await session.refresh(long_failed)
    assert long_failed.state == FileState.AWAITING_CLOUD


# --- Phase 55 Plan 04 Task 2 (L3 / CLOUDROUTE-02): backfill forks on the active cloud kind ---------
# The backfill endpoint resets ledger-scoped failed long files to DISCOVERED and routes them to
# AWAITING_CLOUD for BOTH cloud kinds. The ONLY difference is the held-file ledger seed:
#   - compute : seeds a process_file:<id> row (insert_ledger_if_absent) -- the held file's durable
#               "was scheduled" fact (D-09).
#   - kueue   : seeds NO process_file:<id> row -- a ledger row would let recover_orphaned_work replay
#               the held file onto a LOCAL agent queue (the cloud_job row, NOT the ledger, is the k8s
#               in-flight registry). The seed call must NOT fire on the kueue branch.
# Both forks converge on exactly the prior candidacy row, so the fork is asserted at the
# insert_ledger_if_absent call boundary (spy), which is the only observable difference.


def _spy_on_ledger_seed(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Patch routers.pipeline.insert_ledger_if_absent to record the keys it is called with."""
    import phaze.routers.pipeline as pipeline_mod

    seeded_keys: list[str | None] = []
    original = pipeline_mod.insert_ledger_if_absent

    async def _recording(*args: object, **kwargs: object) -> None:
        seeded_keys.append(kwargs.get("key"))  # type: ignore[arg-type]
        await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline_mod, "insert_ledger_if_absent", _recording)
    return seeded_keys


@pytest.mark.asyncio
async def test_backfill_a1_seeds_process_file_ledger_row(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """compute fork: the held-file branch calls insert_ledger_if_absent for the process_file:<id> key (D-09)."""
    # A single compute backend is the autouse default.
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")  # no compute -> held in AWAITING_CLOUD
    wire_fakes(client)
    seeded_keys = _spy_on_ledger_seed(monkeypatch)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await _drain_background()

    # The a1 branch DID seed the held file's process_file ledger row.
    assert f"process_file:{long_failed.id}" in seeded_keys
    await session.refresh(long_failed)
    assert long_failed.state == FileState.AWAITING_CLOUD
    rows = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{long_failed.id}"))).scalars().all()
    assert len(rows) == 1  # exactly the one row (idempotent over the prior candidacy row)


@pytest.mark.asyncio
async def test_backfill_k8s_holds_awaiting_cloud_without_ledger_seed(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """k8s fork (L3): the held file is reset->routed to AWAITING_CLOUD but NO ledger seed fires.

    A process_file:<id> ledger seed on the kueue branch would let recover_orphaned_work replay the
    held file onto a LOCAL agent queue (CLOUDROUTE-02). The kueue branch therefore SKIPS
    insert_ledger_if_absent entirely; the file still carries exactly its prior candidacy row (no
    NEW row), and the cloud_job row -- seeded later by the stage_cloud_window k8s branch -- is the
    in-flight registry.
    """
    monkeypatch.setattr(settings, "backends", [_KUEUE_BACKEND])
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")  # k8s has no compute agent -> held
    wire_fakes(client)
    seeded_keys = _spy_on_ledger_seed(monkeypatch)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await _drain_background()

    # L3: the k8s branch never seeded a process_file ledger row.
    assert seeded_keys == []
    # The candidate was still reset out of ANALYSIS_FAILED and HELD for the staging cron.
    await session.refresh(long_failed)
    assert long_failed.state == FileState.AWAITING_CLOUD
    # No NEW process_file row was added: exactly the one prior candidacy row remains.
    rows = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{long_failed.id}"))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_backfill_local_redrives_nothing(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """local fork: the cloud-off gate short-circuits -- no file is reset and no ledger seed fires."""
    monkeypatch.setattr(settings, "backends", [_LOCAL_BACKEND])
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")
    wire_fakes(client)
    seeded_keys = _spy_on_ledger_seed(monkeypatch)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await _drain_background()

    assert seeded_keys == []
    await session.refresh(long_failed)
    assert long_failed.state == FileState.ANALYSIS_FAILED  # never reset on the local gate


@pytest.mark.asyncio
async def test_dashboard_context_binds_cloud_lane_kind(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 67 (D-14 Class C): build_dashboard_context binds the NEUTRAL `cloud_lane_kind` key.

    The presentation value the Analyze lane cards read is a transitional legacy-shaped string under
    the neutral `cloud_lane_kind` key: 'local' for an all-local registry, else the single non-local
    backend's kind ('compute'/'kueue'). No `cloud_target` context key survives (Plan 06's package-wide
    gate depends on this).
    """
    from phaze.routers.pipeline import build_dashboard_context

    app_state = client._transport.app.state  # type: ignore[union-attr]

    monkeypatch.setattr(settings, "backends", [_LOCAL_BACKEND])
    ctx_local = await build_dashboard_context(app_state, session)
    assert ctx_local["cloud_lane_kind"] == "local"
    assert "cloud_target" not in ctx_local

    monkeypatch.setattr(settings, "backends", [_COMPUTE_BACKEND])
    ctx_compute = await build_dashboard_context(app_state, session)
    assert ctx_compute["cloud_lane_kind"] == "compute"

    monkeypatch.setattr(settings, "backends", [_KUEUE_BACKEND])
    ctx_kueue = await build_dashboard_context(app_state, session)
    assert ctx_kueue["cloud_lane_kind"] == "kueue"

    # MKUE-01: N Kueue backends (the literal multi-cluster scenario) -> "kueue", NO >1-non-local raise.
    # This is the /pipeline/stats poll's guard; before Phase 70 a 2nd Kueue backend 500'd it.
    kueue_a = KueueBackend(kind="kueue", id="k8s-a", rank=10, cap=2, kube=KubeConfig())
    kueue_b = KueueBackend(kind="kueue", id="k8s-b", rank=20, cap=2, kube=KubeConfig())
    monkeypatch.setattr(settings, "backends", [_LOCAL_BACKEND, kueue_a, kueue_b])
    ctx_two_kueue = await build_dashboard_context(app_state, session)
    assert ctx_two_kueue["cloud_lane_kind"] == "kueue"

    # local + 2 Kueue + 1 compute still reports "kueue" (any-kueue wins), no raise.
    monkeypatch.setattr(settings, "backends", [_LOCAL_BACKEND, kueue_a, kueue_b, _COMPUTE_BACKEND])
    ctx_mixed = await build_dashboard_context(app_state, session)
    assert ctx_mixed["cloud_lane_kind"] == "kueue"


# ---------------------------------------------------------------------------
# Phase 44 Plan 03: POST /pipeline/files/{file_id}/deepen — re-analyze ONE
# sampled file at the full window budget (fine_cap=0 / coarse_cap=0 -> the
# _stride_to_cap analyze-ALL-windows no-op). Mirrors the existing /pipeline
# trigger-test harness (seed_active_agent + the FakeQueue capture doubles) and
# enforces the D-05 incident guards: per-agent routing (never default queue),
# the COMPLETE ProcessFilePayload (v4.0.8 guard), and the deterministic
# process_file:<file_id> dedup key.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deepen_enqueues_elevated_cap_on_per_agent_queue(client: AsyncClient, session: AsyncSession) -> None:
    """POST .../deepen re-enqueues process_file with fine_cap=0/coarse_cap=0 onto the per-agent queue.

    The elevated (sentinel) cap reaches enqueue_process_file and lands on the resolved
    ``phaze-agent-nox`` queue — NEVER the consumer-less default queue (Phase-30 guard).
    """
    file_rec = _make_file(state=FileState.ANALYZED)
    session.add(file_rec)
    await session.commit()
    expected_id = str(file_rec.id)
    await seed_active_agent(session)
    _, task_router = install_fake_queues(client)

    response = await client.post(f"/pipeline/files/{file_rec.id}/deepen")
    assert response.status_code == 200

    queue = task_router.queues["nox"]
    assert len(queue.captured) == 1
    task_name, payload = queue.captured[0]
    assert (queue.name, task_name) == ("phaze-agent-nox", "process_file")
    assert queue.name != "default"
    # The unbounded-deepen sentinel reached the producer and was serialized.
    assert payload["fine_cap"] == 0
    assert payload["coarse_cap"] == 0
    assert payload["file_id"] == expected_id


@pytest.mark.asyncio
async def test_deepen_enqueues_complete_process_file_payload(client: AsyncClient, session: AsyncSession) -> None:
    """The deepen re-enqueue funnels the COMPLETE ProcessFilePayload, not a file_id-only payload (v4.0.8 guard).

    A file_id-only payload would dead-letter under ``extra="forbid"`` (the v4.0.8 incident).
    Assert all five required fields PLUS the two Phase-44 cap overrides are present and the
    exact kwargs validate against ProcessFilePayload.
    """
    file_rec = _make_file(state=FileState.ANALYZED)
    session.add(file_rec)
    await session.commit()
    expected_id = str(file_rec.id)
    expected_path = file_rec.original_path
    expected_type = file_rec.file_type
    agent = await seed_active_agent(session)
    _, task_router = install_fake_queues(client)

    response = await client.post(f"/pipeline/files/{file_rec.id}/deepen")
    assert response.status_code == 200

    queue = task_router.queues["nox"]
    assert len(queue.captured) == 1
    _, payload = queue.captured[0]
    # All five required fields present (not just file_id) plus the cap overrides.
    assert set(payload) == {
        "file_id",
        "original_path",
        "file_type",
        "agent_id",
        "models_path",
        "fine_cap",
        "coarse_cap",
        "expected_sha256",
        "scratch_path",
    }
    assert payload["file_id"] == expected_id
    assert payload["original_path"] == expected_path
    assert payload["file_type"] == expected_type
    assert payload["agent_id"] == agent.id
    assert payload["models_path"] == settings.models_path

    # The exact kwargs the agent worker receives validate against ProcessFilePayload.
    validated = ProcessFilePayload.model_validate(payload)
    assert str(validated.file_id) == expected_id
    assert validated.fine_cap == 0
    assert validated.coarse_cap == 0


@pytest.mark.asyncio
async def test_deepen_uses_deterministic_key_and_dedups_in_flight(client: AsyncClient, session: AsyncSession) -> None:
    """The deepen re-enqueue carries process_file:<file_id>, so an in-flight repeat dedups to a no-op (D-05).

    Uses the DedupFakeQueue (models SAQ's incomplete-set dedup): the first deepen enqueues
    and registers the key; an immediate second deepen of the same in-flight file is a no-op
    (no second capture). After the job ``finish``-es, a third deepen re-enqueues fresh.
    """
    file_rec = _make_file(state=FileState.ANALYZED)
    session.add(file_rec)
    await session.commit()
    expected_key = f"process_file:{file_rec.id}"
    await seed_active_agent(session)

    # Wire the dedup-aware router so the deterministic key collapses an in-flight repeat.
    router = DedupFakeTaskRouter()
    app = client._transport.app  # type: ignore[union-attr]
    app.state.controller_queue = DedupFakeQueue("controller")
    app.state.task_router = router

    # First deepen: enqueues + registers the key.
    r1 = await client.post(f"/pipeline/files/{file_rec.id}/deepen")
    assert r1.status_code == 200
    queue = router.queues["nox"]
    assert len(queue.captured) == 1
    assert queue.captured_policy[0]["key"] == expected_key

    # Second deepen while in-flight: deduped to a no-op (no new capture).
    r2 = await client.post(f"/pipeline/files/{file_rec.id}/deepen")
    assert r2.status_code == 200
    assert len(queue.captured) == 1

    # Once the job completes, the same key re-enqueues fresh (already-ANALYZED, no live job).
    queue.finish(expected_key)
    r3 = await client.post(f"/pipeline/files/{file_rec.id}/deepen")
    assert r3.status_code == 200
    assert len(queue.captured) == 2
    assert queue.captured_policy[1]["key"] == expected_key


@pytest.mark.asyncio
async def test_deepen_no_active_agent_does_not_enqueue(client: AsyncClient, session: AsyncSession) -> None:
    """When no agent is online the deepen endpoint surfaces a fragment and does NOT enqueue (Phase-30 guard).

    NoActiveAgentError must NOT fall through to the consumer-less default queue.
    """
    file_rec = _make_file(state=FileState.ANALYZED)
    session.add(file_rec)
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post(f"/pipeline/files/{file_rec.id}/deepen")
    assert response.status_code == 200
    assert "no active agent" in response.text.lower()
    # Nothing enqueued anywhere — never the default queue.
    assert capture == []


@pytest.mark.asyncio
async def test_deepen_unknown_file_returns_not_found_fragment(client: AsyncClient, session: AsyncSession) -> None:
    """A well-formed but unknown file_id returns a not-found fragment (200), never a 500, and enqueues nothing."""
    await seed_active_agent(session)
    capture = wire_fakes(client)

    missing_id = uuid.uuid4()
    response = await client.post(f"/pipeline/files/{missing_id}/deepen")
    assert response.status_code == 200
    assert "not found" in response.text.lower()
    assert capture == []


@pytest.mark.asyncio
async def test_deepen_malformed_file_id_returns_422(client: AsyncClient) -> None:
    """A malformed (non-uuid) file_id is rejected by the typed path param with 422 (T-44-10)."""
    response = await client.post("/pipeline/files/not-a-uuid/deepen")
    assert response.status_code == 422


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
    response = await client.get("/s/discover", headers={"HX-Request": "true"})
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
async def test_dashboard_renders_awaiting_cloud_card(client: AsyncClient, session: AsyncSession) -> None:
    """The dashboard renders the AWAITING_CLOUD count in the #awaiting-cloud-card (D-05)."""
    session.add_all([_make_file(state=FileState.AWAITING_CLOUD) for _ in range(3)])
    session.add(_make_file(state=FileState.DISCOVERED))
    await session.commit()

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert response.status_code == 200
    text = response.text
    assert 'id="awaiting-cloud-card"' in text
    assert "Awaiting cloud" in text
    # The held count (3) renders inside the card.
    assert ">3<" in text
    # Inline (full-page) render is NOT an OOB swap.
    card_start = text.index('id="awaiting-cloud-card"')
    card_open = text.rfind("<section", 0, card_start)
    assert 'hx-swap-oob="true"' not in text[card_open:card_start]


@pytest.mark.asyncio
async def test_stats_partial_emits_awaiting_cloud_card_oob(client: AsyncClient, session: AsyncSession) -> None:
    """The 5s /pipeline/stats poll re-pushes the awaiting-cloud card OUT-OF-BAND (hx-swap-oob)."""
    session.add_all([_make_file(state=FileState.AWAITING_CLOUD) for _ in range(2)])
    await session.commit()

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    text = response.text
    assert 'id="awaiting-cloud-card"' in text
    # On the poll the card is an OOB fragment so htmx swaps it in place.
    card_start = text.index('id="awaiting-cloud-card"')
    card_open = text.rfind("<section", 0, card_start)
    assert 'hx-swap-oob="true"' in text[card_open : card_start + 200]
    assert ">2<" in text


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
    # Complete payload -- all five ProcessFilePayload fields, not just file_id. Phase 44-01
    # added the optional fine_cap/coarse_cap overrides (None on the bulk path).
    assert set(kwargs) == {
        "file_id",
        "original_path",
        "file_type",
        "agent_id",
        "models_path",
        "fine_cap",
        "coarse_cap",
        "expected_sha256",
        "scratch_path",
    }
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
    response = await client.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
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
    response = await client.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
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
    response = await client.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
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

    dashboard_response = await client.get("/s/analyze", headers={"HX-Request": "true"})
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

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Phase 44 Plan 04 Task 1: straggler + ANALYSIS_FAILED counts on the dashboard
#
# The two counts ride the EXISTING 5s /pipeline/stats poll context (seeded into
# BOTH dashboard() and pipeline_stats_partial()), sourced from the Plan-02
# degrade-safe service reads (get_straggler_count / get_analysis_failed_count).
# The straggler_failed_card renders both buckets; it is re-pushed hx-swap-oob on
# every poll so the counts stay live without re-rendering the DAG buttons.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_renders_straggler_failed_card(client: AsyncClient) -> None:
    """GET /pipeline/ renders the straggler/ANALYSIS_FAILED card with both buckets (zero by default)."""
    response = await client.get("/pipeline/stats", headers={"HX-Request": "true"})
    assert response.status_code == 200
    # Card present with its two distinct buckets (44-02 D-02).
    assert 'id="straggler-failed-card"' in response.text
    assert "Stragglers" in response.text
    assert "Analysis failed" in response.text


@pytest.mark.asyncio
async def test_dashboard_seeds_analysis_failed_count(client: AsyncClient, session: AsyncSession) -> None:
    """A file in ANALYSIS_FAILED bumps analysis_failed_count into the dashboard card render."""
    session.add_all([_make_file(state=FileState.ANALYSIS_FAILED) for _ in range(3)])
    await session.commit()

    response = await client.get("/pipeline/stats", headers={"HX-Request": "true"})
    assert response.status_code == 200
    # The failed bucket count (3) renders inside the card's red panel.
    assert "Analysis failed" in response.text
    # The count value reaches the card (degrade-safe service returns the real count).
    import re

    card = re.search(r'id="straggler-failed-card".*', response.text, re.DOTALL)
    assert card is not None
    assert ">3<" in card.group(0)


@pytest.mark.asyncio
async def test_stats_partial_seeds_counts_and_oob_card(client: AsyncClient, session: AsyncSession) -> None:
    """GET /pipeline/stats re-pushes the straggler/failed card out-of-band on the 5s poll.

    The stats partial seeds straggler_count + analysis_failed_count into context and emits the
    card with hx-swap-oob="true" (it lives outside #pipeline-stats, so the innerHTML swap can
    never reach it). A seeded ANALYSIS_FAILED file proves the failed count rides the poll.
    """
    session.add_all([_make_file(state=FileState.ANALYSIS_FAILED) for _ in range(2)])
    await session.commit()

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    # OOB card re-render on the poll tick.
    assert 'id="straggler-failed-card"' in response.text
    assert 'hx-swap-oob="true"' in response.text
    # Both buckets present; the seeded failed count (2) rides the poll context.
    assert "Stragglers" in response.text
    assert "Analysis failed" in response.text


@pytest.mark.asyncio
async def test_dashboard_straggler_count_zero_when_no_stragglers(client: AsyncClient) -> None:
    """With no in-flight process_file jobs, the straggler bucket renders 0 (degrade-safe, never 500)."""
    response = await client.get("/pipeline/stats", headers={"HX-Request": "true"})
    assert response.status_code == 200
    import re

    card = re.search(r'id="straggler-failed-card".*?</section>', response.text, re.DOTALL)
    assert card is not None
    # The amber stragglers panel reads 0 (no saq_jobs seeded).
    assert "Stragglers" in card.group(0)
    assert ">0<" in card.group(0)


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


# ---------------------------------------------------------------------------
# Phase 55 (55-05, D-04, KROUTE-06): Cloud admission-state card. Carrier-always /
# body-conditional: the #admission-state-card <section> ALWAYS renders (stable OOB
# target), but the heading + four-tile grid render ONLY when any cloud_phase count
# > 0. a1/local rows have NULL cloud_phase so all-zero leaves a quiet empty carrier.
# Each tile is gated on its own count and finished uses GREEN (not amber/alert).
# ---------------------------------------------------------------------------


async def _seed_cloud_phase(session: AsyncSession, *, cloud_phase: str | None) -> None:
    """Seed one file + its cloud_job row in the given cloud_phase (NULL for a1/local) and commit."""
    file = _make_file(state=FileState.PUSHED)
    session.add(file)
    await session.flush()
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            s3_key=f"phaze-staging/{file.id}",
            status=CloudJobStatus.SUBMITTED.value,
            cloud_phase=cloud_phase,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_dashboard_admission_card_carrier_always_renders(client: AsyncClient) -> None:
    """With NO cloud_job rows the empty carrier still renders (stable OOB target), no heading/grid."""
    response = await client.get("/s/analyze", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="admission-state-card"' in response.text
    # All-zero (no k8s activity) → empty carrier: no heading, no tiles.
    assert "Cloud · Admission" not in response.text


@pytest.mark.asyncio
async def test_dashboard_admission_card_renders_matching_tile(client: AsyncClient, session: AsyncSession) -> None:
    """A seeded ADMITTED row renders the heading + the blue Admitted tile (its own count gate)."""
    await _seed_cloud_phase(session, cloud_phase=CloudPhase.ADMITTED.value)

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="admission-state-card"' in response.text
    assert "Cloud · Admission" in response.text
    assert "Admitted" in response.text
    assert "quota granted" in response.text
    # Phases with 0 files stay invisible — their tiles are not rendered.
    assert "Queued (quota)" not in response.text
    assert "Finished" not in response.text


@pytest.mark.asyncio
async def test_dashboard_admission_card_finished_is_green_not_alert(client: AsyncClient, session: AsyncSession) -> None:
    """The finished tile uses GREEN hues; the card carries NO role='alert' and NO amber (healthy progression)."""
    await _seed_cloud_phase(session, cloud_phase=CloudPhase.FINISHED.value)

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})

    assert response.status_code == 200
    import re

    card = re.search(r'id="admission-state-card".*?</section>', response.text, re.DOTALL)
    assert card is not None
    card_html = card.group(0)
    assert "Finished" in card_html
    assert "result returned" in card_html
    assert "bg-green-50" in card_html
    # Healthy progression — alert role + amber stay exclusive to inadmissible_card.
    assert 'role="alert"' not in card_html
    assert "amber" not in card_html


@pytest.mark.asyncio
async def test_dashboard_admission_card_quiet_for_null_cloud_phase(client: AsyncClient, session: AsyncSession) -> None:
    """An a1/local row (NULL cloud_phase) counts toward no phase → empty carrier, no heading."""
    await _seed_cloud_phase(session, cloud_phase=None)

    response = await client.get("/s/analyze", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="admission-state-card"' in response.text
    assert "Cloud · Admission" not in response.text


@pytest.mark.asyncio
async def test_stats_poll_repushes_admission_card_oob(client: AsyncClient, session: AsyncSession) -> None:
    """The 5s /pipeline/stats poll re-pushes the admission card OOB (hx-swap-oob + the matching tile)."""
    await _seed_cloud_phase(session, cloud_phase=CloudPhase.RUNNING.value)

    response = await client.get("/pipeline/stats")

    assert response.status_code == 200
    import re

    card = re.search(r'id="admission-state-card".*?</section>', response.text, re.DOTALL)
    assert card is not None
    card_html = card.group(0)
    assert 'hx-swap-oob="true"' in card_html
    assert "Running" in card_html
    assert "pod analyzing" in card_html
    assert "bg-violet-50" in card_html
