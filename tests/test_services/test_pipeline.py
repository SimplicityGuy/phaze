"""Tests for the pipeline orchestration service."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import text

from phaze.models.file import FileRecord, FileState
from phaze.services.pipeline import get_files_by_state, get_pipeline_stats, get_queue_activity, get_search_busy_count, get_stage_busy_counts
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
