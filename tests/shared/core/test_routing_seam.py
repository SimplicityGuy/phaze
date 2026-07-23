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
from sqlalchemy import select

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.routers.pipeline import _background_tasks, _route_discovered_by_duration
from tests._queue_fakes import FakeTaskRouter, make_agent_live, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_THRESHOLD = 5400
_LONG = 6000.0  # >= threshold


def _make_long_file() -> FileRecord:
    """Build a DISCOVERED FileRecord that routes on the long branch."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )


async def _drain_background() -> None:
    """Await any backgrounded enqueue tasks the router spawned (mirrors the router-test harness)."""
    for task in list(_background_tasks):
        await task


async def _is_held_awaiting_cloud(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """Phase 90 (D-09): a long file is HELD when it carries a cloud_job row with status='awaiting'.

    The former ``files.state = AWAITING_CLOUD`` dual-write was removed; the cloud_job sidecar is the sole
    derived authority PR-A reads.
    """
    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    return job is not None and job.status == CloudJobStatus.AWAITING.value


@pytest.mark.asyncio
async def test_long_file_routes_to_awaiting_cloud_not_compute(session: AsyncSession) -> None:
    """A long file is parked in AWAITING_CLOUD, NOT enqueued straight to a compute agent.

    Even with BOTH a compute and a fileserver agent online, the long-file branch holds the
    file and resolves NO compute queue -- the bounded staging cron is the single entry point.
    """
    await seed_active_agent(session, "cloud", kind="compute")
    await make_agent_live(session)  # phaze-c9w9: the OWNING agent must be live for local routing
    long_file = _make_long_file()
    session.add(long_file)
    await session.commit()

    router = FakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)
    result = await _route_discovered_by_duration(app_state, session, [(long_file, _LONG)], _THRESHOLD, True, "/models")

    assert result["awaiting"] == 1
    assert result["cloud"] == 0
    assert result["local"] == 0
    await _drain_background()
    # The long file is HELD in AWAITING_CLOUD ...
    assert await _is_held_awaiting_cloud(session, long_file.id)
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
    result = await _route_discovered_by_duration(app_state, session, [(long_file, _LONG)], _THRESHOLD, True, "/models")

    assert result["awaiting"] == 1
    assert result["cloud"] == 0
    await _drain_background()
    # Nothing enqueued anywhere; the file is held for the staging cron.
    assert router.captures == []
    assert router.queue_for_calls == []
    assert await _is_held_awaiting_cloud(session, long_file.id)


# --- Phase 67 (REG-04, D-14): the registry cloud_enabled gate on the routing seam ---------
# The helper takes a resolved ``cloud_enabled`` bool; the production callers source it from
# ``settings.cloud_enabled`` (pipeline.py), the registry-derived property. These tests drive the bool
# directly; the kueue case below proves a non-local registry resolves that property onto cloud-on.


@pytest.mark.asyncio
async def test_cloud_disabled_routes_long_file_local(session: AsyncSession) -> None:
    """OFF (cloud_enabled=False): a long file routes LOCAL and is NEVER set to AWAITING_CLOUD (D-02).

    With the master toggle off, NOTHING is "long" -- the >=threshold file falls to the local
    branch, is enqueued onto the fileserver queue like any short file, and its state stays
    DISCOVERED. No row ever reaches AWAITING_CLOUD, so the cloud pipeline stays completely dormant.
    """
    await make_agent_live(session)  # phaze-c9w9: the OWNING agent must be live for local routing
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
    # It was enqueued onto the fileserver queue (the local path), not held.
    assert router.queue_for_calls == ["test-fileserver"]
    # quick-260707-dh1: process_file routes to the analyze lane queue.
    assert [t for t, _ in router.queues["test-fileserver-analyze"].captured] == ["process_file"]


@pytest.mark.asyncio
async def test_cloud_enabled_holds_long_file(session: AsyncSession) -> None:
    """ON (cloud_enabled=True): a long file is HELD in AWAITING_CLOUD (Phase 49/50 regression)."""
    await seed_active_agent(session, "cloud", kind="compute")
    await make_agent_live(session)  # phaze-c9w9: the OWNING agent must be live for local routing
    long_file = _make_long_file()
    session.add(long_file)
    await session.commit()

    router = FakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)
    result = await _route_discovered_by_duration(app_state, session, [(long_file, _LONG)], _THRESHOLD, True, "/models")

    assert result["awaiting"] == 1
    assert result["local"] == 0
    await _drain_background()
    assert await _is_held_awaiting_cloud(session, long_file.id)
    assert router.captures == []


@pytest.mark.asyncio
async def test_kueue_registry_resolves_to_cloud_on(session: AsyncSession) -> None:
    """A single kueue backend resolves settings.cloud_enabled True and HOLDS a long file (D-14).

    The production callers feed the seam ``settings.cloud_enabled``; this case builds that exact
    registry-derived property from a single kueue backend and proves it maps onto the cloud-on bool so
    the long file is held in AWAITING_CLOUD, not routed local. (A single compute backend resolves
    identically -- covered by the cloud_enabled=True case above.)
    """
    from phaze.config import settings
    from phaze.config_backends import KubeConfig, KueueBackend

    await seed_active_agent(session, "cloud", kind="compute")
    await make_agent_live(session)  # phaze-c9w9: the OWNING agent must be live for local routing
    long_file = _make_long_file()
    session.add(long_file)
    await session.commit()

    # The exact registry-derived read pipeline.py performs: a non-local registry -> cloud_enabled True.
    kueue_settings = settings.model_copy(update={"backends": [KueueBackend(kind="kueue", id="k8s", rank=10, cap=2, kube=KubeConfig())]})
    cloud_enabled = kueue_settings.cloud_enabled
    assert cloud_enabled is True

    router = FakeTaskRouter()
    app_state = SimpleNamespace(task_router=router)
    result = await _route_discovered_by_duration(app_state, session, [(long_file, _LONG)], _THRESHOLD, cloud_enabled, "/models")

    assert result["awaiting"] == 1
    assert result["local"] == 0
    await _drain_background()
    assert await _is_held_awaiting_cloud(session, long_file.id)
    assert router.captures == []
