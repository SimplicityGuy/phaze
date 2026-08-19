"""Controller-side tests for `routers/pipeline/backfill.py` (split from test_pipeline.py, phaze-7l8jh).

POST /pipeline/backfill-cloud and the L4 backfill-candidate query -- `routers/pipeline/backfill.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.routers.pipeline._shared import (
    _KUEUE_BACKEND,
    _LOCAL_BACKEND,
    _LONG,
    _SHORT,
    UTC,
    AnalysisResult,
    Any,
    CloudJobStatus,
    RouteControl,
    SchedulingLedger,
    _analysis_failed_at,
    _awaiting_cloud_ids,
    _cloud_compute_registry,  # noqa: F401 -- autouse fixture, never referenced by name
    _cloud_job_status,
    _DrainableStubBackend,
    _is_awaiting_cloud,
    _persist_failed_with_duration,
    _persist_files_with_duration,
    _process_file_ledger_rows,
    _reset_saq_jobs_minimal,
    _run_stage_cloud_window,
    datetime,
    drain_router_background_tasks,
    pytest,
    seed_active_agent,
    select,
    settings,
    text,
    timedelta,
    update,
    wire_fakes,
)


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


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

    await drain_router_background_tasks()
    # No direct-to-compute (or any) enqueue: the long failed file is held for the staging cron.
    assert capture == []

    # The short failed file stays ANALYSIS_FAILED; the never-failed DISCOVERED file is untouched.
    await session.refresh(short_failed)
    await session.refresh(untouched_discovered)
    assert await _analysis_failed_at(session, short_failed.id) is not None  # short file still failed
    # 83-06 (reverses D-09): the long failed file leaves ANALYSIS_FAILED and becomes a CLEAN
    # AWAITING_CLOUD hold -- its failed_at marker is cleared and its orphaned process_file ledger
    # row is deleted (the awaiting cloud_job row is the sole registry the drain now owns).
    assert await _is_awaiting_cloud(session, long_failed.id)
    assert await _analysis_failed_at(session, long_failed.id) is None
    assert await _process_file_ledger_rows(session, long_failed.id) == []


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
async def test_backfill_no_compute_holds_a_clean_awaiting_cloud_file(client: AsyncClient, session: AsyncSession) -> None:
    """83-06 (reverses D-09): with no compute agent online, a backfilled long file becomes a CLEAN AWAITING_CLOUD hold.

    The held file is NEVER enqueued (CLOUDROUTE-02), and it leaves ANALYSIS_FAILED cleanly: failed_at
    cleared + the orphaned process_file ledger row deleted, only the awaiting cloud_job row kept.
    """
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")  # fileserver only, no compute
    capture = wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200

    await drain_router_background_tasks()
    # The held file is NEVER enqueued (the load-bearing CLOUDROUTE-02 safety invariant).
    assert capture == []
    assert await _is_awaiting_cloud(session, long_failed.id)

    # 83-06: the marker is cleared and the orphaned ledger row deleted -- a clean drainable hold.
    assert await _analysis_failed_at(session, long_failed.id) is None
    assert await _process_file_ledger_rows(session, long_failed.id) == []


@pytest.mark.asyncio
async def test_backfill_with_compute_online_still_holds_a_clean_awaiting_cloud_file(client: AsyncClient, session: AsyncSession) -> None:
    """83-06 (reverses D-09): even with a compute agent online the backfilled file is a CLEAN AWAITING_CLOUD hold.

    Phase 50 removed the direct-to-compute backfill branch (every candidate is held for the drain);
    83-06 removes the held-file ledger SEED so the hold is clean -- failed_at cleared, the orphaned
    process_file ledger row deleted, only the awaiting cloud_job row kept (no direct compute enqueue).
    """
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "cloud", kind="compute")
    capture = wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200

    await drain_router_background_tasks()
    assert capture == []  # no direct compute enqueue
    assert await _is_awaiting_cloud(session, long_failed.id)
    assert await _analysis_failed_at(session, long_failed.id) is None
    assert await _process_file_ledger_rows(session, long_failed.id) == []


@pytest.mark.asyncio
async def test_backfill_marker_clear_is_staged_before_routing_commit(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """phaze-7g4t: the marker strips are staged in the SAME session/transaction as the hold.

    The marker strips (failed_at clear + ledger delete) must be pending in the session BEFORE
    ``_route_discovered_by_duration`` runs, so its single internal commit flushes all three mutations
    atomically. The old code committed the holds first and the strips in a SECOND transaction, leaving
    an interruption window that permanently stranded held files. Proving the strips are already staged
    when routing is entered proves there is no separate second commit.
    """
    # phaze-oau1o: `routers/pipeline.py` is now a package; `_route_discovered_by_duration` is read from the
    # `backfill` submodule's namespace, so patching the facade would silently no-op.
    import phaze.routers.pipeline.backfill as pipeline_mod

    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")
    wire_fakes(client)

    real_route = pipeline_mod._route_discovered_by_duration
    seen: dict[str, object] = {}

    async def _spy(app_state: object, sess: AsyncSession, candidates: object, *args: object) -> dict[str, int]:
        # At routing entry the strips must ALREADY be pending in the endpoint's session (autoflush
        # makes the staged UPDATE/DELETE visible to these reads) -- i.e. no separate prior commit.
        seen["ledger_at_route"] = await _process_file_ledger_rows(sess, long_failed.id)
        seen["failed_at_route"] = await _analysis_failed_at(sess, long_failed.id)
        return await real_route(app_state, sess, candidates, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline_mod, "_route_discovered_by_duration", _spy)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200

    # The strips were staged in the SAME transaction before the hold's single commit.
    assert seen["ledger_at_route"] == []  # ledger delete already staged
    assert seen["failed_at_route"] is None  # failed_at clear already staged
    # End state: a clean drainable hold (all three mutations landed together).
    assert await _is_awaiting_cloud(session, long_failed.id)
    assert await _analysis_failed_at(session, long_failed.id) is None
    assert await _process_file_ledger_rows(session, long_failed.id) == []


@pytest.mark.asyncio
async def test_backfill_double_click_holds_nothing_new(client: AsyncClient, session: AsyncSession) -> None:
    """A second backfill click holds zero new files — never a whole-backlog over-enqueue (D-10)."""
    await _persist_failed_with_duration(session, [_LONG, _LONG])
    await seed_active_agent(session, "cloud", kind="compute")
    capture = wire_fakes(client)

    r1 = await client.post("/pipeline/backfill-cloud")
    assert r1.status_code == 200
    await drain_router_background_tasks()
    assert capture == []  # held, never directly enqueued
    assert len(await _awaiting_cloud_ids(session)) == 2  # both long failed files held once (cloud_job awaiting)

    # After the first backfill the candidates carry an active cloud_job (the derived idempotency guard in
    # _backfill_candidates_stmt excludes them, Phase 90 PR-A), so the second click selects nothing new.
    r2 = await client.post("/pipeline/backfill-cloud")
    assert r2.status_code == 200
    await drain_router_background_tasks()
    assert len(await _awaiting_cloud_ids(session)) == 2  # unchanged — no over-enqueue
    assert "No timed-out long files" in r2.text


@pytest.mark.asyncio
async def test_backfill_makes_every_candidate_a_clean_awaiting_cloud_hold(client: AsyncClient, session: AsyncSession) -> None:
    """83-06 (reverses D-09): EVERY backfill candidate becomes a clean AWAITING_CLOUD hold.

    Every backfill candidate is long, so ``_route_discovered_by_duration`` HOLDS all of them (the
    candidate set IS the held set). 83-06 then strips both exclusion markers from every held file: after
    one backfill of THREE long-failed files, each one carries an awaiting cloud_job row, has its
    ``analysis.failed_at`` cleared, and has its orphaned ``process_file:<id>`` ledger row DELETED -- so
    the bounded drain owns all three (none missing, none left excluded).
    """
    files = await _persist_failed_with_duration(session, [_LONG, _LONG, _LONG])
    await seed_active_agent(session, "cloud", kind="compute")
    capture = wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await drain_router_background_tasks()
    assert capture == []  # held, never directly enqueued

    file_ids = [f.id for f in files]  # capture before _awaiting_cloud_ids() expires the ORM objects
    held_ids = await _awaiting_cloud_ids(session)
    for fid in file_ids:
        assert fid in held_ids
        assert await _analysis_failed_at(session, fid) is None, f"held file {fid} must have its failed_at marker cleared"
        assert await _process_file_ledger_rows(session, fid) == [], f"held file {fid} must have its orphaned ledger row deleted"


@pytest.mark.asyncio
async def test_backfill_zero_candidates_returns_empty_fragment(client: AsyncClient, session: AsyncSession) -> None:
    """With no timed-out long files, backfill returns the empty-count fragment and enqueues nothing."""
    await seed_active_agent(session, "cloud", kind="compute")
    capture = wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    assert "No timed-out long files" in response.text

    await drain_router_background_tasks()
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

    await drain_router_background_tasks()
    # Nothing enqueued anywhere -- the disabled path never routes.
    assert capture == []
    # The ANALYSIS_FAILED file is NEVER reset to DISCOVERED (no silent re-time-out, Pitfall 2).
    await session.refresh(long_failed)
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

    await drain_router_background_tasks()
    assert capture == []  # held, never directly enqueued
    assert await _is_awaiting_cloud(session, long_failed.id)


# --- Phase 55 Plan 04 Task 2 (CLOUDROUTE-02): backfill converges the cloud kinds (83-06 reverses D-09) --
# The backfill endpoint routes ledger-scoped failed long files to AWAITING_CLOUD for BOTH cloud kinds.
# 83-06 removed the per-kind held-file ledger seed fork (compute SEED / kueue SKIP): NEITHER branch seeds
# a ledger row now -- both CLEAR analysis.failed_at and DELETE the orphaned process_file:<id> row, so the
# held file is a clean drainable AWAITING_CLOUD candidate whose SOLE registry is the awaiting cloud_job
# row. The compute and kueue paths are therefore behavior-identical; each is asserted directly.


@pytest.mark.asyncio
async def test_backfill_compute_clears_marker_and_deletes_ledger_row(client: AsyncClient, session: AsyncSession) -> None:
    """compute branch (83-06): the held file is a clean AWAITING_CLOUD hold -- failed_at cleared, ledger row deleted."""
    # A single compute backend is the autouse default.
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")  # no compute -> held in AWAITING_CLOUD
    wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await drain_router_background_tasks()

    assert await _is_awaiting_cloud(session, long_failed.id)
    assert await _analysis_failed_at(session, long_failed.id) is None
    assert await _process_file_ledger_rows(session, long_failed.id) == []


@pytest.mark.asyncio
async def test_backfill_kueue_clears_marker_and_deletes_ledger_row(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kueue branch (83-06): identical to compute -- the held file is a clean AWAITING_CLOUD hold.

    The former kueue SKIP fork (no ledger seed, but ``failed_at`` retained) left the held file
    domain-completed and un-drainable; 83-06 clears ``failed_at`` and deletes the orphaned ledger row on
    the kueue branch too, so the held file is a clean drainable candidate the awaiting cloud_job registers.
    """
    monkeypatch.setattr(settings, "backends", [_KUEUE_BACKEND])
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")  # k8s has no compute agent -> held
    wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await drain_router_background_tasks()

    assert await _is_awaiting_cloud(session, long_failed.id)
    assert await _analysis_failed_at(session, long_failed.id) is None
    assert await _process_file_ledger_rows(session, long_failed.id) == []


@pytest.mark.asyncio
async def test_backfill_skips_file_with_live_process_file_job(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-l1km: a candidate with a LIVE process_file job is skipped -- no double-dispatch.

    A producer can re-enqueue process_file on a still-failed long file WITHOUT clearing failed_at, so
    the file satisfies every backfill candidate conjunct (failed marker + long + ledger row EXISTS) while
    its local job is still grinding. Backfilling it would delete the LIVE job's ledger row and hold the
    file for the cloud drain -- dispatching the same file to local + cloud at once and orphaning the local
    job from queue-loss recovery. The endpoint skips any candidate whose process_file key is live: the
    file stays failed, its ledger row survives, and it is NOT held in AWAITING_CLOUD.
    """
    (live_reanalysis,) = await _persist_failed_with_duration(session, [_LONG])  # long + failed + ledger row
    await seed_active_agent(session, "nox", kind="fileserver")  # no compute -> would otherwise hold in AWAITING_CLOUD
    # Model the live re-analysis: a queued/active saq_jobs row for this file's process_file key.
    await _reset_saq_jobs_minimal(session)
    await session.execute(
        text("INSERT INTO saq_jobs (key, status) VALUES (:key, 'active')"),
        {"key": f"process_file:{live_reanalysis.id}"},
    )
    await session.commit()
    wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await drain_router_background_tasks()

    # The live-job file is untouched: failure marker retained, ledger row (the live in-flight marker)
    # NOT deleted, and it is NOT held in AWAITING_CLOUD -- so no local+cloud double-dispatch.
    assert await _analysis_failed_at(session, live_reanalysis.id) is not None
    assert len(await _process_file_ledger_rows(session, live_reanalysis.id)) == 1
    assert not await _is_awaiting_cloud(session, live_reanalysis.id)


@pytest.mark.asyncio
async def test_backfill_ledger_delete_is_cas_guarded_against_a_concurrent_reenqueue(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """phaze-g31m: a concurrent process_file re-enqueue landing between the ledger snapshot and the
    DELETE must not lose the live ledger row out from under it.

    The l1km ``live_keys`` guard is a lock-free snapshot taken well before the ledger DELETE; a
    concurrent enqueue (retry_analysis_failed's background loop, a recovery replay) for this EXACT
    candidate can land in that gap. ``upsert_ledger_entry`` -- the SAQ before_enqueue write hook every
    process_file producer shares -- refreshes ``enqueued_at`` on every re-enqueue of a still-existing
    key, so the ledger DELETE is CAS-guarded on the exact ``enqueued_at`` value observed immediately
    before it runs. Simulated here by bumping the row's ``enqueued_at`` right after that snapshot read
    (in the SAME transaction, standing in for the concurrent commit) -- the CAS's stale comparison value
    no longer matches, so the DELETE misses the row instead of silently clobbering a live producer's claim.
    """
    (candidate,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")
    wire_fakes(client)

    real_execute = session.execute
    bumped = False
    # A literal future value, NOT func.now(): Postgres's now()/CURRENT_TIMESTAMP is fixed at
    # TRANSACTION start, and this whole test runs inside one begin/rollback-wrapped transaction
    # (the shared ``session`` fixture), so a func.now() "bump" here would silently no-op back to the
    # SAME value the row already carries -- masking the very race this test exists to prove closed.
    future_enqueued_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    async def _bump_after_ledger_snapshot(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal bumped
        result = await real_execute(statement, *args, **kwargs)
        compiled = str(statement)
        if not bumped and "scheduling_ledger" in compiled and "enqueued_at" in compiled and "DELETE" not in compiled.upper():
            bumped = True
            await real_execute(
                update(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{candidate.id}").values(enqueued_at=future_enqueued_at),
            )
        return result

    monkeypatch.setattr(session, "execute", _bump_after_ledger_snapshot)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200

    monkeypatch.undo()
    assert bumped, "the ledger snapshot statement was never intercepted -- test is not exercising the race"
    # The CAS-guarded delete missed the row (its enqueued_at no longer matched what was observed) -- the
    # concurrently-refreshed ledger row survives instead of being removed out from under the live job.
    assert len(await _process_file_ledger_rows(session, candidate.id)) == 1


@pytest.mark.asyncio
async def test_backfill_cas_delete_removes_every_candidates_ledger_row(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-krzz5: the CAS delete must still remove EVERY candidate's ledger row for N > 1 rows.

    The `unnest`-based rewrite replaces a per-row composite ``tuple_(...).in_(...)`` with two
    array-bound parameters; this pins the functional behavior (every observed (key, enqueued_at)
    pair is matched and deleted) is unchanged by that rewrite, not just its bind-parameter shape.
    """
    candidates = await _persist_failed_with_duration(session, [_LONG, _LONG, _LONG, _LONG, _LONG])
    await seed_active_agent(session, "nox", kind="fileserver")
    wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await drain_router_background_tasks()

    for candidate in candidates:
        assert await _process_file_ledger_rows(session, candidate.id) == [], (
            f"ledger row for {candidate.id} survived the CAS delete -- the unnest rewrite dropped a row"
        )


@pytest.mark.asyncio
async def test_backfill_local_redrives_nothing(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """local fork: the cloud-off gate short-circuits -- no file is touched (marker + ledger row untouched)."""
    monkeypatch.setattr(settings, "backends", [_LOCAL_BACKEND])
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")
    wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await drain_router_background_tasks()

    # The all-local early-return mutates nothing: the failure marker + the prior ledger row survive.
    assert await _analysis_failed_at(session, long_failed.id) is not None
    assert len(await _process_file_ledger_rows(session, long_failed.id)) == 1
    assert not await _is_awaiting_cloud(session, long_failed.id)


@pytest.mark.asyncio
async def test_backfill_compute_held_file_is_drainable(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """83-06 (reverses D-09): a backfilled COMPUTE-target failed long file DRAINS to the compute backend.

    RED on the pre-83-06 code: backfill retained ``analysis.failed_at`` (domain-completed) AND seeded a
    ``process_file:<id>`` ledger row (in-flight), so ``awaiting_candidate_clause`` excluded the held file
    and ``stage_cloud_window`` staged 0. After OPTION A the held file is a clean awaiting-cloud candidate:
    ``failed_at`` cleared + ledger row deleted, only the awaiting ``cloud_job`` row kept -> staged=1.
    """
    # Autouse fixture pins a single compute backend (cloud ON, active_cloud_kind 'compute').
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")  # the drain's push-initiator gate
    wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await drain_router_background_tasks()

    # The held file is now a CLEAN drainable candidate: awaiting cloud_job kept, failed_at + ledger cleared.
    assert await _is_awaiting_cloud(session, long_failed.id)
    marker = (await session.execute(select(AnalysisResult.failed_at).where(AnalysisResult.file_id == long_failed.id))).scalar_one()
    assert marker is None  # 83-06: the failed_at marker was cleared (mirrors retry_analysis_failed)
    ledger = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{long_failed.id}"))).scalars().all()
    assert ledger == []  # 83-06: the orphaned process_file ledger row was deleted (drain is the single owner)

    # The drain now DISPATCHES it to the compute backend (staged=1, cloud_job submitted).
    backend = _DrainableStubBackend(id="a1")
    result = await _run_stage_cloud_window(monkeypatch, backend)
    assert result == {"staged": 1, "skipped": 0}
    status, backend_id = await _cloud_job_status(session, long_failed.id)
    assert (status, backend_id) == (CloudJobStatus.SUBMITTED.value, "a1")


@pytest.mark.asyncio
async def test_backfill_kueue_held_file_is_drainable(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """83-06 (reverses D-09): the KUEUE twin -- a backfilled kueue-target failed long file also DRAINS.

    The kueue backfill branch already seeded NO ledger row, but it left ``analysis.failed_at`` set, so the
    held file was still domain-completed and excluded by ``awaiting_candidate_clause``. OPTION A clears
    ``failed_at`` on BOTH cloud branches, so the kueue-held file also becomes a clean drainable candidate.
    """
    monkeypatch.setattr(settings, "backends", [_KUEUE_BACKEND])
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "nox", kind="fileserver")
    wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await drain_router_background_tasks()

    assert await _is_awaiting_cloud(session, long_failed.id)
    marker = (await session.execute(select(AnalysisResult.failed_at).where(AnalysisResult.file_id == long_failed.id))).scalar_one()
    assert marker is None
    ledger = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{long_failed.id}"))).scalars().all()
    assert ledger == []

    backend = _DrainableStubBackend(id="k8s")
    result = await _run_stage_cloud_window(monkeypatch, backend)
    assert result == {"staged": 1, "skipped": 0}
    status, backend_id = await _cloud_job_status(session, long_failed.id)
    assert (status, backend_id) == (CloudJobStatus.SUBMITTED.value, "k8s")


@pytest.mark.asyncio
async def test_force_local_backfill_zero_mutation_no_op(client: AsyncClient, session: AsyncSession) -> None:
    """Gate L793 (POST /pipeline/backfill-cloud): force-local True is a ZERO-mutation no-op.

    Byte-identical outcome to ``test_backfill_disabled_when_cloud_local`` (the all-local-registry no-op),
    but here the autouse cloud-ON ``[_COMPUTE_BACKEND]`` registry stays pinned and a persisted
    ``RouteControl(id="global", force_local=True)`` row drives the early-return via the
    ``or await get_route_control(session)`` clause. A forced backfill must NOT reset the ANALYSIS_FAILED
    long files to DISCOVERED and hold them in AWAITING_CLOUD while the (forced) drain no-ops -- that would
    strand them (T-71-08, ``pipeline.py:789-793``). Asserts all THREE zero-mutation signals: nothing
    enqueued, the candidate row stays ANALYSIS_FAILED, and NO SchedulingLedger row is seeded.

    Anti-cheat: the case fails if the ``or await get_route_control(session)`` clause were removed from the
    L793 early-return. ``with_ledger=True`` is LOAD-BEARING -- the backfill candidate query
    (:func:`_backfill_candidates_stmt`) requires an ``EXISTS(scheduling_ledger 'process_file:<id>')``
    predicate, so a genuine (previously-scheduled) candidate is needed for the gate to be the ONLY thing
    holding it back. Without the ledger row the candidate is filtered out regardless of the gate and the
    test would pass even with the L793 clause deleted (a false pass). With the clause removed and the
    cloud-ON registry pinned, the candidate would be reset to DISCOVERED and routed to the online compute
    agent -- flipping ``capture`` non-empty and the state off ANALYSIS_FAILED. The pre-existing
    scheduled-work ledger row stays a single row through the no-op (no duplicate seed, no reset).
    """
    session.add(RouteControl(id="global", force_local=True))
    await session.commit()
    # with_ledger=True makes this a GENUINE backfill candidate (the ledger-scoped candidate query only
    # sees previously-scheduled work). The gate must be the sole reason it is NOT reset/routed here.
    (long_failed,) = await _persist_failed_with_duration(session, [_LONG])
    await seed_active_agent(session, "cloud", kind="compute")
    await seed_active_agent(session, "nox", kind="fileserver")
    capture = wire_fakes(client)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200

    await drain_router_background_tasks()
    # Signal 1: nothing enqueued anywhere -- the forced-local path never routes.
    assert capture == []
    # Signal 2: the ANALYSIS_FAILED file is NEVER reset to DISCOVERED (no silent re-time-out / hold).
    await session.refresh(long_failed)
    # Signal 3: the forced-local no-op neither seeds a duplicate nor drops the pre-existing scheduled-work
    # ledger row -- it stays exactly one process_file:<id> row (the with_ledger=True seed), untouched.
    rows = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{long_failed.id}"))).scalars().all()
    assert len(rows) == 1
