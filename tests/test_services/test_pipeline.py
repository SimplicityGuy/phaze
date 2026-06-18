"""Tests for the pipeline orchestration service."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import text

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist
from phaze.services.pipeline import (
    count_active_agents,
    count_inflight_jobs,
    get_analysis_failed_count,
    get_analysis_failed_files,
    get_files_by_state,
    get_fingerprint_pending_files,
    get_match_busy_count,
    get_match_pending_tracklists,
    get_metadata_pending_files,
    get_pipeline_stats,
    get_proposal_pending_batches,
    get_queue_activity,
    get_scan_busy_count,
    get_scrape_busy_count,
    get_scrape_pending_tracklists,
    get_search_busy_count,
    get_stage_busy_counts,
    get_straggler_count,
    get_untracked_files,
)
from tests._queue_fakes import FakeQueue, FakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_get_pipeline_stats_empty(session: AsyncSession):
    """Empty database returns zero counts for all stages."""
    stats = await get_pipeline_stats(session)
    assert stats["discovered"] == 0
    assert stats["metadata_extracted"] == 0
    assert stats["analyzed"] == 0
    assert stats["proposal_generated"] == 0
    assert stats["approved"] == 0
    assert stats["executed"] == 0


@pytest.mark.asyncio
async def test_get_pipeline_stats_counts(session: AsyncSession):
    """Stats reflect actual file counts per state."""
    for i in range(3):
        f = FileRecord(
            id=uuid.uuid4(),
            sha256_hash=f"abc{i:064d}"[:64],
            original_path=f"/music/test{i}.mp3",
            original_filename=f"test{i}.mp3",
            current_path=f"/music/test{i}.mp3",
            file_type="mp3",
            file_size=1000,
            state=FileState.DISCOVERED,
        )
        session.add(f)
    session.add(
        FileRecord(
            id=uuid.uuid4(),
            sha256_hash="xyz0" + "0" * 60,
            original_path="/music/done.mp3",
            original_filename="done.mp3",
            current_path="/music/done.mp3",
            file_type="mp3",
            file_size=1000,
            state=FileState.ANALYZED,
        )
    )
    await session.commit()
    stats = await get_pipeline_stats(session)
    assert stats["discovered"] == 3
    assert stats["analyzed"] == 1


@pytest.mark.asyncio
async def test_get_pipeline_stats_includes_metadata_extracted(session: AsyncSession):
    """Stats include METADATA_EXTRACTED state count."""
    f = FileRecord(
        id=uuid.uuid4(),
        sha256_hash="m" * 64,
        original_path="/music/tagged.mp3",
        original_filename="tagged.mp3",
        current_path="/music/tagged.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.METADATA_EXTRACTED,
    )
    session.add(f)
    await session.commit()
    stats = await get_pipeline_stats(session)
    assert stats["metadata_extracted"] == 1


@pytest.mark.asyncio
async def test_get_files_by_state(session: AsyncSession):
    """get_files_by_state returns only files in the requested state."""
    f1 = FileRecord(
        id=uuid.uuid4(),
        sha256_hash="a" * 64,
        original_path="/music/a.mp3",
        original_filename="a.mp3",
        current_path="/music/a.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.DISCOVERED,
    )
    f2 = FileRecord(
        id=uuid.uuid4(),
        sha256_hash="b" * 64,
        original_path="/music/b.mp3",
        original_filename="b.mp3",
        current_path="/music/b.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.ANALYZED,
    )
    session.add_all([f1, f2])
    await session.commit()
    discovered = await get_files_by_state(session, FileState.DISCOVERED)
    assert len(discovered) == 1
    assert discovered[0].id == f1.id


# ---------------------------------------------------------------------------
# ANALYSIS_FAILED bucket (Phase 44, D-02) — count/list read from indexed files.state
# ---------------------------------------------------------------------------


def _failed_file(i: int, state: FileState = FileState.ANALYSIS_FAILED) -> FileRecord:
    """Build a FileRecord seed in the given state (default ANALYSIS_FAILED)."""
    return FileRecord(
        id=uuid.uuid4(),
        sha256_hash=f"f{i:063d}"[:64],
        original_path=f"/music/failed{i}.mp3",
        original_filename=f"failed{i}.mp3",
        current_path=f"/music/failed{i}.mp3",
        file_type="mp3",
        file_size=1000,
        state=state,
    )


@pytest.mark.asyncio
async def test_get_analysis_failed_count_happy_path(session: AsyncSession) -> None:
    """Counts exactly the files in ANALYSIS_FAILED; other states are excluded."""
    session.add_all([_failed_file(0), _failed_file(1), _failed_file(2, FileState.ANALYZED)])
    await session.commit()
    assert await get_analysis_failed_count(session) == 2


@pytest.mark.asyncio
async def test_get_analysis_failed_files_returns_failed_rows(session: AsyncSession) -> None:
    """Returns the FileRecords in ANALYSIS_FAILED and only those."""
    a = _failed_file(0)
    b = _failed_file(1)
    session.add_all([a, b, _failed_file(2, FileState.DISCOVERED)])
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
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("files table unavailable")

        async def rollback(self) -> None:
            return None

    assert await get_analysis_failed_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_analysis_failed_not_in_pipeline_stages() -> None:
    """ANALYSIS_FAILED is its OWN bucket — never added to the linear PIPELINE_STAGES (D-02)."""
    from phaze.services.pipeline import PIPELINE_STAGES

    assert FileState.ANALYSIS_FAILED not in PIPELINE_STAGES


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
    follow_up = await get_pipeline_stats(session)
    assert follow_up["discovered"] == 0


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
    follow_up = await get_pipeline_stats(session)
    assert follow_up["discovered"] == 0


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
    follow_up = await get_pipeline_stats(session)
    assert follow_up["discovered"] == 0


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
    follow_up = await get_pipeline_stats(session)
    assert follow_up["discovered"] == 0


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
    follow_up = await get_pipeline_stats(session)
    assert follow_up["discovered"] == 0


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


def _make_pipeline_file(*, file_type: str = "mp3", state: str = FileState.DISCOVERED) -> FileRecord:
    """Build a fully-populated FileRecord row for the pending-set helper tests."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
        state=state,
    )


@pytest.mark.asyncio
async def test_get_metadata_pending_files_returns_only_music_video(session: AsyncSession) -> None:
    """Metadata pending = every music/video file (any state); a non-music file_type is excluded."""
    music = _make_pipeline_file(file_type="mp3", state=FileState.DISCOVERED)
    other = _make_pipeline_file(file_type="txt", state=FileState.DISCOVERED)
    session.add_all([music, other])
    await session.flush()

    result = await get_metadata_pending_files(session)
    ids = {f.id for f in result}
    assert music.id in ids
    assert other.id not in ids


@pytest.mark.asyncio
async def test_get_fingerprint_pending_files_unions_metadata_extracted_and_failed_retry(session: AsyncSession) -> None:
    """Fingerprint pending = METADATA_EXTRACTED union failed-retry (state != FINGERPRINTED), deduped by id.

    A METADATA_EXTRACTED file is in; a failed-fingerprint file still not FINGERPRINTED is in (retry);
    a FINGERPRINTED file with a failed result is excluded; a plain DISCOVERED file is excluded.
    """
    ready = _make_pipeline_file(state=FileState.METADATA_EXTRACTED)
    failed_retry = _make_pipeline_file(state=FileState.ANALYZED)
    already_done = _make_pipeline_file(state=FileState.FINGERPRINTED)
    discovered = _make_pipeline_file(state=FileState.DISCOVERED)
    session.add_all([ready, failed_retry, already_done, discovered])
    await session.flush()
    session.add_all(
        [
            FingerprintResult(id=uuid.uuid4(), file_id=failed_retry.id, engine="audfprint", status="failed"),
            FingerprintResult(id=uuid.uuid4(), file_id=already_done.id, engine="audfprint", status="failed"),
        ]
    )
    await session.flush()

    result = await get_fingerprint_pending_files(session)
    ids = [f.id for f in result]
    assert ready.id in ids
    assert failed_retry.id in ids
    assert already_done.id not in ids
    assert discovered.id not in ids


@pytest.mark.asyncio
async def test_get_fingerprint_pending_files_dedups_metadata_extracted_with_failed_result(session: AsyncSession) -> None:
    """A METADATA_EXTRACTED file that ALSO has a failed fingerprint result appears exactly once."""
    dual = _make_pipeline_file(state=FileState.METADATA_EXTRACTED)
    session.add(dual)
    await session.flush()
    session.add(FingerprintResult(id=uuid.uuid4(), file_id=dual.id, engine="audfprint", status="failed"))
    await session.flush()

    result = await get_fingerprint_pending_files(session)
    ids = [f.id for f in result]
    assert ids.count(dual.id) == 1


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
    files = [_make_pipeline_file(state=FileState.ANALYZED) for _ in range(3)]
    session.add_all(files)
    await session.flush()
    related: list[object] = []
    for f in files:
        related.append(FileMetadata(file_id=f.id, artist="A", title="T"))
        related.append(AnalysisResult(file_id=f.id, bpm=120.0))
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
    only_metadata = _make_pipeline_file(state=FileState.METADATA_EXTRACTED)
    session.add(only_metadata)
    await session.flush()
    session.add(FileMetadata(file_id=only_metadata.id, artist="A", title="T"))
    await session.flush()

    batches = await get_proposal_pending_batches(session, 10)
    flat = [fid for batch in batches for fid in batch]
    assert str(only_metadata.id) not in flat


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
    follow_up = await get_files_by_state(session, FileState.DISCOVERED)
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
    follow_up = await get_pipeline_stats(session)
    assert follow_up["discovered"] == 0
