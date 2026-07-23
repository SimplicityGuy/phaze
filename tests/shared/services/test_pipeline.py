"""Tests for the pipeline orchestration service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import text

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.tracklist import Tracklist
from phaze.services import pipeline as pipeline_mod
from phaze.services.pipeline import (
    analyze_lanes_content_hash,
    count_active_agents,
    count_backfill_candidates,
    count_inflight_jobs,
    deduped_count,
    get_agent_recent_scans,
    get_agent_reconciliations,
    get_analysis_failed_count,
    get_analysis_failed_files,
    get_analyze_files_page,
    get_analyze_working_set,
    get_awaiting_cloud_count,
    get_backfill_candidates,
    get_discovered_files_with_duration,
    get_global_reconciliation,
    get_match_busy_count,
    get_match_pending_tracklists,
    get_metadata_pending_files,
    get_proposal_pending_batches,
    get_pushed_count,
    get_pushing_count,
    get_queue_activity,
    get_scan_busy_count,
    get_scanned_total,
    get_scrape_busy_count,
    get_scrape_pending_tracklists,
    get_search_busy_count,
    get_stage_busy_counts,
    get_stage_progress,
    get_straggler_count,
    get_untracked_files,
)
from tests._queue_fakes import FakeQueue, FakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# ANALYSIS_FAILED bucket (Phase 44, D-02; Phase 90 PR-A: derived from failed_clause) — count/list
# ---------------------------------------------------------------------------


def _failed_file(i: int) -> FileRecord:
    """Build a FileRecord seed in the given state (default ANALYSIS_FAILED)."""
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=f"f{i:063d}"[:64],
        original_path=f"/music/failed{i}.mp3",
        original_filename=f"failed{i}.mp3",
        current_path=f"/music/failed{i}.mp3",
        file_type="mp3",
        file_size=1000,
    )


def _failed_analysis_for(file_id: uuid.UUID) -> AnalysisResult:
    """Build an analyze-FAILURE marker (analysis row, ``failed_at`` set) for ``file_id`` (Phase 90 D-09).

    This is the DERIVED source the cutover reads via ``failed_clause(Stage.ANALYZE)`` -- an
    ``analysis`` row whose ``failed_at`` is non-NULL. Distinct from a completed row (``failed_at``
    NULL, ``analysis_completed_at`` set); the XOR CHECK forbids both being set.
    """
    return AnalysisResult(id=uuid.uuid4(), file_id=file_id, failed_at=datetime.now(UTC), error_message="boom")


def _completed_analysis_for(file_id: uuid.UUID, fine_done: int | None = None, fine_total: int | None = None) -> AnalysisResult:
    """Build a completed analyze marker (``analysis_completed_at`` set, ``failed_at`` NULL) for ``file_id``."""
    return AnalysisResult(
        id=uuid.uuid4(),
        file_id=file_id,
        analysis_completed_at=datetime.now(UTC),
        fine_windows_analyzed=fine_done,
        fine_windows_total=fine_total,
    )


@pytest.mark.asyncio
async def test_get_analysis_failed_count_happy_path(session: AsyncSession) -> None:
    """DERIVED count (Phase 90 D-09): counts files with an analyze-failure marker, not ``files.state``.

    Seeds a consistent corpus (legacy ``state`` AND the derived ``analysis.failed_at`` marker agree),
    then asserts the derived count equals the legacy count of 2 -- proving the reader now sources from
    ``failed_clause(Stage.ANALYZE)``. The ANALYZED file carries a completed marker (no ``failed_at``)
    and is excluded.
    """
    a, b, done = _failed_file(0), _failed_file(1), _failed_file(2)
    session.add_all([a, b, done])
    await session.flush()
    session.add_all([_failed_analysis_for(a.id), _failed_analysis_for(b.id), _completed_analysis_for(done.id)])
    await session.commit()
    assert await get_analysis_failed_count(session) == 2


@pytest.mark.asyncio
async def test_get_analysis_failed_files_returns_failed_rows(session: AsyncSession) -> None:
    """DERIVED (Phase 90 D-09): returns files with an analyze-failure marker, not ``files.state``.

    Seeds two files with the ``analysis.failed_at`` marker (the derived source ``failed_clause`` reads)
    and one non-failed file (no marker); asserts only the two marker-bearing files are returned.
    """
    a = _failed_file(0)
    b = _failed_file(1)
    c = _failed_file(2)
    session.add_all([a, b, c])
    await session.flush()
    session.add_all([_failed_analysis_for(a.id), _failed_analysis_for(b.id)])
    await session.commit()
    rows = await get_analysis_failed_files(session)
    assert {r.id for r in rows} == {a.id, b.id}


@pytest.mark.asyncio
async def test_get_analysis_failed_count_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades the count to 0 (poll-safe via _safe_count), never raising.

    Mirrors the _safe_count degrade discipline: the hot 5s /pipeline/stats poll must keep serving
    instead of 500ing when the files read fails.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("files table unavailable")

    assert await get_analysis_failed_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_queue_activity_sums_across_agents(session: AsyncSession):
    """agent_* fields sum queued+active over ALL non-revoked agents; controller is separate."""
    await seed_active_agent(session, "nox")
    await seed_active_agent(session, "lux")
    router = FakeTaskRouter()
    router.set_counts("nox", queued=3, active=2)
    router.set_counts("lux", queued=4, active=1)
    controller = FakeQueue("controller")
    controller.set_counts(queued=5, active=0)
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    assert activity["agent_queued"] == 7
    assert activity["agent_active"] == 3
    assert activity["agent_busy"] == 10
    assert activity["controller_queued"] == 5
    assert activity["controller_active"] == 0
    assert activity["controller_busy"] == 5


@pytest.mark.asyncio
async def test_get_queue_activity_excludes_scheduled(session: AsyncSession):
    """A large 'incomplete' (scheduled-inclusive) depth never changes the busy totals."""
    await seed_active_agent(session, "nox")
    router = FakeTaskRouter()
    # Seed the cached per-agent queue directly so we can also set a huge incomplete depth.
    router.queue_for("nox").set_counts(queued=3, active=2, incomplete=999)
    controller = FakeQueue("controller")
    controller.set_counts(queued=1, active=0, incomplete=999)
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    # Only queued+active are read; the 999 scheduled-inclusive depth is ignored.
    assert activity["agent_busy"] == 5
    assert activity["controller_busy"] == 1


@pytest.mark.asyncio
async def test_get_queue_activity_degrades_on_redis_error(session: AsyncSession):
    """A Redis error on every source degrades all six values to 0 without raising."""
    await seed_active_agent(session, "nox")
    router = FakeTaskRouter()
    router.set_counts("nox", queued=3, active=2)
    router.queue_for("nox").fail_count()
    controller = FakeQueue("controller")
    controller.set_counts(queued=5, active=0)
    controller.fail_count()
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    assert all(v == 0 for v in activity.values())


@pytest.mark.asyncio
async def test_get_queue_activity_degrades_on_missing_app_state(session: AsyncSession):
    """A SimpleNamespace lacking task_router/controller_queue degrades to all-zero."""
    await seed_active_agent(session, "nox")

    activity = await get_queue_activity(SimpleNamespace(), session)

    assert all(v == 0 for v in activity.values())


@pytest.mark.asyncio
async def test_get_queue_activity_controller_independent_of_agents(session: AsyncSession):
    """A controller-queue outage zeroes only the controller; agent depth stays intact."""
    await seed_active_agent(session, "nox")
    router = FakeTaskRouter()
    router.set_counts("nox", queued=3, active=2)
    controller = FakeQueue("controller")
    controller.set_counts(queued=5, active=0)
    controller.fail_count()
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    assert activity["agent_queued"] == 3
    assert activity["agent_active"] == 2
    assert activity["agent_busy"] == 5
    assert activity["controller_queued"] == 0
    assert activity["controller_active"] == 0
    assert activity["controller_busy"] == 0


@pytest.mark.asyncio
async def test_get_queue_activity_connects_runtime_registered_agent(session: AsyncSession):
    """A per-agent queue not pre-connected at startup is connected before counting.

    Regression (#217): ``main.py`` only opens pools for agents present at boot. A compute
    agent registered at runtime (``phaze agents add --kind compute``) has an unopened psycopg
    pool, so ``count`` raised ``PoolClosed`` and the whole agent source degraded to 0 (and
    logged ``queue_activity_degraded`` every 5s) until the api restarted. The reader must
    ``connect()`` (idempotent) before ``count`` -- mirroring ``enqueue_for_agent``.
    """
    await seed_active_agent(session, "nox")
    await seed_active_agent(session, "k8s-vox")
    router = FakeTaskRouter()
    router.set_counts("nox", queued=3, active=2)
    # k8s-vox models the runtime agent: its base-queue count raises until connect() opens the pool.
    router.queue_for("k8s-vox").require_connect().set_counts(queued=4, active=1)
    controller = FakeQueue("controller")
    controller.set_counts(queued=5, active=0)
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    # Both agents counted (7 queued, 3 active) -- NOT degraded to 0 by the unopened pool.
    assert activity["agent_queued"] == 7
    assert activity["agent_active"] == 3
    assert activity["agent_busy"] == 10
    assert activity["controller_busy"] == 5


@pytest.mark.asyncio
async def test_get_queue_activity_isolates_one_failing_agent(session: AsyncSession):
    """One agent's count failure zeroes only that agent, not the whole agent source.

    A single dead/unconnectable agent queue must not wipe every other agent's live depth
    from the 5s dashboard poll (the pathology that made a single runtime agent degrade the
    entire metric). Per-agent failure isolation, alongside the existing per-source split. (#217)
    """
    await seed_active_agent(session, "nox")
    await seed_active_agent(session, "k8s-vox")
    router = FakeTaskRouter()
    router.set_counts("nox", queued=3, active=2)
    router.queue_for("k8s-vox").set_counts(queued=99, active=99).fail_count()
    controller = FakeQueue("controller")
    controller.set_counts(queued=5, active=0)
    app_state = SimpleNamespace(task_router=router, controller_queue=controller)

    activity = await get_queue_activity(app_state, session)

    # nox's real depth survives; only the failing k8s-vox contributes 0.
    assert activity["agent_queued"] == 3
    assert activity["agent_active"] == 2
    assert activity["agent_busy"] == 5
    assert activity["controller_busy"] == 5


# ---------------------------------------------------------------------------
# get_stage_busy_counts (t7k FIX2) — per-stage in-flight gate, degrade-safe
# ---------------------------------------------------------------------------


class _NullSavepoint:
    """Async-context-manager stand-in for ``session.begin_nested()`` in the fake-session tests.

    ``__aexit__`` returns ``False`` so an exception raised inside the ``async with`` block (the
    saq_jobs read) propagates out to ``get_stage_busy_counts``'s degrade ``except`` — exactly as a
    real SAVEPOINT does after ``ROLLBACK TO SAVEPOINT``.
    """

    async def __aenter__(self) -> _NullSavepoint:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_get_stage_busy_counts_buckets_by_function_prefix() -> None:
    """Rows are bucketed by the deterministic-key function prefix; non-stage functions are ignored.

    saq_jobs has NO function column — the key is ``<function>:<file_id>`` (Phase 35), so the SELECT
    groups by ``split_part(key, ':', 1)`` and each agent-stage function prefix maps back to its stage.
    ``generate_proposals`` / ``scan_directory`` are NOT agent stages, so they fall through and the
    seeded ``fingerprint`` (no rows) stays 0.
    """

    class _FakeResult:
        def __init__(self, rows: list[tuple[str, int]]) -> None:
            self._rows = rows

        def all(self) -> list[tuple[str, int]]:
            return self._rows

    class _FakeSession:
        def __init__(self, rows: list[tuple[str, int]]) -> None:
            self._rows = rows

        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
            return _FakeResult(self._rows)

    rows = [
        ("extract_file_metadata", 4),
        ("process_file", 2),
        ("generate_proposals", 9),  # not an agent stage → ignored
        ("scan_directory", 3),  # not an agent stage → ignored
    ]
    counts = await get_stage_busy_counts(_FakeSession(rows))  # type: ignore[arg-type]
    assert counts == {"metadata": 4, "analyze": 2, "fingerprint": 0}


@pytest.mark.asyncio
async def test_get_stage_busy_counts_degrades_on_db_error() -> None:
    """get_stage_busy_counts returns all-zeros and never raises when the saq_jobs read fails.

    A missing ``saq_jobs`` table or a DB hiccup must degrade to
    ``{"metadata":0,"analyze":0,"fingerprint":0}`` (T-t7k-02) so the hot 5s /pipeline/stats poll
    keeps serving instead of 500ing. The read runs inside a SAVEPOINT (``begin_nested``); the
    exception propagates out of the nested scope and is caught by the degrade ``except``.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "saq_jobs" does not exist')

    counts = await get_stage_busy_counts(_ExplodingSession())  # type: ignore[arg-type]
    assert counts == {"metadata": 0, "analyze": 0, "fingerprint": 0}


@pytest.mark.asyncio
async def test_get_stage_busy_counts_degrade_does_not_poison_session(session: AsyncSession) -> None:
    """The SAVEPOINT degrade leaves the outer transaction usable (no ORM-expiring rollback).

    ``saq_jobs`` may already exist in the shared test DB (a prior real-broker integration test
    creates it via SAQ ``init_db``), so DROP it inside this test's uncommitted transaction to
    deterministically force the absent-table degrade — the only branch that exercises the SAVEPOINT
    rollback recovery. The DROP is rolled back when the session closes, so it never leaks. A
    follow-up query on the SAME session must still succeed — proving the dashboard's later ORM
    lazy-loads are not poisoned (the bug a plain ``session.rollback()`` would cause: a 500 on the
    next access).
    """
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    counts = await get_stage_busy_counts(session)
    assert counts == {"metadata": 0, "analyze": 0, "fingerprint": 0}
    # The outer transaction is intact after the SAVEPOINT rollback: a normal query still runs.
    follow_up = await get_stage_progress(session)
    assert follow_up["discovery"]["done"] == 0


# ---------------------------------------------------------------------------
# get_search_busy_count (Phase 39, REQ-39-3) — search_tracklist in-flight gate, degrade-safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_search_busy_count_buckets_by_search_prefix() -> None:
    """Returns ONLY the ``search_tracklist`` in-flight count; other function prefixes are ignored.

    search_tracklist is a CONTROLLER task (not an agent stage), so it is absent from
    ``get_stage_busy_counts``'s {metadata,analyze,fingerprint} contract. The key is
    ``search_tracklist:<file_id>`` (Phase 35), so the SELECT groups by ``split_part(key, ':', 1)``
    and only the ``search_tracklist`` bucket is summed.
    """

    class _FakeResult:
        def __init__(self, rows: list[tuple[str, int]]) -> None:
            self._rows = rows

        def all(self) -> list[tuple[str, int]]:
            return self._rows

    class _FakeSession:
        def __init__(self, rows: list[tuple[str, int]]) -> None:
            self._rows = rows

        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
            return _FakeResult(self._rows)

    rows = [
        ("search_tracklist", 7),
        ("extract_file_metadata", 4),  # not search → ignored
        ("scrape_and_store_tracklist", 3),  # not search → ignored
    ]
    assert await get_search_busy_count(_FakeSession(rows)) == 7  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_search_busy_count_zero_when_no_search_rows() -> None:
    """With no ``search_tracklist`` rows the in-flight count is 0 (not an error)."""

    class _FakeResult:
        def all(self) -> list[tuple[str, int]]:
            return [("extract_file_metadata", 4)]

    class _FakeSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
            return _FakeResult()

    assert await get_search_busy_count(_FakeSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_search_busy_count_degrades_on_db_error() -> None:
    """get_search_busy_count returns 0 and never raises when the saq_jobs read fails (T-39-03).

    A missing ``saq_jobs`` table or a DB hiccup must degrade to 0 so the hot 5s /pipeline/stats poll
    keeps serving instead of 500ing. The read runs inside a SAVEPOINT (``begin_nested``); the
    exception propagates out of the nested scope and is caught by the degrade ``except``.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "saq_jobs" does not exist')

    assert await get_search_busy_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_search_busy_count_degrade_does_not_poison_session(session: AsyncSession) -> None:
    """The SAVEPOINT degrade leaves the outer transaction usable (mirrors the stage-busy guard).

    DROP ``saq_jobs`` inside this test's uncommitted transaction to deterministically force the
    absent-table degrade — the only branch that exercises the SAVEPOINT rollback recovery. A
    follow-up query on the SAME session must still succeed, proving the dashboard's later ORM
    lazy-loads are not poisoned (the bug a plain ``session.rollback()`` would cause).
    """
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    assert await get_search_busy_count(session) == 0
    # The outer transaction is intact after the SAVEPOINT rollback: a normal query still runs.
    follow_up = await get_stage_progress(session)
    assert follow_up["discovery"]["done"] == 0


# ---------------------------------------------------------------------------
# get_scan_busy_count (Phase 40, REQ-40-3) — scan_live_set in-flight gate, degrade-safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scan_busy_count_buckets_by_scan_prefix() -> None:
    """Returns ONLY the ``scan_live_set`` in-flight count; other function prefixes are ignored.

    scan_live_set is a PER-AGENT task (not one of get_stage_busy_counts's three agent stages) but its
    jobs live in the SAME saq_jobs table. The key is ``scan_live_set:<file_id>`` (Phase 35), so the
    SELECT groups by ``split_part(key, ':', 1)`` and only the ``scan_live_set`` bucket is summed.
    """

    class _FakeResult:
        def __init__(self, rows: list[tuple[str, int]]) -> None:
            self._rows = rows

        def all(self) -> list[tuple[str, int]]:
            return self._rows

    class _FakeSession:
        def __init__(self, rows: list[tuple[str, int]]) -> None:
            self._rows = rows

        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
            return _FakeResult(self._rows)

    rows = [
        ("scan_live_set", 5),
        ("search_tracklist", 7),  # not scan → ignored
        ("extract_file_metadata", 4),  # not scan → ignored
    ]
    assert await get_scan_busy_count(_FakeSession(rows)) == 5  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_scan_busy_count_zero_when_no_scan_rows() -> None:
    """With no ``scan_live_set`` rows the in-flight count is 0 (not an error)."""

    class _FakeResult:
        def all(self) -> list[tuple[str, int]]:
            return [("search_tracklist", 7)]

    class _FakeSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
            return _FakeResult()

    assert await get_scan_busy_count(_FakeSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_scan_busy_count_degrades_on_db_error() -> None:
    """get_scan_busy_count returns 0 and never raises when the saq_jobs read fails (T-40-03).

    A missing ``saq_jobs`` table or a DB hiccup must degrade to 0 so the hot 5s /pipeline/stats poll
    keeps serving instead of 500ing. The read runs inside a SAVEPOINT (``begin_nested``); the
    exception propagates out of the nested scope and is caught by the degrade ``except``.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "saq_jobs" does not exist')

    assert await get_scan_busy_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_scan_busy_count_degrade_does_not_poison_session(session: AsyncSession) -> None:
    """The SAVEPOINT degrade leaves the outer transaction usable (mirrors the search-busy guard).

    DROP ``saq_jobs`` inside this test's uncommitted transaction to deterministically force the
    absent-table degrade — the only branch that exercises the SAVEPOINT rollback recovery. A
    follow-up query on the SAME session must still succeed, proving the dashboard's later ORM
    lazy-loads are not poisoned (the bug a plain ``session.rollback()`` would cause).
    """
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    assert await get_scan_busy_count(session) == 0
    # The outer transaction is intact after the SAVEPOINT rollback: a normal query still runs.
    follow_up = await get_stage_progress(session)
    assert follow_up["discovery"]["done"] == 0


# ---------------------------------------------------------------------------
# count_active_agents (Phase 40, REQ-40-2) — online-agent liveness count, degrade-safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_active_agents_excludes_revoked_and_never_seen(session: AsyncSession) -> None:
    """Counts ONLY agents matching select_active_agent's liveness (revoked + never-seen excluded).

    Seeds one online agent (recently seen, not revoked), one revoked agent (``revoked_at`` set), and
    one never-seen agent (``last_seen_at`` None) — the count must be exactly 1, reusing the EXACT
    enqueue_router liveness rule (CONTEXT decision 2).
    """
    from datetime import UTC, datetime

    from phaze.models.agent import Agent

    await seed_active_agent(session, "nox")  # online: revoked_at NULL, last_seen_at set
    session.add(Agent(id="revoked", name="revoked", token_hash=None, scan_roots=[], last_seen_at=datetime.now(UTC), revoked_at=datetime.now(UTC)))
    session.add(Agent(id="never-seen", name="never-seen", token_hash=None, scan_roots=[], last_seen_at=None, revoked_at=None))
    await session.flush()

    assert await count_active_agents(session) == 1


@pytest.mark.asyncio
async def test_count_active_agents_zero_when_none_online(session: AsyncSession) -> None:
    """With no online agents the count is 0 (fail-safe default leaves the node blocked 'Needs agent')."""
    assert await count_active_agents(session) == 0


@pytest.mark.asyncio
async def test_count_active_agents_degrades_on_db_error() -> None:
    """count_active_agents returns 0 and never raises when the agents read fails (T-40-05).

    The degrade default 0 is FAIL-SAFE: ``agentOnline == 0`` leaves the new node blocked 'Needs
    agent', so a liveness-read failure can never let a scan launch with no agent online. The read
    runs inside a SAVEPOINT (``begin_nested``); the exception is caught by the degrade ``except``.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("agents table unavailable")

    assert await count_active_agents(_ExplodingSession()) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get_scrape_busy_count / get_match_busy_count (Phase 41, REQ-41-3) — controller-task
# in-flight gates over the SAME saq_jobs table, degrade-safe (mirror get_search_busy_count).
# ---------------------------------------------------------------------------


class _BusyResult:
    """Minimal ``.all()`` result double over a fixed list of ``(fn_prefix, count)`` rows."""

    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, int]]:
        return self._rows


class _BusySession:
    """Fake session whose ``execute`` returns the seeded grouped-prefix rows inside a SAVEPOINT."""

    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self._rows = rows

    def begin_nested(self) -> _NullSavepoint:
        return _NullSavepoint()

    async def execute(self, *_args: object, **_kwargs: object) -> _BusyResult:
        return _BusyResult(self._rows)


@pytest.mark.asyncio
async def test_get_scrape_busy_count_buckets_by_scrape_prefix() -> None:
    """Returns ONLY the ``scrape_and_store_tracklist`` in-flight count; other prefixes are ignored.

    scrape_and_store_tracklist is a CONTROLLER task (not an agent stage), so it is absent from
    ``get_stage_busy_counts``'s {metadata,analyze,fingerprint} contract. The key is
    ``scrape_and_store_tracklist:<tracklist_id>`` (Phase 35), so the SELECT groups by
    ``split_part(key, ':', 1)`` and only the ``scrape_and_store_tracklist`` bucket is summed.
    """
    rows = [
        ("scrape_and_store_tracklist", 4),
        ("search_tracklist", 7),  # not scrape → ignored
        ("match_tracklist_to_discogs", 2),  # not scrape → ignored
    ]
    assert await get_scrape_busy_count(_BusySession(rows)) == 4  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_scrape_busy_count_zero_when_no_scrape_rows() -> None:
    """With no ``scrape_and_store_tracklist`` rows the in-flight count is 0 (not an error)."""
    assert await get_scrape_busy_count(_BusySession([("match_tracklist_to_discogs", 3)])) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_scrape_busy_count_degrades_on_db_error() -> None:
    """get_scrape_busy_count returns 0 and never raises when the saq_jobs read fails (T-41-03).

    A missing ``saq_jobs`` table or a DB hiccup must degrade to 0 so the hot 5s /pipeline/stats poll
    keeps serving instead of 500ing. The read runs inside a SAVEPOINT (``begin_nested``); the
    exception propagates out of the nested scope and is caught by the degrade ``except``.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "saq_jobs" does not exist')

    assert await get_scrape_busy_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_scrape_busy_count_degrade_does_not_poison_session(session: AsyncSession) -> None:
    """The SAVEPOINT degrade leaves the outer transaction usable (mirrors the search-busy guard)."""
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    assert await get_scrape_busy_count(session) == 0
    follow_up = await get_stage_progress(session)
    assert follow_up["discovery"]["done"] == 0


@pytest.mark.asyncio
async def test_get_match_busy_count_buckets_by_match_prefix() -> None:
    """Returns ONLY the ``match_tracklist_to_discogs`` in-flight count; other prefixes are ignored."""
    rows = [
        ("match_tracklist_to_discogs", 6),
        ("scrape_and_store_tracklist", 4),  # not match → ignored
        ("search_tracklist", 7),  # not match → ignored
    ]
    assert await get_match_busy_count(_BusySession(rows)) == 6  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_match_busy_count_zero_when_no_match_rows() -> None:
    """With no ``match_tracklist_to_discogs`` rows the in-flight count is 0 (not an error)."""
    assert await get_match_busy_count(_BusySession([("scrape_and_store_tracklist", 4)])) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_match_busy_count_degrades_on_db_error() -> None:
    """get_match_busy_count returns 0 and never raises when the saq_jobs read fails (T-41-03)."""

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "saq_jobs" does not exist')

    assert await get_match_busy_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_match_busy_count_degrade_does_not_poison_session(session: AsyncSession) -> None:
    """The SAVEPOINT degrade leaves the outer transaction usable (mirrors the search-busy guard)."""
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    assert await get_match_busy_count(session) == 0
    follow_up = await get_stage_progress(session)
    assert follow_up["discovery"]["done"] == 0


# ---------------------------------------------------------------------------
# get_scrape_pending_tracklists / get_match_pending_tracklists (Phase 41, REQ-41-1/REQ-41-2) —
# the exact complements of get_stage_progress scrape.done / match.done.
# ---------------------------------------------------------------------------


def _make_tracklist(n: int) -> Tracklist:
    """Build a bare Tracklist row (no version, no discogs chain)."""
    uid = uuid.uuid4()
    return Tracklist(id=uid, external_id=f"tl-{n}-{uid.hex}", source_url=f"http://x/{n}")


@pytest.mark.asyncio
async def test_get_scrape_pending_tracklists_excludes_versioned(session: AsyncSession) -> None:
    """Scrape pending = tracklists with NO tracklist_versions row; a versioned tracklist is excluded."""
    from phaze.models.tracklist import TracklistVersion

    pending = _make_tracklist(1)
    scraped = _make_tracklist(2)
    session.add_all([pending, scraped])
    await session.flush()
    session.add(TracklistVersion(id=uuid.uuid4(), tracklist_id=scraped.id, version_number=1))
    await session.flush()

    result = await get_scrape_pending_tracklists(session)
    ids = {tl.id for tl in result}
    assert pending.id in ids
    assert scraped.id not in ids


@pytest.mark.asyncio
async def test_get_match_pending_tracklists_excludes_discogs_reachable(session: AsyncSession) -> None:
    """Match pending = tracklists NOT reachable from discogs_links; a linked tracklist is excluded.

    The match-reachable chain is version → TracklistTrack → DiscogsLink (the SAME join-walk
    get_stage_progress.match.done uses). A tracklist with no discogs chain stays pending even if it
    HAS a scraped version (scrape and match are independent stages).
    """
    from phaze.models.discogs_link import DiscogsLink
    from phaze.models.tracklist import TracklistTrack, TracklistVersion

    pending = _make_tracklist(1)
    linked = _make_tracklist(2)
    session.add_all([pending, linked])
    await session.flush()
    # `pending` gets a version (scrape-done) but NO discogs link → still match-pending.
    session.add(TracklistVersion(id=uuid.uuid4(), tracklist_id=pending.id, version_number=1))
    linked_version = TracklistVersion(id=uuid.uuid4(), tracklist_id=linked.id, version_number=1)
    session.add(linked_version)
    await session.flush()
    track = TracklistTrack(id=uuid.uuid4(), version_id=linked_version.id, position=1)
    session.add(track)
    await session.flush()
    session.add(DiscogsLink(id=uuid.uuid4(), track_id=track.id, discogs_release_id="r1", confidence=0.9))
    await session.flush()

    result = await get_match_pending_tracklists(session)
    ids = {tl.id for tl in result}
    assert pending.id in ids
    assert linked.id not in ids


# ---------------------------------------------------------------------------
# Phase 42 (D-03 anti-drift): shared pending-set helpers + queue-loss detector.
# These four helpers are the ONE source of truth the manual DAG triggers AND the
# recovery producer read, so the two paths cannot drift apart.
# ---------------------------------------------------------------------------


def _make_pipeline_file(*, file_type: str = "mp3") -> FileRecord:
    """Build a fully-populated FileRecord row for the pending-set helper tests."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
    )


@pytest.mark.asyncio
async def test_get_metadata_pending_files_returns_only_music_video(session: AsyncSession) -> None:
    """Metadata pending = every music/video file (any state); a non-music file_type is excluded."""
    music = _make_pipeline_file(file_type="mp3")
    other = _make_pipeline_file(file_type="txt")
    session.add_all([music, other])
    await session.flush()

    result = await get_metadata_pending_files(session)
    ids = {f.id for f in result}
    assert music.id in ids
    assert other.id not in ids


@pytest.mark.asyncio
async def test_get_untracked_files_excludes_files_with_tracklist(session: AsyncSession) -> None:
    """Untracked = music/video files with NO Tracklist row; a tracked file and a non-music file are out."""
    untracked = _make_pipeline_file(file_type="mp3")
    tracked = _make_pipeline_file(file_type="mp3")
    non_music = _make_pipeline_file(file_type="txt")
    session.add_all([untracked, tracked, non_music])
    await session.flush()
    session.add(Tracklist(id=uuid.uuid4(), file_id=tracked.id, external_id="tl-1", source_url="http://x/1"))
    await session.flush()

    result = await get_untracked_files(session)
    ids = {f.id for f in result}
    assert untracked.id in ids
    assert tracked.id not in ids
    assert non_music.id not in ids


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_sorts_then_chunks(session: AsyncSession) -> None:
    """Convergence files (metadata+analysis) are SORTED by id then chunked -- deterministic batches.

    Sorting before chunking is what aligns the generate_proposals:<sha256(sorted ids)> set-hash key
    between the manual trigger and recovery (42-RESEARCH Pitfall 2). Insert in arbitrary order and
    assert the batches are globally sorted and chunked by batch_size.
    """
    files = [_make_pipeline_file() for _ in range(3)]
    session.add_all(files)
    await session.flush()
    related: list[object] = []
    for f in files:
        related.append(FileMetadata(file_id=f.id, artist="A", title="T"))
        # Phase 57.1: a COMPLETED analysis row carries analysis_completed_at -- the convergence
        # gate now requires it IS NOT NULL, so the positive control must stamp it.
        related.append(AnalysisResult(file_id=f.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))
    session.add_all(related)
    await session.flush()

    batches = await get_proposal_pending_batches(session, 2)

    flat = [fid for batch in batches for fid in batch]
    expected = sorted(str(f.id) for f in files)
    assert flat == expected  # globally sorted, deterministic membership
    assert [len(b) for b in batches] == [2, 1]  # 3 ids / batch_size 2


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_excludes_files_missing_metadata_or_analysis(session: AsyncSession) -> None:
    """Convergence gate: a file with ONLY metadata (no analysis) is NOT batched."""
    only_metadata = _make_pipeline_file()
    session.add(only_metadata)
    await session.flush()
    session.add(FileMetadata(file_id=only_metadata.id, artist="A", title="T"))
    await session.flush()

    batches = await get_proposal_pending_batches(session, 10)
    flat = [fid for batch in batches for fid in batch]
    assert str(only_metadata.id) not in flat


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_excludes_partial_analysis_row(session: AsyncSession) -> None:
    """Phase 57.1 KEY RISK: a METADATA_EXTRACTED file with a PARTIAL analysis row is NOT batched.

    Under D-03 an `analysis` row is upserted at analysis START (NULL aggregates, fine_windows_analyzed
    < total, analysis_completed_at NULL) while the file is still METADATA_EXTRACTED. That row would
    satisfy the old bare `exists(AnalysisResult)` gate and leak into generate_proposals with NULL
    bpm/key/mood. The tightened gate (analysis_completed_at IS NOT NULL) must return it in ZERO batches.
    Positive control: once analysis_completed_at is stamped, the SAME file appears -- proving the
    tighten did not over-exclude legitimate completed files.
    """
    pending = _make_pipeline_file()
    session.add(pending)
    await session.flush()
    session.add(FileMetadata(file_id=pending.id, artist="A", title="T"))
    # Partial in-flight row: NULL bpm, analyzed < total, NO completion stamp.
    partial = AnalysisResult(file_id=pending.id, bpm=None, fine_windows_analyzed=3, fine_windows_total=40, analysis_completed_at=None)
    session.add(partial)
    await session.flush()

    batches = await get_proposal_pending_batches(session, 10)
    flat = [fid for batch in batches for fid in batch]
    assert str(pending.id) not in flat, "a partial (in-flight) analysis row must NOT leak into proposal batches"

    # Positive control: stamping completion makes the same file eligible.
    partial.analysis_completed_at = datetime.now(UTC)
    await session.flush()
    batches_after = await get_proposal_pending_batches(session, 10)
    flat_after = [fid for batch in batches_after for fid in batch]
    assert str(pending.id) in flat_after, "a completed analysis row MUST appear (tighten did not over-exclude)"


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_excludes_already_proposed_file(session: AsyncSession) -> None:
    """Phase 90 (PR-A, Pitfall 4): a file with an EXISTING proposal is NOT re-batched.

    The retired ``files.state IN (ANALYZED, METADATA_EXTRACTED)`` gate is replaced by
    ``~done_clause(Stage.PROPOSE)`` -- ``done_clause(PROPOSE)`` is "a RenameProposal row exists", so a
    file that already has a proposal is a DONE propose and MUST be excluded (no re-propose), even though
    it still carries its converging metadata + completed-analysis rows. A twin file with no proposal is
    the positive control that appears.
    """
    proposed = _make_pipeline_file()
    unproposed = _make_pipeline_file()
    session.add_all([proposed, unproposed])
    await session.flush()
    for f in (proposed, unproposed):
        session.add(FileMetadata(file_id=f.id, artist="A", title="T"))
        session.add(AnalysisResult(file_id=f.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))
    # Only ``proposed`` already carries a RenameProposal -> done_clause(PROPOSE) True -> excluded.
    session.add(RenameProposal(id=uuid.uuid4(), file_id=proposed.id, proposed_filename="x.mp3", status=ProposalStatus.PENDING.value))
    await session.flush()

    batches = await get_proposal_pending_batches(session, 10)
    flat = [fid for batch in batches for fid in batch]
    assert str(proposed.id) not in flat, "an already-proposed file must NOT be re-batched (Pitfall 4)"
    assert str(unproposed.id) in flat, "a not-yet-proposed converged file MUST still be batched"


def _backend(backend_id: str, kind: str) -> SimpleNamespace:
    """A minimal registry-entry stand-in — non_local_backend_kinds reads only ``.id`` / ``.kind``."""
    return SimpleNamespace(id=backend_id, kind=kind)


def _backend_settings(*backends: SimpleNamespace) -> SimpleNamespace:
    """A minimal settings stand-in carrying only ``.backends`` (the sole attribute the derivation reads)."""
    return SimpleNamespace(backends=list(backends))


@pytest.mark.asyncio
async def test_get_analyze_working_set_derives_flags_from_markers(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 90 (PR-A) + COMPUTE-03 + Phase 95 (phaze-zqvh.2): working-set rows + flags DERIVE from markers/cloud_job, not ``files.state``.

    Seeds four files whose ``files.state`` is deliberately a neutral ``DISCOVERED`` (proving the read no
    longer consults it): a completed analysis (``analysis_completed_at`` set -- surfaced via the bounded
    recent-completions window), a failed analysis (``failed_at`` set -- working set), an awaiting-cloud
    sidecar with NO ``backend_id`` stamped yet (``cloud_job(status='awaiting')`` -- working set), and an
    off-stage file with NO analysis/cloud_job (must be absent). Asserts membership + the derived
    ``completed`` / ``analysis_failed`` / ``awaiting_cloud`` flags, that no raw ``state`` key is exposed,
    and that the unattributed cloud_job (no backend_id) gets the truthful ``lane="cloud"`` fallback --
    never the stale ``"a1"`` heuristic label.
    """
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _backend_settings(_backend("vox", "kueue")))

    done_f = _make_pipeline_file()
    failed_f = _make_pipeline_file()
    awaiting_f = _make_pipeline_file()
    off_stage = _make_pipeline_file()
    session.add_all([done_f, failed_f, awaiting_f, off_stage])
    await session.flush()
    session.add(_completed_analysis_for(done_f.id, fine_done=40, fine_total=40))
    session.add(_failed_analysis_for(failed_f.id))
    session.add(CloudJob(id=uuid.uuid4(), file_id=awaiting_f.id, status=CloudJobStatus.AWAITING.value))
    await session.commit()

    rows = (await get_analyze_working_set(session)).rows
    by_id = {r["file_id"]: r for r in rows}

    assert str(off_stage.id) not in by_id, "a file with no analysis/cloud_job marker is not in the Analyze stage"
    assert "state" not in by_id[str(done_f.id)], "the raw scalar-state key must be gone (derived flags replace it)"

    assert by_id[str(done_f.id)]["completed"] is True
    assert by_id[str(done_f.id)]["analysis_failed"] is False
    assert by_id[str(done_f.id)]["awaiting_cloud"] is False
    assert by_id[str(done_f.id)]["lane"] == "local", "no cloud_job row -> local lane"
    assert by_id[str(done_f.id)]["lane_kind"] == "local"

    assert by_id[str(failed_f.id)]["analysis_failed"] is True
    assert by_id[str(failed_f.id)]["completed"] is False

    assert by_id[str(awaiting_f.id)]["awaiting_cloud"] is True
    assert by_id[str(awaiting_f.id)]["completed"] is False
    assert by_id[str(awaiting_f.id)]["lane"] == "cloud", "NULL backend_id -> truthful unattributed 'cloud' fallback, never 'a1'"
    assert by_id[str(awaiting_f.id)]["lane_kind"] == "cloud"


@pytest.mark.asyncio
async def test_get_analyze_working_set_lane_derives_from_backend_id(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """COMPUTE-03: a stamped ``backend_id`` drives both ``lane`` (the id) and ``lane_kind`` (registry kind).

    A kueue-registered backend id ("vox") yields ``lane="vox"`` / ``lane_kind="kueue"``; a backend id
    that is no longer in the registry falls back to ``lane_kind="cloud"`` (deregistered cluster, still
    truthfully labeled as cloud rather than crashing or reintroducing a heuristic).
    """
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _backend_settings(_backend("vox", "kueue")))

    kueue_f = _make_pipeline_file()
    stale_backend_f = _make_pipeline_file()
    session.add_all([kueue_f, stale_backend_f])
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=kueue_f.id, status=CloudJobStatus.RUNNING.value, backend_id="vox", cloud_phase="running"))
    session.add(
        CloudJob(
            id=uuid.uuid4(), file_id=stale_backend_f.id, status=CloudJobStatus.RUNNING.value, backend_id="retired-cluster", cloud_phase="running"
        )
    )
    await session.commit()

    rows = (await get_analyze_working_set(session)).rows
    by_id = {r["file_id"]: r for r in rows}

    assert by_id[str(kueue_f.id)]["lane"] == "vox"
    assert by_id[str(kueue_f.id)]["lane_kind"] == "kueue"

    assert by_id[str(stale_backend_f.id)]["lane"] == "retired-cluster"
    assert by_id[str(stale_backend_f.id)]["lane_kind"] == "cloud", "a backend_id no longer in the registry falls back to 'cloud', never crashes"


def _inflight_analysis_for(file_id: uuid.UUID, fine_done: int = 5, fine_total: int = 10) -> AnalysisResult:
    """Build a mid-flight analyze marker: an ``analysis`` row with NO completed/failed timestamp (57.1 N/M)."""
    return AnalysisResult(id=uuid.uuid4(), file_id=file_id, fine_windows_analyzed=fine_done, fine_windows_total=fine_total)


@pytest.mark.asyncio
async def test_get_analyze_working_set_bounds_completions_window(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zqvh.2: the DEFAULT view never returns the whole completed corpus -- completions are LIMIT-ed.

    Seeds MANY completed files (more than the window) plus the active working set (in-flight + failed +
    awaiting-cloud). phaze-5462: the active working set is now PAGED rather than "returned IN FULL
    because it is naturally bounded" -- that claim was false and is the bug this bead fixed. Here the
    3 active rows fit one page, so the total is ``active (3) + window (2)`` and the completed set is
    still capped at ``completions_limit``.
    """
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _backend_settings())

    # Active working set: 1 in-flight + 1 failed + 1 awaiting-cloud (all must appear, unbounded).
    inflight_f = _make_pipeline_file()
    failed_f = _make_pipeline_file()
    awaiting_f = _make_pipeline_file()
    # Completed set: 6 finished files -- MORE than the window of 2 below.
    completed = [_make_pipeline_file() for _ in range(6)]
    session.add_all([inflight_f, failed_f, awaiting_f, *completed])
    await session.flush()
    session.add(_inflight_analysis_for(inflight_f.id))
    session.add(_failed_analysis_for(failed_f.id))
    session.add(CloudJob(id=uuid.uuid4(), file_id=awaiting_f.id, status=CloudJobStatus.AWAITING.value))
    for f in completed:
        session.add(_completed_analysis_for(f.id, fine_done=40, fine_total=40))
    await session.commit()

    rows = (await get_analyze_working_set(session, completions_limit=2)).rows
    by_id = {r["file_id"]: r for r in rows}

    # The full active working set is present (in-flight / failed / awaiting-cloud), regardless of window.
    assert str(inflight_f.id) in by_id and not by_id[str(inflight_f.id)]["completed"]
    assert str(failed_f.id) in by_id and by_id[str(failed_f.id)]["analysis_failed"]
    assert str(awaiting_f.id) in by_id and by_id[str(awaiting_f.id)]["awaiting_cloud"]
    # The completed set is BOUNDED to the window (2), not all 6 -- the corpus never lands in the DOM.
    completed_returned = [r for r in rows if r["completed"]]
    assert len(completed_returned) == 2, "completed files beyond the window must NOT be returned by the default view"
    # Total is working-set (3) + window (2) == 5, never the full membership (9).
    assert len(rows) == 5


@pytest.mark.asyncio
async def test_get_analyze_working_set_is_active_first(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zqvh.2: active work renders BEFORE the recent-completions window (active-first ordering)."""
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _backend_settings())

    inflight_f = _make_pipeline_file()
    completed_f = _make_pipeline_file()
    session.add_all([inflight_f, completed_f])
    await session.flush()
    session.add(_inflight_analysis_for(inflight_f.id))
    session.add(_completed_analysis_for(completed_f.id, fine_done=40, fine_total=40))
    await session.commit()

    rows = (await get_analyze_working_set(session)).rows
    order = [r["file_id"] for r in rows]
    assert order.index(str(inflight_f.id)) < order.index(str(completed_f.id)), "active work must render before completions"


@pytest.mark.asyncio
async def test_get_analyze_files_page_paginates_without_overlap(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zqvh.2: the full-listing pager is bounded (page_size) with a +1 sentinel has_next, no overlap.

    ``page_size`` is clamped to a 10-row floor (parity with ``get_files_page``), so 25 completed files at
    ``page_size=10`` partition into 10 + 10 + 5: page1/page2 carry ``has_next`` (the +1 sentinel), page3 is
    the last. The three pages partition the corpus with NO duplicated or skipped id (the id tiebreaker).
    """
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _backend_settings())

    files = [_make_pipeline_file() for _ in range(25)]
    session.add_all(files)
    await session.flush()
    for f in files:
        session.add(_completed_analysis_for(f.id, fine_done=1, fine_total=1))
    await session.commit()

    seen: list[str] = []
    page1 = await get_analyze_files_page(session, page=1, page_size=10, status="completed")
    assert len(page1.rows) == 10 and page1.has_next is True
    page2 = await get_analyze_files_page(session, page=2, page_size=10, status="completed")
    assert len(page2.rows) == 10 and page2.has_next is True
    page3 = await get_analyze_files_page(session, page=3, page_size=10, status="completed")
    assert len(page3.rows) == 5 and page3.has_next is False
    for pg in (page1, page2, page3):
        seen.extend(r["file_id"] for r in pg.rows)
    assert sorted(seen) == sorted(str(f.id) for f in files), "pages must partition the corpus with no overlap/gap"


@pytest.mark.asyncio
async def test_get_analyze_files_page_status_filter_lens(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zqvh.2: each status lens returns ONLY its bucket; unknown status degrades to the full membership.

    Seeds one completed + one failed + one awaiting-cloud + one off-stage file. ``completed`` /
    ``failed`` / ``awaiting_cloud`` each return exactly their file; an unknown status degrades to the
    full analyze-stage membership (all three in-stage files, off-stage excluded) -- never a 422.
    """
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _backend_settings())

    done_f = _make_pipeline_file()
    failed_f = _make_pipeline_file()
    awaiting_f = _make_pipeline_file()
    off_stage = _make_pipeline_file()
    session.add_all([done_f, failed_f, awaiting_f, off_stage])
    await session.flush()
    session.add(_completed_analysis_for(done_f.id, fine_done=1, fine_total=1))
    session.add(_failed_analysis_for(failed_f.id))
    session.add(CloudJob(id=uuid.uuid4(), file_id=awaiting_f.id, status=CloudJobStatus.AWAITING.value))
    await session.commit()

    completed = await get_analyze_files_page(session, status="completed")
    assert {r["file_id"] for r in completed.rows} == {str(done_f.id)}
    failed = await get_analyze_files_page(session, status="failed")
    assert {r["file_id"] for r in failed.rows} == {str(failed_f.id)}
    awaiting = await get_analyze_files_page(session, status="awaiting_cloud")
    assert {r["file_id"] for r in awaiting.rows} == {str(awaiting_f.id)}
    # Unknown status -> full analyze-stage membership (all three in-stage), off-stage excluded, never 422.
    unknown = await get_analyze_files_page(session, status="not-a-real-status")
    ids = {r["file_id"] for r in unknown.rows}
    assert ids == {str(done_f.id), str(failed_f.id), str(awaiting_f.id)}
    assert str(off_stage.id) not in ids
    assert unknown.status is None, "an unknown status degrades to the unfiltered listing"


def test_analyze_lanes_content_hash_is_stable_and_state_sensitive() -> None:
    """phaze-zqvh.3: the grid content hash is deterministic, changes with lane state / selection, degrade-safe.

    The hash is the server side of the idempotent-swap: identical inputs MUST yield an identical digest
    (so an unchanged tick is byte-identical and the client skips the swap), while ANY change to the lane
    snapshot OR the selected-lane highlight MUST change it (so a real update still swaps). A
    non-serializable input degrades to ``""`` (fail-safe: an empty hash never matches -> always swap).
    """
    lanes_a = [{"id": "a1", "kind": "compute", "in_flight": 2, "cap": 4, "available": True}]
    lanes_b = [{"id": "a1", "kind": "compute", "in_flight": 3, "cap": 4, "available": True}]

    # Deterministic + non-empty for identical inputs.
    h1 = analyze_lanes_content_hash(lanes_a, None)
    assert h1 and h1 == analyze_lanes_content_hash(lanes_a, None)
    # A changed datum (in_flight) changes the hash.
    assert analyze_lanes_content_hash(lanes_b, None) != h1
    # A changed selection changes the hash (the highlight is part of the rendered grid).
    assert analyze_lanes_content_hash(lanes_a, "a1") != h1
    # Empty snapshot is stable + non-crashing.
    assert analyze_lanes_content_hash([], None) == analyze_lanes_content_hash([], None)
    # Degrade-safe: an un-serializable input (a circular reference -- unhandled even by default=str)
    # collapses to "" (fail-safe -> an empty hash never matches, so the grid always swaps).
    circular: dict[str, object] = {}
    circular["self"] = circular
    assert analyze_lanes_content_hash([circular], None) == ""


@pytest.mark.asyncio
async def test_count_inflight_jobs_counts_queued_and_active() -> None:
    """count_inflight_jobs returns the scalar COUNT(*) of in-flight saq_jobs rows."""

    class _ScalarResult:
        def __init__(self, value: int) -> None:
            self._value = value

        def scalar(self) -> int:
            return self._value

    class _FakeSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> _ScalarResult:
            return _ScalarResult(7)

    assert await count_inflight_jobs(_FakeSession()) == 7  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_count_inflight_jobs_degrades_to_zero_on_db_error() -> None:
    """A missing saq_jobs table or DB hiccup degrades count_inflight_jobs to 0 (never raises, T-42-04)."""

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "saq_jobs" does not exist')

    assert await count_inflight_jobs(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_count_inflight_jobs_degrade_does_not_poison_session(session: AsyncSession) -> None:
    """The SAVEPOINT degrade leaves the outer transaction usable (no ORM-expiring rollback)."""
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    assert await count_inflight_jobs(session) == 0
    # A follow-up query on the SAME session still succeeds (the nested rollback did not poison it).
    follow_up = await get_analysis_failed_files(session)
    assert follow_up == []


# ---------------------------------------------------------------------------
# get_straggler_count (Phase 44, D-01) — active process_file running-age count, degrade-safe
# ---------------------------------------------------------------------------


def _job_blob(started_ms: int | None) -> str:
    """Serialize a minimal SAQ-style job blob (default json serializer) carrying `started`.

    The real blob is `json.dumps(job.to_dict())` (saq base queue). The straggler reader only
    reads the top-level `started` epoch-ms int, so a tiny JSON object is a faithful stand-in.
    A None started omits the field entirely (the dequeued-but-not-yet-stamped case).
    """
    d: dict[str, object] = {"key": "process_file:abc", "queue": "phaze-agent-nox"}
    if started_ms is not None:
        d["started"] = started_ms
    return json.dumps(d)


@pytest.mark.asyncio
async def test_get_straggler_count_counts_only_over_threshold() -> None:
    """Counts active process_file jobs whose Python-computed running-age exceeds the threshold.

    saq_jobs has NO started SQL column (PATTERNS.md banner) — age is read from the deserialized
    job blob's `started` (epoch ms). One job started 2h ago (> 1h threshold) counts; one started
    1s ago does not; one with no `started` (dequeued-but-unstamped) does not.
    """
    from saq.utils import now as saq_now

    now_ms = saq_now()
    threshold_sec = 3600  # 1 hour

    class _FakeResult:
        def __init__(self, rows: list[tuple[str]]) -> None:
            self._rows = rows

        def all(self) -> list[tuple[str]]:
            return self._rows

    class _FakeSession:
        def __init__(self, rows: list[tuple[str]]) -> None:
            self._rows = rows

        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
            return _FakeResult(self._rows)

    rows = [
        (_job_blob(now_ms - 2 * 3600 * 1000),),  # started 2h ago → straggler
        (_job_blob(now_ms - 1000),),  # started 1s ago → not yet old
        (_job_blob(None),),  # no started → not counted
    ]
    assert await get_straggler_count(_FakeSession(rows), threshold_sec) == 1  # type: ignore[arg-type]


def test_job_started_ms_handles_malformed_blobs() -> None:
    """A non-JSON, non-dict, or started-less blob returns None (never raises) so the straggler reader is malformed-tolerant.

    saq_jobs is an external broker table; a corrupt/legacy/unexpected blob must degrade to
    "not countable", never crash the hot 5s poll.
    """
    from phaze.services.pipeline import _job_started_ms

    assert _job_started_ms(b"not json at all {{{") is None  # invalid JSON -> None
    assert _job_started_ms(json.dumps([1, 2, 3])) is None  # JSON but not a dict -> None
    assert _job_started_ms(json.dumps({"queue": "q"})) is None  # no started -> None
    assert _job_started_ms(json.dumps({"started": 0})) is None  # non-positive started -> None
    assert _job_started_ms(json.dumps({"started": "soon"})) is None  # non-int started -> None
    assert _job_started_ms(12345) is None  # not a (str/bytes/dict) blob -> None
    assert _job_started_ms({"started": 999}) == 999  # already-a-dict blob -> read directly


@pytest.mark.asyncio
async def test_get_straggler_count_zero_when_no_active_jobs() -> None:
    """With no active process_file rows the straggler count is 0 (not an error)."""

    class _FakeResult:
        def all(self) -> list[tuple[str]]:
            return []

    class _FakeSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
            return _FakeResult()

    assert await get_straggler_count(_FakeSession(), 3600) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_straggler_count_degrades_on_db_error() -> None:
    """get_straggler_count returns 0 and never raises when the saq_jobs read fails (T-44-04).

    A missing saq_jobs table or a DB hiccup must degrade to 0 so the hot 5s /pipeline/stats poll
    keeps serving instead of 500ing. The read runs inside a SAVEPOINT (begin_nested); the
    exception propagates out of the nested scope and is caught by the degrade except.
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "saq_jobs" does not exist')

    assert await get_straggler_count(_ExplodingSession(), 3600) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_straggler_count_degrade_does_not_poison_session(session: AsyncSession) -> None:
    """The SAVEPOINT degrade leaves the outer transaction usable (mirrors the busy-count guards).

    DROP saq_jobs inside this test's uncommitted transaction to deterministically force the
    absent-table degrade — the only branch that exercises the SAVEPOINT rollback recovery. A
    follow-up query on the SAME session must still succeed, proving the dashboard's later ORM
    lazy-loads are not poisoned (the bug a plain session.rollback() would cause).
    """
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    assert await get_straggler_count(session, 3600) == 0
    # The outer transaction is intact after the SAVEPOINT rollback: a normal query still runs.
    follow_up = await get_stage_progress(session)
    assert follow_up["discovery"]["done"] == 0


# ---------------------------------------------------------------------------
# Scanned / deduped / unique reconciliation (quick 260622-i0w) — turns the
# Discovery-count vs agent-scan-total gap into a self-explaining reconciliation.
#   scanned   = SUM over agents of (each agent's LATEST completed batch).total_files
#   deduped   = max(0, scanned - discovery_done)  [global: discovery_done = COUNT(all files)]
#   per-agent = max(0, agent_latest_total_files - agent file-row count)
# A None scanned (no completed batches / DB error) hides the whole line.
# ---------------------------------------------------------------------------


def _completed_batch(agent_id: str, total_files: int, *, status: str = ScanStatus.COMPLETED.value, created_at: object = None) -> ScanBatch:
    """Build a ScanBatch seed; set ``created_at`` explicitly when latest-per-agent ordering matters."""
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id=agent_id,
        scan_path="/music",
        status=status,
        total_files=total_files,
        processed_files=total_files,
    )
    if created_at is not None:
        batch.created_at = created_at  # type: ignore[assignment]
    return batch


def _recon_file(agent_id: str, i: int) -> FileRecord:
    """Build a unique FileRecord owned by ``agent_id`` (the reconciliation groups by agent_id)."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{agent_id}/{i}-{uid.hex}.mp3",
        original_filename=f"{i}.mp3",
        current_path=f"/music/{agent_id}/{i}-{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        agent_id=agent_id,
    )


def test_deduped_count_none_passthrough() -> None:
    """A None ``scanned`` passes through as None so the UI hides the reconciliation line."""
    assert deduped_count(None, 5) is None


def test_deduped_count_basic_arithmetic() -> None:
    """deduped = scanned - unique when scanned > unique."""
    assert deduped_count(10, 4) == 6


def test_deduped_count_clamps_negative_to_zero() -> None:
    """deduped never goes negative: more files than scanned clamps to 0."""
    assert deduped_count(3, 8) == 0


@pytest.mark.asyncio
async def test_get_scanned_total_single_completed_batch(session: AsyncSession) -> None:
    """One agent with one completed batch (total_files=100) → scanned 100."""
    await seed_active_agent(session, "nox")
    session.add(_completed_batch("nox", 100))
    await session.commit()
    assert await get_scanned_total(session) == 100


@pytest.mark.asyncio
async def test_get_scanned_total_rescan_counts_latest_only(session: AsyncSession) -> None:
    """A second completed batch (a re-scan) counts the LATEST only — never doubles the total."""
    from datetime import datetime

    await seed_active_agent(session, "nox")
    # Naive datetimes: the test-DB create_all schema makes created_at TIMESTAMP WITHOUT TIME ZONE.
    earlier = _completed_batch("nox", 100, created_at=datetime(2026, 1, 1, 10, 0, 0))
    later = _completed_batch("nox", 120, created_at=datetime(2026, 1, 1, 11, 0, 0))
    session.add_all([earlier, later])
    await session.commit()
    # Latest (120), not the sum (220) and not the earlier (100).
    assert await get_scanned_total(session) == 120


@pytest.mark.asyncio
async def test_get_scanned_total_sums_across_agents(session: AsyncSession) -> None:
    """scanned sums each agent's latest completed batch: 100 (nox) + 50 (lux) → 150."""
    await seed_active_agent(session, "nox")
    await seed_active_agent(session, "lux")
    session.add_all([_completed_batch("nox", 100), _completed_batch("lux", 50)])
    await session.commit()
    assert await get_scanned_total(session) == 150


@pytest.mark.asyncio
async def test_get_scanned_total_ignores_non_completed(session: AsyncSession) -> None:
    """RUNNING / FAILED / LIVE batches never contribute to scanned."""
    await seed_active_agent(session, "nox")
    session.add_all(
        [
            _completed_batch("nox", 100),
            _completed_batch("nox", 999, status=ScanStatus.RUNNING.value),
            _completed_batch("nox", 999, status=ScanStatus.FAILED.value),
        ]
    )
    await session.commit()
    assert await get_scanned_total(session) == 100


@pytest.mark.asyncio
async def test_get_scanned_total_empty_db_returns_none(session: AsyncSession) -> None:
    """No completed batches → None (the 'hide' sentinel, distinct from a real 0)."""
    assert await get_scanned_total(session) is None


@pytest.mark.asyncio
async def test_get_scanned_total_degrades_to_none_on_db_error() -> None:
    """A forced read error degrades scanned to None (hidden state), never raising into the 5s poll.

    The read runs inside a SAVEPOINT (``begin_nested``); the exception propagates out of the nested
    scope and is caught by the degrade ``except`` (CR-01 -- the caller's shared session is never
    touched with a full ``session.rollback()``).
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("scan_batches table unavailable")

    assert await get_scanned_total(_ExplodingSession()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_scanned_total_degrades_when_begin_nested_itself_raises() -> None:
    """Even if the session is so broken ``begin_nested()`` itself raises, scanned still degrades to None.

    Exercises the last-ditch branch where opening the SAVEPOINT fails synchronously (before any
    query runs). The function must still swallow everything and return the hidden-state sentinel
    rather than propagating into the 5s poll.
    """

    class _DoublyExplodingSession:
        def begin_nested(self) -> object:
            raise RuntimeError("connection already closed")

    assert await get_scanned_total(_DoublyExplodingSession()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_scanned_total_degrade_preserves_caller_loaded_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-01: the degrade must NOT expire ORM rows the caller already loaded on this same session.

    ``build_dashboard_context`` loads ``agents`` on the request session BEFORE calling
    :func:`get_scanned_total` (transitively via :func:`get_global_reconciliation`). A plain
    ``session.rollback()`` in the degrade branch would expire that already-loaded ``Agent`` row,
    500-ing the template render on the next lazy load (MissingGreenlet from a sync context).

    Distinguishing signal (fixture never commits, so ``inspect().expired`` cannot tell a SAVEPOINT
    rollback apart from a plain one -- a plain rollback expunges the pending flush to *transient*,
    not *expired*): flush an Agent row, force ONLY the scanned-total SELECT to fail, then assert
    ``session.get`` still finds the agent afterwards -- proving the outer transaction survived.
    """
    from unittest.mock import AsyncMock

    agent = Agent(id="cr01-scanned-total-agent", name="Cr01ScanBox", scan_roots=[], last_seen_at=datetime.now(UTC), kind="fileserver")
    session.add(agent)
    await session.flush()

    real_execute = session.execute
    monkeypatch.setattr(session, "execute", AsyncMock(side_effect=RuntimeError("boom")))
    result = await get_scanned_total(session)
    monkeypatch.setattr(session, "execute", real_execute)  # restore for the assertion query

    assert result is None
    assert await session.get(Agent, "cr01-scanned-total-agent") is not None


@pytest.mark.asyncio
async def test_get_global_reconciliation_happy_path(session: AsyncSession) -> None:
    """scanned 11428 with 11106 discovered files → {'scanned': 11428, 'deduped': 322}."""
    await seed_active_agent(session, "nox")
    session.add(_completed_batch("nox", 11428))
    # 5 files stands in for the discovery_done COUNT; assert the arithmetic, not a 11106-row seed.
    session.add_all([_recon_file("nox", i) for i in range(5)])
    await session.commit()
    recon = await get_global_reconciliation(session)
    assert recon == {"scanned": 11428, "deduped": 11423}


@pytest.mark.asyncio
async def test_get_global_reconciliation_hidden_when_scanned_unavailable(session: AsyncSession) -> None:
    """When get_scanned_total degrades to None the whole reconciliation is the hidden state."""
    recon = await get_global_reconciliation(session)  # empty DB → no completed batches → None
    assert recon == {"scanned": None, "deduped": None}


@pytest.mark.asyncio
async def test_get_global_reconciliation_clamps_when_discovery_ge_scanned(session: AsyncSession) -> None:
    """deduped clamps to 0 when discovery_done ≥ scanned (never negative)."""
    await seed_active_agent(session, "nox")
    session.add(_completed_batch("nox", 2))
    session.add_all([_recon_file("nox", i) for i in range(5)])  # 5 files > scanned 2
    await session.commit()
    recon = await get_global_reconciliation(session)
    assert recon == {"scanned": 2, "deduped": 0}


@pytest.mark.asyncio
async def test_get_agent_reconciliations_per_agent_dedup(session: AsyncSession) -> None:
    """Per-agent: A latest 100 with 90 rows → deduped 10; B latest 50 with 50 rows → deduped 0."""
    await seed_active_agent(session, "nox")
    await seed_active_agent(session, "lux")
    session.add_all([_completed_batch("nox", 100), _completed_batch("lux", 50)])
    session.add_all([_recon_file("nox", i) for i in range(90)])
    session.add_all([_recon_file("lux", i) for i in range(50)])
    await session.commit()

    recon = await get_agent_reconciliations(session)
    assert recon["nox"] == {"scanned": 100, "unique": 90, "deduped": 10}
    assert recon["lux"] == {"scanned": 50, "unique": 50, "deduped": 0}


@pytest.mark.asyncio
async def test_get_agent_reconciliations_rescan_counts_latest_only(session: AsyncSession) -> None:
    """A second completed batch for one agent counts the latest total_files only."""
    from datetime import datetime

    await seed_active_agent(session, "nox")
    # Naive datetimes: the test-DB create_all schema makes created_at TIMESTAMP WITHOUT TIME ZONE.
    earlier = _completed_batch("nox", 100, created_at=datetime(2026, 1, 1, 10, 0, 0))
    later = _completed_batch("nox", 120, created_at=datetime(2026, 1, 1, 11, 0, 0))
    session.add_all([earlier, later])
    session.add_all([_recon_file("nox", i) for i in range(90)])
    await session.commit()

    recon = await get_agent_reconciliations(session)
    assert recon["nox"] == {"scanned": 120, "unique": 90, "deduped": 30}


@pytest.mark.asyncio
async def test_get_agent_reconciliations_tiebreaks_tied_created_at_by_id_desc(session: AsyncSession) -> None:
    """rn==1 for a ``created_at`` tie must be the MAX id (``ScanBatch.id.desc()`` tiebreak), not
    arbitrary heap/plan order.

    Mirrors the phaze-c6j5 regression-guard technique (``test_get_agent_recent_scans_tiebreaker_
    orders_tied_created_at_by_id_desc`` above): seeds several completed batches for ONE agent
    sharing an EXPLICIT ``created_at`` with ids assigned in a SCRAMBLED order relative to insertion,
    and pins each batch's ``total_files`` to a value derived from its id index so the row actually
    selected as rn==1 is identifiable precisely. Only the ``ScanBatch.id.desc()`` tiebreaker
    appended to the window's ``order_by`` (matching the primary ``created_at.desc()``) makes the
    "agent's most recent completed batch" pick deterministic on a tie; without it the pick tracks
    Postgres heap/plan order, not id order -- which the scrambled insertion order below defeats.
    """
    await seed_active_agent(session, "nox")
    tied_at = datetime(2026, 7, 20, 12, 0, 0)  # naive: test schema's created_at is TIMESTAMP WITHOUT TZ
    # 5 fixed, distinct ids -- inserted in a SCRAMBLED order (not ascending, not descending).
    ids = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{i:02d}") for i in range(5)]
    scrambled_indices = [2, 0, 4, 1, 3]
    for i in scrambled_indices:
        batch = ScanBatch(
            id=ids[i],
            agent_id="nox",
            scan_path="/music",
            status=ScanStatus.COMPLETED.value,
            total_files=(i + 1) * 10,
            processed_files=(i + 1) * 10,
        )
        batch.created_at = tied_at  # type: ignore[assignment]
        session.add(batch)
    await session.commit()

    recon = await get_agent_reconciliations(session)

    # id DESC as the tiebreak -> ids[4] (the LARGEST id) must win -> total_files=(4+1)*10=50.
    assert recon["nox"]["scanned"] == 50


@pytest.mark.asyncio
async def test_get_agent_reconciliations_degrades_to_empty_on_db_error() -> None:
    """A forced read error degrades to an empty map (no annotations), never raising.

    The reads run inside a SAVEPOINT (``begin_nested``); the exception propagates out of the
    nested scope and is caught by the degrade ``except`` (CR-01 -- the caller's shared session is
    never touched with a full ``session.rollback()``).
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("scan_batches table unavailable")

    assert await get_agent_reconciliations(_ExplodingSession()) == {}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_agent_reconciliations_degrades_when_begin_nested_itself_raises() -> None:
    """Even if the session is so broken ``begin_nested()`` itself raises, the per-agent map still
    degrades to ``{}`` rather than propagating into the 5s dashboard poll."""

    class _DoublyExplodingSession:
        def begin_nested(self) -> object:
            raise RuntimeError("connection already closed")

    assert await get_agent_reconciliations(_DoublyExplodingSession()) == {}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_agent_reconciliations_degrade_preserves_caller_loaded_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-01: the degrade must NOT expire ORM rows the caller already loaded on this same session.

    ``build_recent_scans`` (``routers.pipeline_scans``) loads ScanBatch ORM rows on this SAME
    session BEFORE calling ``get_agent_reconciliations``. A plain ``session.rollback()`` in the
    degrade branch would expire those already-loaded rows, 500-ing the render on the next lazy load
    (MissingGreenlet from a sync context).

    Distinguishing signal (fixture never commits, so ``inspect().expired`` cannot tell a SAVEPOINT
    rollback apart from a plain one -- a plain rollback expunges the pending flush to *transient*,
    not *expired*): flush a ScanBatch row, force ONLY the reconciliation reads to fail, then assert
    ``session.get`` still finds the scan batch afterwards -- proving the outer transaction survived.
    """
    from unittest.mock import AsyncMock

    session.add(Agent(id="cr01-recon-agent", name="Cr01ReconBox", scan_roots=[], kind="fileserver"))
    await session.flush()
    batch = ScanBatch(id=uuid.uuid4(), agent_id="cr01-recon-agent", scan_path="/data/music", status=ScanStatus.COMPLETED.value, total_files=5)
    session.add(batch)
    await session.flush()

    real_execute = session.execute
    monkeypatch.setattr(session, "execute", AsyncMock(side_effect=RuntimeError("boom")))
    recon = await get_agent_reconciliations(session)
    monkeypatch.setattr(session, "execute", real_execute)  # restore for the assertion query

    assert recon == {}
    assert await session.get(ScanBatch, batch.id) is not None


# ---------------------------------------------------------------------------
# Phase 49 duration-routing helpers (D-05, D-09/D-10): duration join,
# awaiting-cloud count, and backfill candidates (ANALYSIS_FAILED + duration>=N)
# ---------------------------------------------------------------------------


def _file(i: int) -> FileRecord:
    """Build a FileRecord seed in the given state (unique hash/path per ``i``)."""
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=f"d{i:063d}"[:64],
        original_path=f"/music/dur{i}.mp3",
        original_filename=f"dur{i}.mp3",
        current_path=f"/music/dur{i}.mp3",
        file_type="mp3",
        file_size=1000,
    )


def _metadata_for(file_id: uuid.UUID, duration: float | None) -> FileMetadata:
    """Build a FileMetadata row for ``file_id`` carrying ``duration`` (or None)."""
    return FileMetadata(id=uuid.uuid4(), file_id=file_id, duration=duration)


async def _seed_process_file_ledger(session: AsyncSession, *files: FileRecord) -> None:
    """Seed a ``process_file:<id>`` scheduling-ledger row per file.

    Phase 55 (L4) scopes the backfill candidate query to *previously-scheduled* work: a file
    is a candidate only if such a ledger row exists (a SAQ timeout abandons the job without
    clearing the row, so it persists into ANALYSIS_FAILED). These tests assert the state +
    duration filter, so every failed file is ledgered — exclusions come from state/duration,
    not a missing ledger row.
    """
    from phaze.services.scheduling_ledger import insert_ledger_if_absent

    for f in files:
        await insert_ledger_if_absent(
            session,
            key=f"process_file:{f.id}",
            function="process_file",
            kwargs={},
            timeout=7200,
            retries=2,
        )


@pytest.mark.asyncio
async def test_get_discovered_files_with_duration_joins_duration(session: AsyncSession) -> None:
    """Each DISCOVERED file is paired with its joined FileMetadata.duration."""
    f = _file(0)
    session.add(f)
    await session.flush()
    session.add(_metadata_for(f.id, 6000.0))
    await session.commit()

    rows = await get_discovered_files_with_duration(session)

    assert len(rows) == 1
    record, duration = rows[0]
    assert record.id == f.id
    assert duration == 6000.0


@pytest.mark.asyncio
async def test_get_discovered_files_with_duration_outerjoin_null(session: AsyncSession) -> None:
    """A DISCOVERED file with no metadata row still appears, with duration None (LEFT JOIN)."""
    f = _file(1)
    session.add(f)
    await session.commit()

    rows = await get_discovered_files_with_duration(session)

    assert len(rows) == 1
    record, duration = rows[0]
    assert record.id == f.id
    assert duration is None


@pytest.mark.asyncio
async def test_get_awaiting_cloud_count_happy_path(session: AsyncSession) -> None:
    """Counts exactly the genuinely-parked awaiting cloud_job rows; other states are excluded (Phase 83, D-15)."""
    a, b, discovered = _file(4), _file(5), _file(6)
    session.add_all([a, b, discovered])
    await session.commit()
    # Phase 83: the count derives from cloud_job(status='awaiting'), not FileRecord.state -- the two held
    # files carry their sidecar rows; the DISCOVERED file has none (and would not be a drain candidate).
    session.add_all(
        [
            CloudJob(id=uuid.uuid4(), file_id=a.id, status=CloudJobStatus.AWAITING.value),
            CloudJob(id=uuid.uuid4(), file_id=b.id, status=CloudJobStatus.AWAITING.value),
        ]
    )
    await session.commit()

    assert await get_awaiting_cloud_count(session) == 2


@pytest.mark.asyncio
async def test_get_awaiting_cloud_count_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades the count to 0 (poll-safe via _safe_count), never raising."""

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("files table unavailable")

    assert await get_awaiting_cloud_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


async def _seed_cloud_job(session: AsyncSession, file_index: int, status: CloudJobStatus) -> None:
    """Seed a ``(FileRecord, cloud_job)`` pair; the cloud_job carries ``status`` (Phase 90 D-12)."""
    f = _file(file_index)
    session.add(f)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, status=status.value))


@pytest.mark.asyncio
async def test_get_pushing_count_happy_path(session: AsyncSession) -> None:
    """DERIVED "pushing" count (Phase 90 D-12): cloud_job status IN (uploading, submitted).

    Sources from the ``cloud_job`` sidecar, NOT ``files.state == PUSHING``. An ``uploaded`` (pushed)
    and an ``awaiting`` cloud_job are excluded, proving the status membership.
    """
    await _seed_cloud_job(session, 40, CloudJobStatus.UPLOADING)
    await _seed_cloud_job(session, 41, CloudJobStatus.SUBMITTED)
    await _seed_cloud_job(session, 42, CloudJobStatus.UPLOADED)
    await _seed_cloud_job(session, 43, CloudJobStatus.AWAITING)
    await session.commit()

    assert await get_pushing_count(session) == 2


@pytest.mark.asyncio
async def test_get_pushed_count_happy_path(session: AsyncSession) -> None:
    """DERIVED "pushed / analyzing" count (Phase 90 D-12): cloud_job status IN (uploaded, running).

    Sources from the ``cloud_job`` sidecar, NOT ``files.state == PUSHED``. An ``uploading`` (pushing)
    cloud_job is excluded, proving the status membership.
    """
    await _seed_cloud_job(session, 44, CloudJobStatus.UPLOADED)
    await _seed_cloud_job(session, 45, CloudJobStatus.RUNNING)
    await _seed_cloud_job(session, 46, CloudJobStatus.UPLOADED)
    await _seed_cloud_job(session, 47, CloudJobStatus.UPLOADING)
    await session.commit()

    assert await get_pushed_count(session) == 3


@pytest.mark.asyncio
async def test_get_pushing_count_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades the PUSHING count to 0 (poll-safe via _safe_count)."""

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("files table unavailable")

    assert await get_pushing_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_pushed_count_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades the PUSHED count to 0 (poll-safe via _safe_count)."""

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("files table unavailable")

    assert await get_pushed_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_backfill_candidates_filters_by_state_and_duration(session: AsyncSession) -> None:
    """Only ANALYSIS_FAILED files whose joined duration >= threshold are candidates (D-09/D-10).

    A long ANALYSIS_FAILED file qualifies; a short one, a null-duration one, and a long file
    in another state are all EXCLUDED — proving the explicit duration filter that closes the
    over-enqueue class (NOT a bare ANALYSIS_FAILED count).
    """
    threshold = 5400

    long_failed = _file(7)
    short_failed = _file(8)
    null_failed = _file(9)
    long_other = _file(10)
    session.add_all([long_failed, short_failed, null_failed, long_other])
    await session.flush()
    session.add_all(
        [
            _metadata_for(long_failed.id, 6000.0),
            _metadata_for(short_failed.id, 120.0),
            _metadata_for(null_failed.id, None),
            _metadata_for(long_other.id, 6000.0),
        ]
    )
    # Phase 90 (PR-A): the candidate predicate DERIVES the failure from the ``analysis.failed_at`` marker
    # (``failed_clause(ANALYZE)``), not ``files.state`` -- so the three failed files carry the marker; the
    # long DISCOVERED file (negative control) has none and is excluded by the failure clause, not by state.
    session.add_all([_failed_analysis_for(long_failed.id), _failed_analysis_for(short_failed.id), _failed_analysis_for(null_failed.id)])
    await _seed_process_file_ledger(session, long_failed, short_failed, null_failed)
    await session.commit()

    assert await count_backfill_candidates(session, threshold) == 1

    rows = await get_backfill_candidates(session, threshold)
    assert len(rows) == 1
    record, duration = rows[0]
    assert record.id == long_failed.id
    assert duration == 6000.0


@pytest.mark.asyncio
async def test_backfill_candidates_boundary_is_inclusive(session: AsyncSession) -> None:
    """A file exactly at the threshold qualifies (>=, not >)."""
    threshold = 5400
    at_threshold = _file(11)
    session.add(at_threshold)
    await session.flush()
    session.add(_metadata_for(at_threshold.id, float(threshold)))
    session.add(_failed_analysis_for(at_threshold.id))  # Phase 90 (PR-A): DERIVED failure marker
    await _seed_process_file_ledger(session, at_threshold)
    await session.commit()

    assert await count_backfill_candidates(session, threshold) == 1


# ---------------------------------------------------------------------------
# get_agent_recent_scans (phaze-c6j5): the LIMIT boundary must be deterministic
# on a created_at tie, not arbitrary heap order.
# ---------------------------------------------------------------------------


def _scan_batch(agent_id: str, *, batch_id: uuid.UUID, created_at: object = None) -> ScanBatch:
    """Build a ScanBatch with an INJECTABLE id, for tiebreaker tests that need a fixed pk."""
    batch = ScanBatch(
        id=batch_id,
        agent_id=agent_id,
        scan_path="/music",
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
    )
    if created_at is not None:
        batch.created_at = created_at  # type: ignore[assignment]
    return batch


@pytest.mark.asyncio
async def test_get_agent_recent_scans_tiebreaker_orders_tied_created_at_by_id_desc(session: AsyncSession) -> None:
    """Rows with an IDENTICAL created_at come back ordered by ScanBatch.id DESC, not heap order.

    Seeds ``_AGENT_RECENT_SCANS_N`` + 1 (11) rows sharing ONE explicit ``created_at`` -- so
    ``created_at`` alone leaves every row tied -- with ids assigned in a SCRAMBLED order
    relative to insertion. Only the ``ScanBatch.id`` tiebreaker on
    ``services.pipeline.get_agent_recent_scans`` makes the LIMIT-10 boundary total and
    deterministic: the 10 returned rows must be exactly the 10 largest ids (id DESC to match
    the primary ``created_at DESC``), and their order must be strictly descending by id.

    Regression guard for phaze-c6j5: reverting the ``, ScanBatch.id.desc()`` suffix makes
    both the boundary membership and the in-page order depend on Postgres heap layout
    (verified: this assertion fails without the tiebreaker -- heap/insertion order does not
    match descending-id order for the scrambled ids below).
    """
    from phaze.services.pipeline import _AGENT_RECENT_SCANS_N

    await seed_active_agent(session, "nox")

    tied_at = datetime(2026, 7, 20, 12, 0, 0)  # naive: test schema's created_at is TIMESTAMP WITHOUT TZ
    # 11 fixed, distinct ids -- deliberately NOT inserted in id order.
    seed_count = _AGENT_RECENT_SCANS_N + 1
    ids = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{i:02d}") for i in range(seed_count)]
    scrambled = ids[::2] + ids[1::2]  # e.g. [0,2,4,6,8,10,1,3,5,7,9]

    for bid in scrambled:
        session.add(_scan_batch("nox", batch_id=bid, created_at=tied_at))
    await session.commit()

    rows = await get_agent_recent_scans(session, "nox")
    actual_ids = [row.id for row in rows]

    # LIMIT is _AGENT_RECENT_SCANS_N (10 of the 11 seeded); the boundary + in-page order come
    # entirely from the id tiebreaker: the 10 LARGEST ids, strictly descending.
    assert len(actual_ids) == _AGENT_RECENT_SCANS_N
    assert actual_ids == sorted(ids, reverse=True)[:_AGENT_RECENT_SCANS_N]


@pytest.mark.asyncio
async def test_get_agent_recent_scans_orders_by_created_at_then_id(session: AsyncSession) -> None:
    """Distinct created_at values dominate; the id tiebreaker only breaks exact ties.

    Seeds explicit, strictly-increasing ``created_at`` values with ids in the OPPOSITE
    order, and asserts the result follows ``created_at`` DESC (newest first) -- confirming
    the primary sort key still wins when timestamps differ.
    """
    await seed_active_agent(session, "nox")
    base = datetime(2026, 7, 20, 9, 0, 0)
    # created_at increases with i; id decreases with i -> the two keys disagree.
    ids = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{(90 - i * 10):02d}") for i in range(5)]
    for i, bid in enumerate(ids):
        session.add(_scan_batch("nox", batch_id=bid, created_at=base + timedelta(seconds=i)))
    await session.commit()

    rows = await get_agent_recent_scans(session, "nox")

    # Newest created_at first: i=4 (last inserted, largest timestamp) down to i=0.
    assert [row.id for row in rows] == list(reversed(ids))
