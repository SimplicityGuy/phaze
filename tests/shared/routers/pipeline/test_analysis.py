"""Controller-side tests for `routers/pipeline/analysis.py` (split from test_pipeline.py, phaze-7l8jh).

POST /api/v1/analyze, /pipeline/analyze, and the ANALYSIS_FAILED bulk/per-file retry endpoints -- `routers/pipeline/analysis.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.routers.pipeline._shared import (
    _DEAD_DEEPEN_ARTIFACTS,
    _JOB_HEARTBEAT_SEC,
    _LONG,
    _SHORT,
    UTC,
    AnalysisResult,
    CloudJob,
    CloudJobStatus,
    DedupFakeQueue,
    FileRecord,
    Path,
    ProcessFilePayload,
    RouteControl,
    SchedulingLedger,
    _awaiting_cloud_ids,
    _cloud_compute_registry,  # noqa: F401 -- autouse fixture, never referenced by name
    _is_awaiting_cloud,
    _make_file,
    _make_file_owned_by,
    _persist_files_with_duration,
    _seed_analysis_failed,
    datetime,
    delete,
    drain_router_background_tasks,
    install_fake_queues,
    make_agent_live,
    postgresql,
    pytest,
    seed_active_agent,
    select,
    settings,
    uuid,
    wire_fakes,
)


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_analyze_enqueues_discovered(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/analyze enqueues process_file onto phaze-agent-nox (not default)."""
    session.add_all([_make_file() for _ in range(3)])
    await session.commit()
    await make_agent_live(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 3

    await drain_router_background_tasks()
    assert len(capture) == 3
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-test-fileserver-analyze", "process_file")}
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
    file_rec = _make_file()
    session.add(file_rec)
    await session.commit()
    # expire_on_commit=False (conftest) -- these stay readable after commit.
    expected_id = str(file_rec.id)
    expected_path = file_rec.original_path
    expected_type = file_rec.file_type
    await make_agent_live(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    assert response.json()["enqueued"] == 1

    await drain_router_background_tasks()
    assert len(capture) == 1
    queue_name, task_name, kwargs = capture[0]
    assert (queue_name, task_name) == ("phaze-agent-test-fileserver-analyze", "process_file")

    # All five required fields present -- not just file_id (the pre-fix bug). Phase 50 added the
    # optional expected_sha256/scratch_path for the cloud push pipeline, both None on the bulk
    # local path (a local file is read in place). phaze-w55w1 removed the Phase 44-01
    # fine_cap/coarse_cap overrides entirely -- there is no per-job window budget to carry.
    assert set(kwargs) == {
        "file_id",
        "original_path",
        "file_type",
        "agent_id",
        "models_path",
        "expected_sha256",
        "scratch_path",
    }
    assert kwargs["file_id"] == expected_id
    assert kwargs["original_path"] == expected_path
    assert kwargs["file_type"] == expected_type
    assert kwargs["agent_id"] == "test-fileserver"
    assert kwargs["models_path"] == settings.models_path

    # The exact kwargs the agent worker receives validate against ProcessFilePayload.
    validated = ProcessFilePayload.model_validate(kwargs)
    assert str(validated.file_id) == expected_id


@pytest.mark.asyncio
async def test_enqueue_analysis_jobs_logs_a_blocked_collision(caplog: pytest.LogCaptureFixture) -> None:
    """phaze-ewen: a bulk-run file whose key is held by a DEAD job is logged, not silently omitted.

    Pre-fix, ``_enqueue_analysis_jobs`` discarded every ``None`` return uniformly -- a file
    blocked by a zombie 'aborting'/'failed'/stuck row vanished from a run the dashboard reports
    as "N enqueued" with nothing distinguishing it from a benign already-in-flight dedup.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import phaze.routers.pipeline as pipeline_mod

    file_rec = _make_file()
    key = f"process_file:{file_rec.id}"
    queue = DedupFakeQueue("phaze-agent-test-fileserver-analyze")
    queue._live_keys.add(key)  # pre-register the key as already "in flight" so enqueue dedups to None
    queue.job = AsyncMock(return_value=SimpleNamespace(status="aborting", stuck=False))

    with caplog.at_level("WARNING", logger="phaze.routers.pipeline"):
        await pipeline_mod._enqueue_analysis_jobs(queue, [file_rec], "test-fileserver", settings.models_path)

    assert queue.captured == []  # nothing enqueued -- collision, not a fresh job
    assert "deterministic key held by a dead job" in caplog.text
    assert str(file_rec.id) in caplog.text


@pytest.mark.asyncio
async def test_enqueue_analysis_jobs_does_not_log_a_live_collision(caplog: pytest.LogCaptureFixture) -> None:
    """A collision against a genuinely LIVE job stays quiet -- only a BLOCKED collision is loud."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import phaze.routers.pipeline as pipeline_mod

    file_rec = _make_file()
    key = f"process_file:{file_rec.id}"
    queue = DedupFakeQueue("phaze-agent-test-fileserver-analyze")
    queue._live_keys.add(key)
    queue.job = AsyncMock(return_value=SimpleNamespace(status="queued", stuck=False))

    with caplog.at_level("WARNING", logger="phaze.routers.pipeline"):
        await pipeline_mod._enqueue_analysis_jobs(queue, [file_rec], "test-fileserver", settings.models_path)

    assert queue.captured == []
    assert "deterministic key held by a dead job" not in caplog.text


@pytest.mark.asyncio
async def test_enqueue_analysis_jobs_contains_a_raising_collision_probe(caplog: pytest.LogCaptureFixture) -> None:
    """phaze-p2qvv: a raising ``queue.job()`` probe costs one file's diagnostic, not the group.

    Pre-fix, ``classify_process_file_collision(await queue.job(...))`` sat OUTSIDE the per-file
    ``try``/``except`` -- a raise there (e.g. a transient broker error) escaped
    ``_enqueue_analysis_jobs`` entirely, aborting every remaining file in the batch. The probe is
    purely diagnostic and must never affect ``failed_ids`` or abort the loop.
    """
    from unittest.mock import AsyncMock

    import phaze.routers.pipeline as pipeline_mod

    blocked_file = _make_file()
    later_file = _make_file()
    key = f"process_file:{blocked_file.id}"
    queue = DedupFakeQueue("phaze-agent-test-fileserver-analyze")
    queue._live_keys.add(key)  # dedups blocked_file's enqueue to None
    queue.job = AsyncMock(side_effect=ConnectionError("transient broker pool error"))

    with caplog.at_level("WARNING", logger="phaze.routers.pipeline"):
        failed_ids = await pipeline_mod._enqueue_analysis_jobs(queue, [blocked_file, later_file], "test-fileserver", settings.models_path)

    # The probe's own failure never lands in failed_ids -- it is diagnostic-only.
    assert failed_ids == []
    # later_file's enqueue still ran -- one file's probe failure did not abort the group.
    assert len(queue.captured) == 1
    assert queue.captured[0][1]["file_id"] == str(later_file.id)
    assert "collision-classification probe failed" in caplog.text
    assert str(blocked_file.id) in caplog.text


@pytest.mark.asyncio
async def test_analyze_enqueues_bounded_timeout_and_retries(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-w55w1: POST /api/v1/analyze enqueues process_file with NO wall clock + a heartbeat.

    The outer SAQ timeout went 14400 (Phase 31) -> 7200 (Phase 43, under a 6600s inner kill) ->
    ``0``, i.e. disabled. Exhaustive analysis means a concert set legitimately runs past any
    such number, and a wall clock cannot tell that from a hang (phaze-1b39). Liveness moves to
    SAQ's own ``heartbeat``, which the agent lane touches off the analysis child's progress
    channel. retries=2 stays in the locked 1-2 band so apply_project_job_defaults does NOT
    clobber it to worker_max_retries (the retries==1 -> 4 churn).
    """
    file_rec = _make_file()
    session.add(file_rec)
    await session.commit()
    expected_key = f"process_file:{file_rec.id}"
    await make_agent_live(session)
    _, task_router = install_fake_queues(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    assert response.json()["enqueued"] == 1

    await drain_router_background_tasks()
    queue = task_router.queues["test-fileserver-analyze"]
    assert len(queue.captured_policy) == 1
    # Phase 32: the shared helper now also sets the deterministic dedup key.
    assert queue.captured_policy[0] == {"key": expected_key, "timeout": 0, "heartbeat": _JOB_HEARTBEAT_SEC, "retries": 2}
    # retries is explicitly NOT 1 (which apply_project_job_defaults would override to 4).
    assert queue.captured_policy[0]["retries"] != 1
    # Payload still complete (job-control keys are split out, not part of the payload).
    task_name, payload = queue.captured[0]
    assert task_name == "process_file"
    assert set(payload) == {
        "file_id",
        "original_path",
        "file_type",
        "agent_id",
        "models_path",
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
    files = [_make_file() for _ in range(3)]
    session.add_all(files)
    await session.commit()
    expected_keys = {f"process_file:{f.id}" for f in files}
    await make_agent_live(session)
    _, task_router = install_fake_queues(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    assert response.json()["enqueued"] == 3

    await drain_router_background_tasks()
    queue = task_router.queues["test-fileserver-analyze"]
    assert len(queue.captured_policy) == 3
    # Every enqueue carries a key, and it matches that same enqueue's payload file_id.
    for (task_name, payload), policy in zip(queue.captured, queue.captured_policy, strict=True):
        assert task_name == "process_file"
        assert policy["key"] == f"process_file:{payload['file_id']}"
    assert {p["key"] for p in queue.captured_policy} == expected_keys


@pytest.mark.asyncio
async def test_analyze_ui_enqueues_bounded_timeout_and_retries(client: AsyncClient, session: AsyncSession) -> None:
    """The HTMX /pipeline/analyze path enqueues with the same policy: no wall clock, a heartbeat."""
    file_rec = _make_file()
    session.add(file_rec)
    await session.commit()
    expected_key = f"process_file:{file_rec.id}"
    await make_agent_live(session)
    _, task_router = install_fake_queues(client)

    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200

    await drain_router_background_tasks()
    queue = task_router.queues["test-fileserver-analyze"]
    assert len(queue.captured_policy) == 1
    # Phase 32: the shared helper now also sets the deterministic dedup key.
    assert queue.captured_policy[0] == {"key": expected_key, "timeout": 0, "heartbeat": _JOB_HEARTBEAT_SEC, "retries": 2}


@pytest.mark.asyncio
async def test_process_file_enqueue_policy_survives_project_defaults_hook() -> None:
    """The before_enqueue hook leaves the explicit timeout=0 / retries=2 policy intact.

    apply_project_job_defaults fills a Job still at the SAQ defaults (timeout==10, retries==1)
    and then pins process_file's own policy. phaze-w55w1 makes the timeout half worth asserting
    twice over: ``0`` is both the intended value AND exactly what a naive "fill in a sane
    default" step would overwrite, and overwriting it would silently restore the wall-clock kill
    this bead removed.
    """
    from saq import Job

    from phaze.tasks._shared.queue_defaults import apply_project_job_defaults

    job = Job(function="process_file", timeout=0, retries=2)
    await apply_project_job_defaults(job)
    assert job.timeout == 0
    assert job.retries == 2


@pytest.mark.asyncio
async def test_analyze_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/analyze with files but no active agent surfaces a visible empty-state."""
    session.add_all([_make_file() for _ in range(3)])
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0
    assert "no active agent" in data["message"].lower()

    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_analyze_no_files(client: AsyncClient) -> None:
    """POST /api/v1/analyze with no DISCOVERED files returns enqueued=0."""
    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0


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

    await drain_router_background_tasks()
    # No direct-to-compute (or any) enqueue: the file holds for the staging cron.
    assert capture == []
    assert await _is_awaiting_cloud(session, long_file.id)


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

    await drain_router_background_tasks()
    # Nothing is enqueued (the long file is held; the short file is skipped, never enqueued).
    assert capture == []
    assert await _is_awaiting_cloud(session, long_file.id)
    # The short file stays DISCOVERED (skipped != held, no state change).
    await session.refresh(short_file)


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

    await drain_router_background_tasks()
    # The held file is NEVER enqueued (the load-bearing CLOUDROUTE-02 safety invariant).
    assert capture == []
    assert await _is_awaiting_cloud(session, long_file.id)


@pytest.mark.asyncio
async def test_analyze_short_and_null_route_to_fileserver_with_key(client: AsyncClient, session: AsyncSession) -> None:
    """A <threshold file AND a null-duration file both route to the fileserver queue with key process_file:<id> (D-06)."""
    short_file, null_file = await _persist_files_with_duration(session, [_SHORT, None])
    await make_agent_live(session)  # phaze-c9w9: the OWNING agent must be live for local routing
    _, task_router = install_fake_queues(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["local"] == 2
    assert data["cloud"] == 0

    await drain_router_background_tasks()
    queue = task_router.queues["test-fileserver-analyze"]
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

    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_analyze_ui_reports_split_counts(client: AsyncClient, session: AsyncSession) -> None:
    """The HTMX /pipeline/analyze response renders the split counts 'N local, K awaiting cloud' (D-12, Phase 50).

    Phase 50 reshape: long files are held in AWAITING_CLOUD (``cloud`` is always 0), so the long
    file surfaces under 'awaiting cloud', not 'cloud'.
    """
    await _persist_files_with_duration(session, [_LONG, _SHORT, None])
    await seed_active_agent(session, "cloud", kind="compute")
    await make_agent_live(session)  # phaze-c9w9: the OWNING agent must be live for local routing
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

    await drain_router_background_tasks()
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

    # The file really is held in AWAITING_CLOUD (derived from the cloud_job sidecar, Phase 90 D-09).
    assert len(await _awaiting_cloud_ids(session)) == 1

    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_get_awaiting_cloud_count_derives_from_the_drain_clause(session: AsyncSession) -> None:
    """D-15: get_awaiting_cloud_count counts EXACTLY the drain's genuinely-parked awaiting rows.

    Re-anchored (Phase 83) off the retired ``FileRecord.state == AWAITING_CLOUD`` display read onto
    ``COUNT(cloud_job) WHERE status='awaiting' AND ~inflight_clause(ANALYZE) AND
    ~domain_completed_clause(ANALYZE)`` -- the SAME clause ``get_cloud_staging_candidates`` uses, so the
    card and the drain can NEVER disagree. A LOCAL_ANALYZING long file that still carries its inert
    awaiting row (D-13 keeps the flip; D-14 reaps the row at the analyze-terminal seam) is
    analyze-in-flight, so it is excluded from BOTH the count and the drain candidate set.
    """
    from phaze.services.pipeline import get_awaiting_cloud_count, get_cloud_staging_candidates

    # (1) A genuinely-parked awaiting file: awaiting row, no ledger, no analysis -> counted.
    parked = _make_file()
    # (2) A locally-dispatched long file: still carries its awaiting row (D-13) but is analyze-in-flight
    #     (a committed process_file:<id> ledger row) -> excluded from the count AND the drain.
    analyzing = _make_file()
    session.add_all([parked, analyzing])
    await session.commit()
    session.add(CloudJob(id=uuid.uuid4(), file_id=parked.id, status=CloudJobStatus.AWAITING.value))
    session.add(CloudJob(id=uuid.uuid4(), file_id=analyzing.id, status=CloudJobStatus.AWAITING.value))
    session.add(
        SchedulingLedger(key=f"process_file:{analyzing.id}", function="process_file", routing="agent", payload={"file_id": str(analyzing.id)})
    )
    await session.commit()

    # Only the genuinely-parked file is counted; the analyze-in-flight file is excluded.
    assert await get_awaiting_cloud_count(session) == 1
    # And the count agrees with the drain candidate set exactly (card and drain cannot disagree).
    candidates = await get_cloud_staging_candidates(session, limit=10)
    assert {f.id for f, _ in candidates} == {parked.id}


@pytest.mark.asyncio
async def test_route_discovered_by_duration_skips_a_file_deleted_before_its_hold(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-e8kv: a FileRecord deleted between the discovered-set SELECT and the hold's FK-bearing
    INSERT must be skipped, never a 500 that aborts the whole run.

    Mirrors ``force_skip_stage``'s SAVEPOINT + caught-``IntegrityError`` discipline for the identical
    race (a concurrent ``delete_scan`` cascade removing the row). Two long candidates are routed: the
    first is deleted out from under the loop right before its hold runs (simulating the cascade landing
    mid-loop); the second is untouched. The FK violation on the first must cost exactly one skipped
    file, not the second file's hold or the whole request.
    """
    # phaze-oau1o: `routers/pipeline.py` is now a package; `hold_awaiting_cloud` is read from the
    # `analysis` submodule's namespace, so patching the facade would silently no-op.
    import phaze.routers.pipeline.analysis as pipeline_mod
    from phaze.services.backends import hold_awaiting_cloud

    doomed = _make_file()
    survivor = _make_file()
    session.add_all([doomed, survivor])
    await session.commit()
    doomed_id = doomed.id
    survivor_id = survivor.id  # capture before expire_all() below

    async def _delete_then_hold(sess: AsyncSession, file: FileRecord, **kwargs: object) -> bool:
        if file.id == doomed_id:
            await sess.execute(delete(FileRecord).where(FileRecord.id == doomed_id))
        return await hold_awaiting_cloud(sess, file, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline_mod, "hold_awaiting_cloud", _delete_then_hold)

    counts = await pipeline_mod._route_discovered_by_duration(
        app_state=None,
        session=session,
        files_with_duration=[(doomed, 999.0), (survivor, 999.0)],
        threshold_sec=60,
        cloud_enabled=True,
        models_path="models",
    )

    assert counts["awaiting"] == 1  # only the survivor was held
    assert counts["skipped"] == 1  # the doomed file is counted, not silently dropped
    session.expire_all()
    survivor_row = (await session.execute(select(CloudJob).where(CloudJob.file_id == survivor_id))).scalar_one()
    assert survivor_row.status == CloudJobStatus.AWAITING.value
    doomed_rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == doomed_id))).scalars().all()
    assert doomed_rows == []  # no orphaned cloud_job row for the vanished file


def test_scheduling_ledger_cas_delete_stmt_uses_a_constant_bind_count_regardless_of_row_count() -> None:
    """phaze-krzz5: THE regression pin -- bind-parameter count must be constant (2), never O(N).

    A bare ``tuple_(key, enqueued_at).in_(rows)`` renders TWO literal bind parameters PER ROW,
    which is exactly the shape that re-crosses asyncpg's 32767-parameter cap this module's sibling
    array-bind helpers (``_analysis_file_ids_scope`` / ``_ledger_keys_scope``) were hardened to
    avoid. Compiling the statement and counting its bind parameters -- independent of how many
    ledger rows were observed -- is the direct, fast (no live DB, no 16K-row fixture) assertion
    that the fix actually changed the PARAMETER SHAPE, not merely that a small-N delete still works
    (the functional test above would pass against the un-fixed code too, since the bug only
    manifests at ~16,383+ rows).
    """
    from phaze.routers.pipeline import _scheduling_ledger_cas_delete_stmt

    small = [(f"process_file:{i}", datetime(2026, 1, 1, tzinfo=UTC)) for i in range(3)]
    large = [(f"process_file:{i}", datetime(2026, 1, 1, tzinfo=UTC)) for i in range(5000)]

    small_compiled = _scheduling_ledger_cas_delete_stmt(small).compile(dialect=postgresql.dialect())
    large_compiled = _scheduling_ledger_cas_delete_stmt(large).compile(dialect=postgresql.dialect())

    # Exactly the two array bind params (`cas_delete_keys`, `cas_delete_enqueued_ats`) -- NOT one
    # pair of literal params per row, and NOT growing with the row count.
    assert set(small_compiled.params) == {"cas_delete_keys", "cas_delete_enqueued_ats"}
    assert len(small_compiled.params) == len(large_compiled.params) == 2


# ---------------------------------------------------------------------------
# phaze-w55w1: POST /pipeline/files/{file_id}/deepen is REMOVED.
#
# Phase 44's per-file "deepen" re-enqueued one file at a cap of 0 to lift the
# window caps for it. With analysis exhaustive by default (ADR-0007 §7) there is no cap to
# lift, so the endpoint, its progress poll, their templates, and the ~20 tests that pinned
# their routing/collision/poll-state behaviour are all gone. What replaces them is the two
# assertions below: the routes 404, and nothing still links to them.
#
# The guards those tests protected are NOT lost -- per-agent routing, the NoActiveAgentError
# refusal, the complete-payload funnel, and phaze-ewen collision classification are all
# exercised by the surviving process_file producers (`_enqueue_analysis_jobs` and the
# operator-gated retry endpoints) in this same file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deepen_routes_are_gone(client: AsyncClient, session: AsyncSession) -> None:
    """Both deepen routes 404 -- they are unregistered, not merely unreachable from the UI."""
    file_rec = _make_file()
    session.add(file_rec)
    await session.commit()

    post = await client.post(f"/pipeline/files/{file_rec.id}/deepen")
    poll = await client.get(f"/pipeline/files/{file_rec.id}/deepen-progress?since=1700000000.0")

    assert post.status_code == 404
    assert poll.status_code == 404


def test_no_live_reference_to_the_removed_deepen_surface() -> None:
    """No resolvable reference to the removed surface survives anywhere in the app.

    A dangling `hx-post` renders perfectly and fails only on click; a stale `{% include %}` of a
    deleted partial is a 500 on whatever page includes it; a Jinja gate on a dropped column is a
    silent always-false. None of the three is caught by a route test, so the tree is swept for
    the concrete artifacts directly.
    """
    import phaze

    root = Path(phaze.__file__).resolve().parent
    offenders = [
        f"{path.relative_to(root)}:{i}: {line.strip()[:80]}"
        for path in [*root.rglob("*.py"), *root.rglob("*.html")]
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if any(token in line for token in _DEAD_DEEPEN_ARTIFACTS)
    ]

    assert offenders == [], f"live references to the removed deepen surface: {offenders}"


# ---------------------------------------------------------------------------
# quick-260707-d79: operator-gated BULK retry of ANALYSIS_FAILED files.
#
# POST /pipeline/analysis-failed/retry re-drives EVERY ANALYSIS_FAILED file through the SAME
# guarded funnel every producer uses (per-agent routing -> NoActiveAgentError guard ->
# enqueue_process_file full payload + deterministic key). Each file leaves the red bucket immediately (the retired ``files.state`` flip that
# accompanied this is gone since Phase 90). recover_orphaned_work / _select_done_analyze_ids stay unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_reenqueues_all_failed_and_flips_state(client: AsyncClient, session: AsyncSession) -> None:
    """POST retry re-enqueues process_file for every ANALYSIS_FAILED file on the per-agent queue.

    All N failed files land on ``phaze-agent-nox`` (never the default queue) carrying the COMPLETE
    ProcessFilePayload -- since phaze-w55w1 a retry and a first run are literally the same job,
    there being no per-file window budget left to vary -- and every file's derived analyze-failure marker is cleared (0 remain in failed_clause). Phase 90
    (D-09): retry NO LONGER writes files.state. The ack reports N.
    """
    failed = await _seed_analysis_failed(session, 3)
    failed_uuids = [f.id for f in failed]  # capture before any expire_all() (avoids async lazy reload)
    failed_ids = {str(fid) for fid in failed_uuids}
    await make_agent_live(session)
    _, task_router = install_fake_queues(client)

    response = await client.post("/pipeline/analysis-failed/retry")
    assert response.status_code == 200
    assert "re-queued 3 failed file(s)" in response.text.lower()

    await drain_router_background_tasks()  # phaze-zecg: the enqueue loop now runs as a background task
    queue = task_router.queues["test-fileserver-analyze"]
    assert len(queue.captured) == 3
    assert queue.name == "phaze-agent-test-fileserver-analyze"
    assert queue.name != "default"
    captured_ids = set()
    for task_name, payload in queue.captured:
        assert task_name == "process_file"
        # Complete payload (v4.0.8 guard): validates against ProcessFilePayload.
        ProcessFilePayload.model_validate(payload)
        captured_ids.add(payload["file_id"])
    assert captured_ids == failed_ids

    # Phase 90 (D-09): retry clears the derived failure marker (analysis.failed_at), committed before
    # enqueue, so every retried file leaves failed_clause(Stage.ANALYZE). files.state is NOT written.
    session.expire_all()
    marker_rows = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id.in_(failed_uuids)))).scalars().all()
    assert len(marker_rows) == 3
    assert all(r.failed_at is None for r in marker_rows), "every retried file's analyze-failure marker must be cleared"


@pytest.mark.asyncio
async def test_retry_no_active_agent_enqueues_nothing_and_keeps_state(client: AsyncClient, session: AsyncSession) -> None:
    """No active agent -> zero enqueues, files STAY ANALYSIS_FAILED, ack surfaces "no active agent" (Phase-30 guard)."""
    failed = await _seed_analysis_failed(session, 2)
    failed_ids = {str(f.id) for f in failed}
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/pipeline/analysis-failed/retry")
    assert response.status_code == 200
    assert "no active agent" in response.text.lower()

    await drain_router_background_tasks()
    # Nothing enqueued anywhere -- never the default queue.
    assert capture == []
    # Derived failure markers UNCHANGED: the no-op retry clears nothing (the marker is the sole authority).
    still_failed = (await session.execute(select(AnalysisResult.file_id).where(AnalysisResult.failed_at.is_not(None)))).scalars().all()
    assert {str(fid) for fid in still_failed} == failed_ids


@pytest.mark.asyncio
async def test_retry_zero_failed_is_noop(client: AsyncClient, session: AsyncSession) -> None:
    """No ANALYSIS_FAILED files -> 200, zero enqueues, ack count 0."""
    await make_agent_live(session)
    capture = wire_fakes(client)

    response = await client.post("/pipeline/analysis-failed/retry")
    assert response.status_code == 200
    assert "no failed files to retry" in response.text.lower()

    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_retry_button_renders_only_when_count_positive(client: AsyncClient, session: AsyncSession) -> None:
    """The "Retry failed" button appears in the Analysis Health card ONLY when analysis_failed_count > 0.

    Rendered via the existing 5s /pipeline/stats poll (which seeds analysis_failed_count into the
    analysis_failed_card). With no ANALYSIS_FAILED files the button + endpoint reference are absent;
    with >0 the hx-post + hx-confirm are present.
    """
    # count == 0: no button, no endpoint reference.
    zero = await client.get("/pipeline/stats", headers={"HX-Request": "true"})
    assert zero.status_code == 200
    assert "analysis-failed/retry" not in zero.text

    # count > 0: button with the confirm gate appears.
    await _seed_analysis_failed(session, 2)
    positive = await client.get("/pipeline/stats", headers={"HX-Request": "true"})
    assert positive.status_code == 200
    assert "analysis-failed/retry" in positive.text
    assert "hx-confirm" in positive.text


@pytest.mark.asyncio
async def test_trigger_analysis_ui_with_files(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/analyze enqueues process_file onto phaze-agent-nox + renders the fragment."""
    session.add_all([_make_file() for _ in range(2)])
    await session.commit()
    await make_agent_live(session)
    capture = wire_fakes(client)

    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "analysis" in response.text

    await drain_router_background_tasks()
    assert len(capture) == 2
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-test-fileserver-analyze", "process_file")}
    # UI path enqueues a complete payload too (every job carries all five fields).
    for _q, _t, kwargs in capture:
        ProcessFilePayload.model_validate(kwargs)


@pytest.mark.asyncio
async def test_trigger_analysis_ui_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/analyze with files but no active agent renders the no-active-agent copy."""
    session.add_all([_make_file() for _ in range(2)])
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200
    assert "No active agent available" in response.text

    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_trigger_analysis_ui_no_files(client: AsyncClient) -> None:
    """POST /pipeline/analyze with no DISCOVERED files returns HTML with zero count."""
    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_enqueue_analysis_background(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/analyze enqueues a complete ProcessFilePayload in the background."""
    session.add(_make_file())
    await session.commit()
    await make_agent_live(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    # Verify the enqueue was called (background task may complete by now)
    assert response.json()["enqueued"] == 1

    await drain_router_background_tasks()
    assert len(capture) == 1
    queue_name, task_name, kwargs = capture[0]
    assert queue_name == "phaze-agent-test-fileserver-analyze"
    assert task_name == "process_file"
    # Complete payload -- all five ProcessFilePayload fields, not just file_id, plus the two
    # optional Phase-50 cloud-push fields (None on the bulk path).
    assert set(kwargs) == {
        "file_id",
        "original_path",
        "file_type",
        "agent_id",
        "models_path",
        "expected_sha256",
        "scratch_path",
    }
    ProcessFilePayload.model_validate(kwargs)


# ---------------------------------------------------------------------------
# Phase 75 Plan 02 (HYG-04): force-local duration-router gate regression region.
#
# Guards the T-71-08 regression class at all THREE live gate sites of the
# ``effective_cloud_enabled = settings.cloud_enabled and not await get_route_control(session)``
# fold:
#   - pipeline.py L396  trigger_analysis        (POST /api/v1/analyze)
#   - pipeline.py L718  trigger_analysis_ui     (POST /pipeline/analyze)
#   - pipeline.py L793  trigger_backfill_cloud  (POST /pipeline/backfill-cloud, zero-mutation no-op)
#
# Each True case KEEPS the autouse ``_cloud_compute_registry`` (a single compute backend =>
# cloud_enabled True) so the ONLY thing forcing local is a persisted
# ``RouteControl(id="global", force_local=True)`` row on the shared session. The False control
# (no route_control row) proves the toggle -- not some other condition -- drives the local routing.
#
# Anti-cheat: the True cases assert the ABSENCE of AWAITING_CLOUD rows (a
# ``select(FileRecord).where(state == AWAITING_CLOUD)`` scalars check), NOT a bare enqueue/routing
# count, so each case would FAIL if the ``and not await get_route_control(session)`` clause were
# removed from its gate (a long file would then be held for the cloud drain).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_local_analyze_api_routes_local_no_hold(client: AsyncClient, session: AsyncSession) -> None:
    """Gate L396 (POST /api/v1/analyze): force-local True routes a _LONG file LOCAL, ZERO AWAITING_CLOUD held.

    Consolidates + supersedes ``tests/shared/routers/test_routing.py::test_route_forced_local_no_hold``
    (the prior PARTIAL coverage of this gate): the autouse cloud-ON ``[_COMPUTE_BACKEND]`` registry would
    normally HOLD a >=threshold DISCOVERED file in AWAITING_CLOUD, but a persisted
    ``RouteControl(id="global", force_local=True)`` row makes the effective fold
    ``cloud_enabled AND NOT force_local`` False, so the long file routes to the fileserver queue exactly
    like an all-local registry and is NEVER parked in AWAITING_CLOUD (D-09/D-10, T-71-08).
    """
    session.add(RouteControl(id="global", force_local=True))
    await session.commit()
    await _persist_files_with_duration(session, [_LONG])
    await make_agent_live(session)  # phaze-c9w9: the OWNING agent must be live for local routing
    wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    # Force-local => the long file routes LOCAL (fileserver), nothing held for the cloud drain.
    assert data["local"] == 1
    assert data["awaiting_cloud"] == 0

    await drain_router_background_tasks()
    # Anti-cheat: ZERO cloud_job awaiting rows (not a bare enqueue count) -- fails if the
    # `and not await get_route_control(session)` clause were dropped from gate L396. Phase 90 (D-09):
    # "held" derives from the cloud_job sidecar, not files.state.
    assert await _awaiting_cloud_ids(session) == set()


@pytest.mark.asyncio
async def test_force_local_analyze_ui_routes_local_no_hold(client: AsyncClient, session: AsyncSession) -> None:
    """Gate L718 (POST /pipeline/analyze): force-local True routes a _LONG file LOCAL, ZERO AWAITING_CLOUD held.

    The HTMX duration trigger shares the same ``effective_cloud_enabled`` fold as the API route but is a
    physically separate gate line, so it is exercised separately (D-09). Same setup, same zero-hold
    outcome: the persisted force-local row routes the long file local through the real endpoint.
    """
    session.add(RouteControl(id="global", force_local=True))
    await session.commit()
    await _persist_files_with_duration(session, [_LONG])
    await make_agent_live(session)  # phaze-c9w9: the OWNING agent must be live for local routing
    wire_fakes(client)

    response = await client.post("/pipeline/analyze")
    assert response.status_code == 200
    text = response.text
    # Force-local => the long file surfaces under "local", never "awaiting cloud".
    assert "1 local" in text
    assert "0 awaiting cloud" in text

    await drain_router_background_tasks()
    # Anti-cheat: ZERO cloud_job awaiting rows -- fails if the force-local clause were dropped from gate
    # L718. Phase 90 (D-09): "held" derives from the cloud_job sidecar, not files.state.
    assert await _awaiting_cloud_ids(session) == set()


@pytest.mark.asyncio
async def test_force_local_analyze_api_false_control_still_holds(client: AsyncClient, session: AsyncSession) -> None:
    """Control (force-local OFF): with NO route_control row the SAME _LONG file IS held AWAITING_CLOUD.

    Proves the persisted ``RouteControl(force_local=True)`` toggle -- not some other condition -- is the
    only variable driving the local routing above. With the autouse cloud-ON registry and no override, a
    compute agent online still HOLDS the long file in AWAITING_CLOUD (the registry is honored, D-10).
    """
    # No RouteControl row seeded => get_route_control returns False => cloud honored.
    (long_file,) = await _persist_files_with_duration(session, [_LONG])
    await seed_active_agent(session, "cloud", kind="compute")
    await seed_active_agent(session, "nox", kind="fileserver")
    capture = wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["local"] == 0
    assert data["cloud"] == 0
    assert data["awaiting_cloud"] == 1

    await drain_router_background_tasks()
    # The long file IS held (nothing enqueued from here -- the staging cron is the sole compute entry).
    assert capture == []
    assert await _awaiting_cloud_ids(session) == {long_file.id}


@pytest.mark.asyncio
async def test_analyze_skips_files_of_offline_owner_and_routes_the_rest(client: AsyncClient, session: AsyncSession) -> None:
    """Run Analysis with one owner offline: the live owner's file routes; the offline owner's is skipped.

    phaze-c9w9: the offline owner's file must NOT be rerouted onto the live agent's mount (the
    spurious-terminal-failure / cross-agent-corruption shapes); it is reported ``skipped`` instead.
    """
    await seed_active_agent(session, "fileserver-east")
    east_file = _make_file_owned_by("fileserver-east")
    # test-fileserver (the conftest FK parent) is never-seen -> its file's owner is offline.
    offline_file = _make_file()
    session.add_all([east_file, offline_file])
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["local"] == 1
    assert data["skipped"] == 1

    await drain_router_background_tasks()
    assert [(q, kwargs["file_id"]) for q, _t, kwargs in capture] == [("phaze-agent-fileserver-east-analyze", str(east_file.id))]
