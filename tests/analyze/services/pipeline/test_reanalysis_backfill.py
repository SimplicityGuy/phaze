"""Unit tests for ``phaze.services.reanalysis_backfill`` (phaze-kj8dl).

Covers the acceptance-critical behaviors: the windows-columns-only selection (strided rows in
either tier are selected, NULL legacy rows are the recorded skip, applied/moved rows are excluded
and counted), the duration-based cloud-routing guards (long -> held, short/null -> local, already
in the cloud pipeline -> skipped), the enqueue/classify loop's outcomes including containment
around a broker failure, idempotent re-run via SAQ's deterministic-key dedup, and the exit-code
function.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select

from phaze.config import get_settings
from phaze.config_backends import ComputeBackend, LocalBackend
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
import phaze.services.reanalysis_backfill as reanalysis_backfill_mod
from phaze.services.reanalysis_backfill import (
    ReanalysisOutcome,
    compute_exit_code,
    count_applied_incomplete_analyses_rows,
    count_null_windows_columns_rows,
    enqueue_incomplete_reanalysis,
    select_incomplete_analyses,
)
from tests._queue_fakes import DedupFakeTaskRouter, FakeTaskRouter, make_agent_live


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings


_COMPUTE_BACKEND = ComputeBackend(kind="compute", id="a1", rank=10, cap=2, agent_ref="cloud-1", scratch_dir="/scratch", push_host="a1.push")
_LOCAL_BACKEND = LocalBackend(kind="local", id="local", rank=99, cap=1)


def _set_backends(monkeypatch: pytest.MonkeyPatch, backends: list[Any]) -> ControlSettings:
    """Patch ``.backends`` on the ``ControlSettings`` instance THIS test's ``get_settings()`` returns.

    ``phaze.services.reanalysis_backfill`` reads settings via ``get_settings()`` (matching
    ``services/backends.py``'s own established idiom, ``cast("ControlSettings", get_settings())``),
    NOT via the ``phaze.config.settings`` module-level singleton ``routers/pipeline.py`` uses --
    the two are DIFFERENT objects (``get_settings`` is a separate ``@lru_cache``d constructor, and
    ``tests/conftest.py`` clears that cache before every test so each test gets its own fresh
    instance). Patching the module singleton (the ``tests/shared/routers/test_pipeline.py``
    precedent for the router-level duration tests) would silently no-op here.
    """
    settings = cast("ControlSettings", get_settings())
    monkeypatch.setattr(settings, "backends", backends)
    return settings


@pytest.fixture(autouse=True)
def _all_local_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to an all-local registry (``cloud_enabled`` False).

    Most of this module's tests exercise the LOCAL enqueue path and must not accidentally hold a
    file for the cloud drain because of whatever the test's env-derived default registry happens to
    be. The cloud-routing tests below override this inside their own bodies via :func:`_set_backends`.
    """
    _set_backends(monkeypatch, [_LOCAL_BACKEND])


async def _seed_analysis(
    session: AsyncSession,
    file: FileRecord,
    *,
    fine_analyzed: int | None = None,
    fine_total: int | None = None,
    coarse_analyzed: int | None = None,
    coarse_total: int | None = None,
) -> AnalysisResult:
    """Insert a bare ``AnalysisResult`` row carrying only the windows-progress columns under test."""
    result = AnalysisResult(
        id=uuid.uuid4(),
        file_id=file.id,
        fine_windows_analyzed=fine_analyzed,
        fine_windows_total=fine_total,
        coarse_windows_analyzed=coarse_analyzed,
        coarse_windows_total=coarse_total,
    )
    session.add(result)
    await session.commit()
    return result


async def _seed_duration(session: AsyncSession, file: FileRecord, duration: float) -> None:
    """Insert a ``FileMetadata`` row carrying only ``duration`` (the cloud-routing input)."""
    session.add(FileMetadata(id=uuid.uuid4(), file_id=file.id, duration=duration))
    await session.commit()


async def _seed_applied(session: AsyncSession, file: FileRecord) -> None:
    """Insert an EXECUTED ``RenameProposal`` -- the authoritative already-moved marker (``applied_clause``)."""
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file.id,
            proposed_filename="Renamed Set.mp3",
            proposed_path=None,
            confidence=0.9,
            status=ProposalStatus.EXECUTED,
        )
    )
    await session.commit()


async def _seed_active_cloud_job(session: AsyncSession, file: FileRecord, status: str = CloudJobStatus.UPLOADING.value) -> None:
    """Insert a ``CloudJob`` row in an ACTIVE status -- the T-82-A1 double-dispatch guard input."""
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=status))
    await session.commit()


# ---------------------------------------------------------------------------
# select_incomplete_analyses / count_null_windows_columns_rows / count_applied_incomplete_analyses_rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_incomplete_analyses_strided_fine_tier(session: AsyncSession, make_file: Any) -> None:
    """A row whose FINE tier is short of its natural total is selected."""
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=60, fine_total=240, coarse_analyzed=30, coarse_total=30)

    selected = await select_incomplete_analyses(session)

    assert [f.id for f, _duration in selected] == [file.id]


@pytest.mark.asyncio
async def test_select_incomplete_analyses_strided_coarse_tier(session: AsyncSession, make_file: Any) -> None:
    """A row whose COARSE tier is short of its natural total is selected (OR, not AND)."""
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=10, fine_total=10, coarse_analyzed=5, coarse_total=20)

    selected = await select_incomplete_analyses(session)

    assert [f.id for f, _duration in selected] == [file.id]


@pytest.mark.asyncio
async def test_select_incomplete_analyses_excludes_fully_covered_rows(session: AsyncSession, make_file: Any) -> None:
    """``analyzed == total`` in both tiers -- a healthy row -- is never selected."""
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=10, fine_total=10, coarse_analyzed=5, coarse_total=5)

    assert await select_incomplete_analyses(session) == []


@pytest.mark.asyncio
async def test_select_incomplete_analyses_excludes_null_legacy_rows(session: AsyncSession, make_file: Any) -> None:
    """Pre-Phase-43 rows (every windows column NULL) are the recorded skip decision -- never selected."""
    file = await make_file()
    await _seed_analysis(session, file)  # all four columns NULL

    assert await select_incomplete_analyses(session) == []


@pytest.mark.asyncio
async def test_select_incomplete_analyses_selects_partial_inflight_row(session: AsyncSession, make_file: Any) -> None:
    """A mid-flight progress row (one tier populated + incomplete, the other still NULL) IS selected.

    Per the module docstring: this is not a legacy row (not all four columns are NULL), and its
    populated tier is genuinely incomplete, so it is correctly selected -- the enqueue/classify
    loop's dedup is what keeps re-enqueueing it from double-booking a job already in flight.
    """
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=0, fine_total=240, coarse_analyzed=None, coarse_total=None)

    assert [f.id for f, _duration in await select_incomplete_analyses(session)] == [file.id]


@pytest.mark.asyncio
async def test_select_incomplete_analyses_carries_duration(session: AsyncSession, make_file: Any) -> None:
    """The LEFT OUTER JOINed ``FileMetadata.duration`` rides along with the selected file."""
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    await _seed_duration(session, file, 6000.0)

    selected = await select_incomplete_analyses(session)

    assert selected == [(file, 6000.0)]


@pytest.mark.asyncio
async def test_select_incomplete_analyses_excludes_applied_rows(session: AsyncSession, make_file: Any) -> None:
    """A file with incomplete coverage that has ALREADY been applied/moved is excluded (finding #1)."""
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    await _seed_applied(session, file)

    assert await select_incomplete_analyses(session) == []


@pytest.mark.asyncio
async def test_count_null_windows_columns_rows(session: AsyncSession, make_file: Any) -> None:
    """Only fully-NULL rows count; a strided or complete row does not."""
    legacy = await make_file()
    await _seed_analysis(session, legacy)
    strided = await make_file()
    await _seed_analysis(session, strided, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    complete = await make_file()
    await _seed_analysis(session, complete, fine_analyzed=1, fine_total=1, coarse_analyzed=1, coarse_total=1)

    assert await count_null_windows_columns_rows(session) == 1


@pytest.mark.asyncio
async def test_count_applied_incomplete_analyses_rows(session: AsyncSession, make_file: Any) -> None:
    """Only applied (executed-proposal) incomplete-coverage rows count."""
    applied = await make_file()
    await _seed_analysis(session, applied, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    await _seed_applied(session, applied)
    unapplied = await make_file()
    await _seed_analysis(session, unapplied, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    complete_but_applied = await make_file()
    await _seed_analysis(session, complete_but_applied, fine_analyzed=1, fine_total=1, coarse_analyzed=1, coarse_total=1)
    await _seed_applied(session, complete_but_applied)

    # Only the incomplete-AND-applied row counts -- a complete row was never a backfill
    # candidate in the first place, applied or not.
    assert await count_applied_incomplete_analyses_rows(session) == 1


# ---------------------------------------------------------------------------
# enqueue_incomplete_reanalysis -- local routing (short/null duration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_empty_selection_returns_empty(session: AsyncSession) -> None:
    """No incomplete rows -> no routing attempted, empty report."""
    app_state = SimpleNamespace(task_router=FakeTaskRouter())

    assert await enqueue_incomplete_reanalysis(session, app_state) == []


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_queues_via_owning_agent(session: AsyncSession, make_file: Any) -> None:
    """A live owner queues a fresh ``process_file`` job on that agent's ``analyze`` lane."""
    await make_agent_live(session)
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    assert outcomes == [ReanalysisOutcome(file.id, file.original_path, "queued")]
    queue = router.queue_for("test-fileserver", "analyze")
    assert queue.captured[0][0] == "process_file"
    assert queue.captured_policy[0]["key"] == f"process_file:{file.id}"


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_on_outcome_called_incrementally(session: AsyncSession, make_file: Any) -> None:
    """``on_outcome`` fires per file AS PRODUCED, not just via the final returned list (durability, findings #3/4)."""
    await make_agent_live(session)
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)
    seen: list[ReanalysisOutcome] = []

    outcomes = await enqueue_incomplete_reanalysis(session, app_state, on_outcome=seen.append)

    assert seen == outcomes
    assert seen == [ReanalysisOutcome(file.id, file.original_path, "queued")]


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_idempotent_rerun_reports_in_flight(session: AsyncSession, make_file: Any) -> None:
    """A second run against the same (still in-flight) job dedups to ``in_flight``, never doubling."""
    await make_agent_live(session)
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)

    first = await enqueue_incomplete_reanalysis(session, app_state)
    second = await enqueue_incomplete_reanalysis(session, app_state)

    assert [o.outcome for o in first] == ["queued"]
    assert [o.outcome for o in second] == ["in_flight"]
    # Still exactly ONE captured enqueue on the queue -- the dedup makes the repeat a no-op.
    queue = router.queue_for("test-fileserver", "analyze")
    assert len(queue.captured) == 1


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_blocked_when_dead_job_holds_key(session: AsyncSession, make_file: Any) -> None:
    """A deterministic-key collision against a dead (aborting/failed/aborted) job classifies ``blocked``."""
    await make_agent_live(session)
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)

    await enqueue_incomplete_reanalysis(session, app_state)  # first: queues, key now live
    queue = router.queue_for("test-fileserver", "analyze")
    queue.job = AsyncMock(return_value=SimpleNamespace(status="aborting", stuck=False))

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    assert outcomes == [ReanalysisOutcome(file.id, file.original_path, "blocked")]


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_no_active_agent(session: AsyncSession, make_file: Any) -> None:
    """No live owning agent (never brought live via ``make_agent_live``) -> every file is ``no_active_agent``."""
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    app_state = SimpleNamespace(task_router=FakeTaskRouter())

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    assert outcomes == [ReanalysisOutcome(file.id, file.original_path, "no_active_agent")]


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_partial_skip_offline_owner_never_rerouted(session: AsyncSession, make_file: Any) -> None:
    """One live owner + one offline owner -> the offline owner's file is ``no_active_agent``, never rerouted (phaze-c9w9)."""
    await make_agent_live(session)
    live_owned = await make_file()
    await _seed_analysis(session, live_owned, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)

    session.add(Agent(id="test-fileserver-2", name="fs2", token_hash=None, kind="fileserver", scan_roots=[], last_seen_at=None, revoked_at=None))
    offline_owned = FileRecord(
        agent_id="test-fileserver-2",
        id=uuid.uuid4(),
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/test/music/{uuid.uuid4().hex}/set.mp3",
        original_filename="set.mp3",
        current_path="/test/music/set.mp3",
        file_type="mp3",
        file_size=1024,
    )
    session.add(offline_owned)
    await session.commit()
    await _seed_analysis(session, offline_owned, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)

    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    by_id = {o.file_id: o.outcome for o in outcomes}
    assert by_id[live_owned.id] == "queued"
    assert by_id[offline_owned.id] == "no_active_agent"
    # Never rerouted to the live owner's queue.
    assert len(router.queue_for("test-fileserver", "analyze").captured) == 1


# ---------------------------------------------------------------------------
# enqueue_incomplete_reanalysis -- containment (findings #3/#4, phaze-4ter / phaze-p2qvv)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_enqueue_error_contained(session: AsyncSession, make_file: Any) -> None:
    """A transient broker error enqueuing file N does not abort the run -- the rest still gets a full report."""
    await make_agent_live(session)
    doomed = await make_file()
    await _seed_analysis(session, doomed, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    survivor = await make_file()
    await _seed_analysis(session, survivor, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)
    queue = router.queue_for("test-fileserver", "analyze")
    real_enqueue = queue.enqueue

    async def _flaky_enqueue(task_name: str, **kwargs: Any) -> Any:
        if kwargs.get("file_id") == str(doomed.id):
            msg = "broker pool exhausted"
            raise RuntimeError(msg)
        return await real_enqueue(task_name, **kwargs)

    queue.enqueue = _flaky_enqueue  # type: ignore[method-assign]

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    by_id = {o.file_id: o.outcome for o in outcomes}
    assert by_id[doomed.id] == "enqueue_error"
    assert by_id[survivor.id] == "queued"


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_probe_failure_is_unknown(session: AsyncSession, make_file: Any) -> None:
    """A collision-classification probe failure degrades to ``unknown``, never escapes (phaze-p2qvv)."""
    await make_agent_live(session)
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)

    await enqueue_incomplete_reanalysis(session, app_state)  # first: queues, key now live
    queue = router.queue_for("test-fileserver", "analyze")
    queue.job = AsyncMock(side_effect=RuntimeError("connection reset"))

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    assert outcomes == [ReanalysisOutcome(file.id, file.original_path, "unknown")]


# ---------------------------------------------------------------------------
# enqueue_incomplete_reanalysis -- cloud routing (finding #2, CLOUDROUTE-02 / T-49-03)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_holds_long_file_for_cloud(
    session: AsyncSession, make_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long file (>= threshold, cloud enabled) is HELD, never enqueued locally."""
    settings = _set_backends(monkeypatch, [_COMPUTE_BACKEND])
    await make_agent_live(session)
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=60, fine_total=240, coarse_analyzed=30, coarse_total=30)
    await _seed_duration(session, file, settings.cloud_route_threshold_sec + 1)
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    assert outcomes == [ReanalysisOutcome(file.id, file.original_path, "awaiting_cloud")]
    # No local queue was ever touched for this file.
    assert router.queue_for("test-fileserver", "analyze").captured == []
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert cloud_job.status == CloudJobStatus.AWAITING.value


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_vanished_file_during_hold_is_skipped(
    session: AsyncSession, make_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file deleted concurrently while being held for the cloud drain is reported ``vanished``, not a crash.

    Mirrors ``tests/shared/routers/test_pipeline.py::
    test_route_discovered_by_duration_skips_a_file_deleted_before_its_hold``'s technique: wrap
    ``hold_awaiting_cloud`` to delete the row out from under the SAVEPOINT-guarded hold right before
    it runs, forcing the FK-bearing INSERT to raise ``IntegrityError``.
    """
    from sqlalchemy import delete

    settings = _set_backends(monkeypatch, [_COMPUTE_BACKEND])
    await make_agent_live(session)
    doomed = await make_file()
    await _seed_analysis(session, doomed, fine_analyzed=60, fine_total=240, coarse_analyzed=30, coarse_total=30)
    await _seed_duration(session, doomed, settings.cloud_route_threshold_sec + 1)
    doomed_id = doomed.id
    from phaze.services.backends import hold_awaiting_cloud as real_hold_awaiting_cloud

    async def _delete_then_hold(sess: AsyncSession, file: FileRecord, **kwargs: Any) -> bool:
        if file.id == doomed_id:
            await sess.execute(delete(FileRecord).where(FileRecord.id == doomed_id))
        return await real_hold_awaiting_cloud(sess, file, **kwargs)

    monkeypatch.setattr(reanalysis_backfill_mod, "hold_awaiting_cloud", _delete_then_hold)
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    assert outcomes == [ReanalysisOutcome(doomed.id, doomed.original_path, "vanished")]


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_short_file_still_routes_local_when_cloud_enabled(
    session: AsyncSession, make_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short/null-duration file routes local even with cloud enabled -- only long files are held."""
    _set_backends(monkeypatch, [_COMPUTE_BACKEND])
    await make_agent_live(session)
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    await _seed_duration(session, file, 60.0)  # well under the default 5400s threshold
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    assert outcomes == [ReanalysisOutcome(file.id, file.original_path, "queued")]
    assert (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).first() is None


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_null_duration_routes_local_even_when_long_would_hold(
    session: AsyncSession, make_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file with no metadata row yet (duration None) is never held -- ``is_long`` requires a known duration."""
    _set_backends(monkeypatch, [_COMPUTE_BACKEND])
    await make_agent_live(session)
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=1, fine_total=2, coarse_analyzed=1, coarse_total=1)
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    assert outcomes == [ReanalysisOutcome(file.id, file.original_path, "queued")]


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_skips_file_already_in_cloud_pipeline(
    session: AsyncSession, make_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file already carrying an ACTIVE cloud_job is skipped (T-82-A1 double-dispatch guard) -- routed no further."""
    settings = _set_backends(monkeypatch, [_COMPUTE_BACKEND])
    await make_agent_live(session)
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=60, fine_total=240, coarse_analyzed=30, coarse_total=30)
    await _seed_duration(session, file, settings.cloud_route_threshold_sec + 1)
    await _seed_active_cloud_job(session, file, CloudJobStatus.RUNNING.value)
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    assert outcomes == [ReanalysisOutcome(file.id, file.original_path, "in_cloud_pipeline")]
    # The pre-existing cloud_job row was never touched (still 'running', not re-stamped 'awaiting').
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert cloud_job.status == CloudJobStatus.RUNNING.value
    assert router.queue_for("test-fileserver", "analyze").captured == []


@pytest.mark.asyncio
async def test_enqueue_incomplete_reanalysis_force_local_overrides_cloud_enabled(
    session: AsyncSession, make_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The force-local route_control override folds in exactly like every other duration-router caller (Phase 71)."""
    from phaze.models.route_control import RouteControl

    settings = _set_backends(monkeypatch, [_COMPUTE_BACKEND])
    session.add(RouteControl(id="global", force_local=True))
    await session.commit()
    await make_agent_live(session)
    file = await make_file()
    await _seed_analysis(session, file, fine_analyzed=60, fine_total=240, coarse_analyzed=30, coarse_total=30)
    await _seed_duration(session, file, settings.cloud_route_threshold_sec + 1)
    router = DedupFakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)

    outcomes = await enqueue_incomplete_reanalysis(session, app_state)

    # Forced local: even a long file routes to the fileserver queue, never held.
    assert outcomes == [ReanalysisOutcome(file.id, file.original_path, "queued")]


# ---------------------------------------------------------------------------
# compute_exit_code (finding #7)
# ---------------------------------------------------------------------------


def test_compute_exit_code_no_candidates_is_success() -> None:
    """Genuinely nothing to do -> exit 0."""
    assert compute_exit_code([]) == 0


def test_compute_exit_code_some_queued_is_success() -> None:
    assert compute_exit_code([ReanalysisOutcome(uuid.uuid4(), "a.mp3", "queued")]) == 0


def test_compute_exit_code_awaiting_cloud_is_success() -> None:
    assert compute_exit_code([ReanalysisOutcome(uuid.uuid4(), "a.mp3", "awaiting_cloud")]) == 0


def test_compute_exit_code_in_cloud_pipeline_is_success() -> None:
    assert compute_exit_code([ReanalysisOutcome(uuid.uuid4(), "a.mp3", "in_cloud_pipeline")]) == 0


def test_compute_exit_code_in_flight_is_success() -> None:
    assert compute_exit_code([ReanalysisOutcome(uuid.uuid4(), "a.mp3", "in_flight")]) == 0


def test_compute_exit_code_all_no_active_agent_is_failure() -> None:
    """Candidates existed but nothing could be routed -- worth an operator's attention."""
    outcomes = [ReanalysisOutcome(uuid.uuid4(), "a.mp3", "no_active_agent"), ReanalysisOutcome(uuid.uuid4(), "b.mp3", "no_active_agent")]
    assert compute_exit_code(outcomes) == 1


def test_compute_exit_code_all_blocked_is_failure() -> None:
    assert compute_exit_code([ReanalysisOutcome(uuid.uuid4(), "a.mp3", "blocked")]) == 1


def test_compute_exit_code_mixed_productive_and_stuck_is_success() -> None:
    """One productive outcome among several stuck ones is still success -- SOME progress was made."""
    outcomes = [ReanalysisOutcome(uuid.uuid4(), "a.mp3", "no_active_agent"), ReanalysisOutcome(uuid.uuid4(), "b.mp3", "queued")]
    assert compute_exit_code(outcomes) == 0
