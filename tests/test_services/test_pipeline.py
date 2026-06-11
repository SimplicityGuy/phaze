"""Tests for the pipeline orchestration service."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.services.pipeline import get_files_by_state, get_pipeline_stats, get_queue_activity
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
