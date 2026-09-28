"""Tests for `services/backends/local.py` (split from test_backends.py, phaze-7l8jh).

`LocalBackend.is_available` / `in_flight_count` / `dispatch` / `reconcile` -- `services/backends/local.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from tests.analyze.services.backends.protocol._shared import (
    CloudJob,
    CloudJobStatus,
    DedupFakeTaskRouter,
    _local,
    _make_file,
    backends,
    pytest,
    seed_active_agent,
    uuid,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord


# ``saq_jobs`` is SAQ-owned (``Base.metadata.create_all`` does not know it), and a sibling suite may have
# left one behind with a different shape, so a bare ``CREATE TABLE IF NOT EXISTS`` would be
# order-dependent. DROP + CREATE pins the schema these tests control -- the idiom from
# tests/analyze/tasks/test_ledger_reaper.py; the per-test transaction rolls it back. Only ``key`` /
# ``status`` are read by ``live_job_clause``, so the minimal shape is faithful.
_CREATE_SAQ_JOBS = (
    text("DROP TABLE IF EXISTS saq_jobs"),
    text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"),
)


async def _saq_table(session: AsyncSession) -> None:
    for stmt in _CREATE_SAQ_JOBS:
        await session.execute(stmt)
    await session.commit()


async def _seed_process_file_ledger(session: AsyncSession, file: FileRecord) -> str:
    """Write the ``process_file:<file_id>`` ledger row ``before_enqueue`` would; return its key."""
    from phaze.models.scheduling_ledger import SchedulingLedger

    key = f"process_file:{file.id}"
    session.add(SchedulingLedger(key=key, function="process_file", routing="agent", payload={"file_id": str(file.id)}))
    await session.commit()
    return key


async def _seed_saq(session: AsyncSession, key: str, status: str) -> None:
    await session.execute(text("INSERT INTO saq_jobs (key, status) VALUES (:key, :status)"), {"key": key, "status": status})
    await session.commit()


@pytest.mark.asyncio
async def test_local_is_available_always_true(session: AsyncSession) -> None:
    """LocalBackend.is_available is unconditionally True -- local dispatch needs no remote agent."""
    assert await _local().is_available(session) is True


@pytest.mark.asyncio
async def test_local_in_flight_count_is_zero_when_nothing_running(session: AsyncSession) -> None:
    """LocalBackend has no cloud_job rows AND no ledger rows -> in_flight_count is 0 (the healthy idle case)."""
    assert await _local().in_flight_count(session) == 0


@pytest.mark.asyncio
async def test_local_in_flight_count_is_ledger_derived_not_hardcoded_zero(session: AsyncSession) -> None:
    """phaze-xd8k: LocalBackend.in_flight_count reports the REAL running count, not a hardcoded 0.

    A local burst writes NO ``cloud_job`` row (the ``_BaseBackend`` COUNT substrate is structurally
    always 0 for this lane) -- that hardcode was the observability bug: the ANALYZE header's
    ``analyzeActive`` derives from the SAME ``process_file:<file_id>`` scheduling-ledger predicate
    (:func:`phaze.services.stage_status.inflight_clause`) and counted thousands of in-flight files while
    this lane rendered a literal 0. Seed TWO music/video files carrying a live ledger row (mirrors the
    ``before_enqueue`` hook's own write, seeded directly here since ``DedupFakeQueue`` does not run that
    hook, matching ``test_local_dispatch_excluded_from_staging_candidates``'s idiom) and a THIRD
    non-music/video file with a ledger row that must NOT be counted (mirrors the ``music_video_total``
    scoping every other stage-progress read applies) -> in_flight_count reports exactly 2.
    """
    await _saq_table(session)
    running = [_make_file() for _ in range(2)]
    non_music_video = _make_file(file_type="txt")
    for file in [*running, non_music_video]:
        session.add(file)
    await session.commit()
    # phaze-1kowg: each seeded ledger row carries a live broker job, so every one is genuinely RUNNING.
    for file in [*running, non_music_video]:
        await _seed_saq(session, await _seed_process_file_ledger(session, file), "active")

    assert await _local().in_flight_count(session) == 2


@pytest.mark.asyncio
async def test_local_in_flight_count_excludes_files_with_an_in_flight_cloud_job(session: AsyncSession) -> None:
    """phaze-xd8k: a file whose ``process_file`` is running on a CLOUD agent is NOT double-counted as local.

    Both local and compute dispatch enqueue ``process_file`` under the IDENTICAL deterministic ledger key
    (``process_file:<file_id>``, :func:`phaze.services.analysis_enqueue.process_file_job_key`), so the
    ledger key alone cannot distinguish "local" from "cloud" -- the real-count query must ALSO exclude any
    file still carrying an in-flight ``cloud_job`` row (the D-10 ``{UPLOADING, UPLOADED, SUBMITTED,
    RUNNING}`` set), which is exactly what a kueue file's actively-RUNNING analysis Job holds. Seed one
    ledger-in-flight file WITHOUT a cloud_job row (genuinely local -> counted) alongside one
    ledger-in-flight file WITH an in-flight cloud_job row (cloud-routed -> excluded).
    """
    await _saq_table(session)
    local_only = _make_file()
    cloud_routed = _make_file()
    session.add(local_only)
    session.add(cloud_routed)
    await session.commit()
    session.add(CloudJob(id=uuid.uuid4(), file_id=cloud_routed.id, backend_id="kueue-x64", status=CloudJobStatus.RUNNING.value))
    await session.commit()
    for file in (local_only, cloud_routed):
        await _seed_saq(session, await _seed_process_file_ledger(session, file), "active")

    assert await _local().in_flight_count(session) == 1


@pytest.mark.asyncio
async def test_local_in_flight_count_ignores_a_ledger_row_with_no_saq_job(session: AsyncSession) -> None:
    """phaze-1kowg: a ``process_file`` ledger row with NO broker job behind it does not occupy the lane.

    The production shape (spike phaze-tch4d): 2374 ``process_file`` ledger rows enqueued 2026-07-16..08-08,
    zero ``process_file`` rows in ``saq_jobs``, ``cap=1`` -- the bare-ledger count read the lane full
    forever. Seed that shape in miniature: several ORPHANED rows (no ``saq_jobs`` row at all) plus rows
    whose broker job reached a NON-live status (``complete`` / ``failed`` / ``aborting`` / ``aborted``).
    None is running on the fileserver agent, so the count is 0 -- while the ledger rows themselves are
    untouched (owed work stays owed; re-driving it is recovery's job, phaze-5y73k).
    """
    from sqlalchemy import func, select

    from phaze.models.scheduling_ledger import SchedulingLedger

    await _saq_table(session)
    orphans = [_make_file() for _ in range(3)]
    dead = {status: _make_file() for status in ("complete", "failed", "aborting", "aborted")}
    session.add_all([*orphans, *dead.values()])
    await session.commit()
    for file in orphans:
        await _seed_process_file_ledger(session, file)
    for status, file in dead.items():
        await _seed_saq(session, await _seed_process_file_ledger(session, file), status)

    assert await _local().in_flight_count(session) == 0
    assert (await session.execute(select(func.count()).select_from(SchedulingLedger))).scalar_one() == len(orphans) + len(dead)


@pytest.mark.asyncio
async def test_local_in_flight_count_counts_queued_and_active_jobs_but_not_phantoms(session: AsyncSession) -> None:
    """phaze-1kowg: a ledger row WITH a ``queued`` or ``active`` broker job still counts; its phantom neighbours do not.

    ``queued`` counts as well as ``active``: a queued job holds its place on the agent's analyze queue (and a
    parked/paused job keeps ``status='queued'``), so it genuinely occupies the lane. Mixed with phantoms in
    ONE database so the assertion discriminates -- the pre-fix bare-ledger count reads 5 here, not 2.
    """
    await _saq_table(session)
    queued, active = _make_file(), _make_file()
    phantoms = [_make_file() for _ in range(3)]
    session.add_all([queued, active, *phantoms])
    await session.commit()
    await _seed_saq(session, await _seed_process_file_ledger(session, queued), "queued")
    await _seed_saq(session, await _seed_process_file_ledger(session, active), "active")
    for file in phantoms:
        await _seed_process_file_ledger(session, file)

    assert await _local().in_flight_count(session) == 2


@pytest.mark.asyncio
async def test_local_in_flight_count_does_not_count_a_live_job_with_no_ledger_row(session: AsyncSession) -> None:
    """The count is the RUNNING refinement OF in-flight, never wider than the ledger: a bare broker row alone is not counted."""
    await _saq_table(session)
    file = _make_file()
    session.add(file)
    await session.commit()
    await _seed_saq(session, f"process_file:{file.id}", "active")

    assert await _local().in_flight_count(session) == 0


@pytest.mark.asyncio
async def test_local_in_flight_count_degrades_to_the_ledger_only_count_when_saq_jobs_is_unreadable(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """phaze-1kowg DEGRADE: no ``saq_jobs`` table -> the pre-fix ledger-only count, logged, never a raise.

    The conservative direction for a CAPACITY read: over-count so the lane holds, rather than read an
    unreadable broker as "nothing running" and over-admit. The SAVEPOINT keeps the failed probe from
    poisoning the caller's transaction -- the follow-up read in the SAME session must still work.
    """
    from sqlalchemy import func, select

    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    await session.commit()
    files = [_make_file() for _ in range(2)]
    session.add_all(files)
    await session.commit()
    for file in files:
        await _seed_process_file_ledger(session, file)

    assert await _local().in_flight_count(session) == 2
    assert "local_in_flight_degraded" in caplog.text
    # The caller's transaction survived the failed probe.
    assert (await session.execute(select(func.count()).select_from(text("files")))).scalar_one() >= 2


@pytest.mark.asyncio
async def test_local_dispatch_writes_no_cloud_job_row(session: AsyncSession) -> None:
    """LocalBackend.dispatch stays on the local process_file path -- it writes no cloud_job row."""
    backend = _local()
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    from sqlalchemy import func, select

    count = int((await session.execute(select(func.count(CloudJob.id)).where(CloudJob.file_id == file.id))).scalar() or 0)
    assert count == 0


@pytest.mark.asyncio
async def test_local_dispatch_writes_no_state_and_no_cloud_job(session: AsyncSession) -> None:
    """CR-01 / Phase 90 (D-09): LocalBackend.dispatch no longer writes files.state and creates no cloud_job.

    The former LOCAL_ANALYZING flip was removed; a locally-spilled file leaves the AWAITING_CLOUD
    candidate set via its ``process_file:<id>`` scheduling-ledger row (proven in
    test_local_dispatch_excluded_from_staging_candidates), NOT a state write.
    """
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _local()
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    # Phase 90 (D-09): files.state is left untouched, and local dispatch writes no cloud_job row.
    from sqlalchemy import select

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is None


@pytest.mark.asyncio
async def test_local_dispatch_excluded_from_staging_candidates(session: AsyncSession) -> None:
    """CR-01 / D-05: after a local dispatch the file is excluded from ``get_cloud_staging_candidates``.

    Post Phase-83 the drain no longer reads ``FileRecord.state`` (SC#1): a candidate must carry a
    ``cloud_job(status='awaiting')`` row (INNER join) AND not be analyze-in-flight. ``LocalBackend.dispatch``
    writes NO cloud_job row and deletes none (D-05 rejects deletion -- the awaiting row is retained); the
    committed ``process_file:<id>`` ledger row (the ``before_enqueue`` hook's own write, seeded here since
    the DedupFakeQueue does not run that hook) is what makes ``~inflight_clause(ANALYZE)`` exclude the file.
    """
    from sqlalchemy import select

    from phaze.models.scheduling_ledger import SchedulingLedger
    from phaze.services.pipeline import get_cloud_staging_candidates

    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _local()
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    session.add(CloudJob(id=uuid.uuid4(), file_id=fid, status=CloudJobStatus.AWAITING.value))
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)
    session.add(SchedulingLedger(key=f"process_file:{fid}", function="process_file", routing="agent", payload={"file_id": str(fid)}))
    await session.commit()

    candidates = await get_cloud_staging_candidates(session, limit=10)
    assert fid not in {f.id for f, _ in candidates}  # excluded by ~inflight_clause(ANALYZE)
    # D-05: the awaiting row is retained (the conjunct excludes; it does not delete the row).
    retained = (await session.execute(select(CloudJob.status).where(CloudJob.file_id == fid))).scalar_one()
    assert retained == CloudJobStatus.AWAITING.value


@pytest.mark.asyncio
async def test_local_dispatch_returns_true_on_enqueue(session: AsyncSession) -> None:
    """WR-01: a genuine ``process_file`` enqueue reports a truthy dispatch (new work staged)."""
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _local()
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    assert await backend.dispatch(file, session, router) is True


@pytest.mark.asyncio
async def test_local_dispatch_returns_false_on_dedup_noop(session: AsyncSession) -> None:
    """WR-01: a deterministic-key ``process_file:<id>`` dedup no-op reports False (not newly staged).

    ``enqueue_process_file`` returns ``None`` when SAQ dedups the deterministic key (the file is already
    being analyzed locally); LocalBackend.dispatch must report that as ``False`` so the drain's staged
    tally is honest -- mirroring ``ComputeAgentBackend.dispatch``'s ``return job is not None``.
    """
    from phaze.services.analysis_enqueue import process_file_job_key

    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _local()
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    # Pre-enqueue the deterministic key on the fileserver's queue so dispatch's enqueue dedups to None.
    live_queue = router.queue_for("nox", "analyze")
    await live_queue.enqueue("process_file", key=process_file_job_key(file.id))
    router.queue_for_calls.clear()

    assert await backend.dispatch(file, session, router) is False


@pytest.mark.asyncio
async def test_local_reconcile_is_noop(session: AsyncSession) -> None:
    """LocalBackend.reconcile is a no-op (local completion is synchronous, no cron read)."""
    assert await _local().reconcile(session) is None


@pytest.mark.asyncio
async def test_local_dispatch_leaves_awaiting_row_present(session: AsyncSession) -> None:
    """D-13: LocalBackend.dispatch NEITHER writes NOR deletes a held file's inert awaiting cloud_job row.

    A held file carries an ``awaiting`` cloud_job row. LocalBackend stays a no-``cloud_job``-row
    writer/deleter (D-05 chose the drain predicate conjunct over row deletion), so after a local dispatch
    the inert ``awaiting`` row is still present, still ``status='awaiting'`` (it is reaped later by D-14, not
    here). Phase 90 (D-09): the former LOCAL_ANALYZING files.state flip was removed.
    """
    from sqlalchemy import select

    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    file = _make_file()
    session.add(file)
    await session.flush()
    await backends.hold_awaiting_cloud(session, file)  # held: awaiting cloud_job row present
    await session.commit()

    backend = _local()
    await backend.dispatch(file, session, DedupFakeTaskRouter())

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is not None  # LocalBackend did NOT delete the inert awaiting row (D-13)
    assert job.status == CloudJobStatus.AWAITING.value  # nor re-write it
