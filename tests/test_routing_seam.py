"""Phase 50 routing-seam reshape tests (CLOUDPIPE-01).

The routing seam must hold EVERY long file in ``AWAITING_CLOUD`` for the bounded
``stage_cloud_window`` staging cron to pick up -- it must NEVER enqueue directly to the
compute agent, bypassing the ≤N in-flight window (T-50-bypass). Plan 50-06 replaced the
Phase-49 ``compute_agent is not None -> direct compute enqueue`` branch with an
unconditional AWAITING_CLOUD hold, so the window is enforceable in exactly one place.

These tests drive the FastAPI-free helper ``_route_discovered_by_duration`` directly with a
real DB session + a ``FakeTaskRouter`` capture double, so they assert the seam's state
transition and the ABSENCE of any compute enqueue without standing up the whole app.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.routers.pipeline import _background_tasks, _route_discovered_by_duration
from tests._queue_fakes import FakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_THRESHOLD = 5400
_LONG = 6000.0  # >= threshold


def _make_long_file() -> FileRecord:
    """Build a DISCOVERED FileRecord that routes on the long branch."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.DISCOVERED,
    )


async def _drain_background() -> None:
    """Await any backgrounded enqueue tasks the router spawned (mirrors the router-test harness)."""
    for task in list(_background_tasks):
        await task


@pytest.mark.asyncio
async def test_long_file_routes_to_awaiting_cloud_not_compute(session: AsyncSession) -> None:
    """A long file is parked in AWAITING_CLOUD, NOT enqueued straight to a compute agent.

    Even with BOTH a compute and a fileserver agent online, the long-file branch holds the
    file and resolves NO compute queue -- the bounded staging cron is the single entry point.
    """
    await seed_active_agent(session, "cloud", kind="compute")
    await seed_active_agent(session, "nox", kind="fileserver")
    long_file = _make_long_file()
    session.add(long_file)
    await session.commit()

    router = FakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)
    result = await _route_discovered_by_duration(app_state, session, [(long_file, _LONG)], _THRESHOLD, "/models")

    assert result["awaiting"] == 1
    assert result["cloud"] == 0
    assert result["local"] == 0
    await _drain_background()
    # The long file is HELD in AWAITING_CLOUD ...
    await session.refresh(long_file)
    assert long_file.state == FileState.AWAITING_CLOUD
    # ... and NO queue was ever resolved/enqueued for it (no direct-to-compute path).
    assert router.captures == []
    assert "cloud" not in router.queue_for_calls


@pytest.mark.asyncio
async def test_no_direct_to_compute_enqueue_path(session: AsyncSession) -> None:
    """With ONLY a compute agent online, a long file is still held -- never enqueued to compute.

    This proves the seam has no routing path that enqueues to the compute agent directly,
    bypassing the bounded staging window (the compute agent is now ONLY a consumer reached
    via stage_cloud_window -> push_file, never from this router).
    """
    await seed_active_agent(session, "cloud", kind="compute")  # compute only, no fileserver
    long_file = _make_long_file()
    session.add(long_file)
    await session.commit()

    router = FakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)
    result = await _route_discovered_by_duration(app_state, session, [(long_file, _LONG)], _THRESHOLD, "/models")

    assert result["awaiting"] == 1
    assert result["cloud"] == 0
    await _drain_background()
    # Nothing enqueued anywhere; the file is held for the staging cron.
    assert router.captures == []
    assert router.queue_for_calls == []
    await session.refresh(long_file)
    assert long_file.state == FileState.AWAITING_CLOUD


# --- Phase 51 (D-02): the cloud_burst_enabled gate on the routing seam --------------------


@pytest.mark.asyncio
async def test_cloud_burst_disabled_routes_long_file_local(session: AsyncSession) -> None:
    """OFF (cloud_enabled=False): a long file routes LOCAL and is NEVER set to AWAITING_CLOUD (D-02).

    With the master toggle off, NOTHING is "long" -- the >=threshold file falls to the local
    branch, is enqueued onto the fileserver queue like any short file, and its state stays
    DISCOVERED. No row ever reaches AWAITING_CLOUD, so the cloud pipeline stays completely dormant.
    """
    await seed_active_agent(session, "nox", kind="fileserver")
    long_file = _make_long_file()
    session.add(long_file)
    await session.commit()

    router = FakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)
    result = await _route_discovered_by_duration(app_state, session, [(long_file, _LONG)], _THRESHOLD, False, "/models")

    assert result["local"] == 1
    assert result["awaiting"] == 0
    assert result["cloud"] == 0
    await _drain_background()
    # The long file routed local -- it stays DISCOVERED, never AWAITING_CLOUD.
    await session.refresh(long_file)
    assert long_file.state == FileState.DISCOVERED
    # It was enqueued onto the fileserver queue (the local path), not held.
    assert router.queue_for_calls == ["nox"]
    assert [t for t, _ in router.queues["nox"].captured] == ["process_file"]


@pytest.mark.asyncio
async def test_cloud_burst_enabled_holds_long_file(session: AsyncSession) -> None:
    """ON (cloud_enabled=True): a long file is HELD in AWAITING_CLOUD (Phase 49/50 regression)."""
    await seed_active_agent(session, "cloud", kind="compute")
    await seed_active_agent(session, "nox", kind="fileserver")
    long_file = _make_long_file()
    session.add(long_file)
    await session.commit()

    router = FakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)
    result = await _route_discovered_by_duration(app_state, session, [(long_file, _LONG)], _THRESHOLD, True, "/models")

    assert result["awaiting"] == 1
    assert result["local"] == 0
    await _drain_background()
    await session.refresh(long_file)
    assert long_file.state == FileState.AWAITING_CLOUD
    assert router.captures == []
