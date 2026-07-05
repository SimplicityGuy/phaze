"""Tests for the Phase-71 force-local routing override (BEUI-02).

Two behaviors are proven here:

* ``get_route_control`` (Task 2) -- the degrade-safe reader: True iff the seeded ``'global'`` row has
  ``force_local`` True; absent row -> False; any DB exception -> guarded rollback -> False, never raises
  (the reader is on the hot 5s poll + the routing gate, so a raise would 500 them -- T-71-03).
* the duration router (Task 3) -- with force-local engaged a new long file routes LOCAL and is NOT held
  in ``AWAITING_CLOUD``, behaving exactly like the all-local (``cloud_enabled=False``) path (D-08).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import pytest

from phaze.config import settings
from phaze.config_backends import ComputeBackend
from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.models.route_control import RouteControl
from phaze.services.route_control import get_route_control
from tests._queue_fakes import seed_active_agent, wire_fakes


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# A single compute backend -> cloud_enabled True + active_cloud_kind 'compute'; force-local then
# overrides it to the all-local path. Mirrors test_pipeline's _COMPUTE_BACKEND registry fixture.
_COMPUTE_BACKEND = ComputeBackend(kind="compute", id="a1", rank=10, cap=2, agent_ref="cloud-1", scratch_dir="/scratch")

_LONG = 6000.0  # >= cloud_route_threshold_sec default (5400)


async def _seed_route_control(session: AsyncSession, *, force_local: bool) -> None:
    """Seed (or update) the single ``'global'`` route_control row to ``force_local``."""
    row = await session.get(RouteControl, "global")
    if row is None:
        session.add(RouteControl(id="global", force_local=force_local))
    else:
        row.force_local = force_local
    await session.commit()


# --- Task 2: get_route_control degrade-safe reader --------------------------------------


@pytest.mark.asyncio
async def test_route_control_degrades_on_absent_row(session: AsyncSession) -> None:
    """No ``'global'`` row (pre-migration / empty table) -> False (cloud-enabled), never raises."""
    assert await get_route_control(session) is False


@pytest.mark.asyncio
async def test_route_control_reads_seeded_false(session: AsyncSession) -> None:
    """A seeded ``force_local=false`` row reads as False (cloud-enabled)."""
    await _seed_route_control(session, force_local=False)
    assert await get_route_control(session) is False


@pytest.mark.asyncio
async def test_route_control_reads_forced_true(session: AsyncSession) -> None:
    """A ``force_local=true`` row reads as True (force-local engaged)."""
    await _seed_route_control(session, force_local=True)
    assert await get_route_control(session) is True


@pytest.mark.asyncio
async def test_route_control_degrades_on_db_error() -> None:
    """Any DB exception degrades to False with a guarded rollback -- the reader NEVER raises (T-71-03)."""
    rolled_back = False

    class _BoomSession:
        async def get(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("boom")

        async def rollback(self) -> None:
            nonlocal rolled_back
            rolled_back = True

    assert await get_route_control(_BoomSession()) is False  # type: ignore[arg-type]
    assert rolled_back, "get_route_control must roll back the aborted transaction on a DB error"


# --- Task 3: duration router routes-local when force-local engaged ----------------------


@pytest.mark.asyncio
async def test_route_forced_local_no_hold(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force-local engaged: a new long file routes LOCAL (enqueued), NOT held in AWAITING_CLOUD (D-08).

    With a compute backend in the registry (cloud_enabled True) a long file would normally be HELD in
    AWAITING_CLOUD. Engaging force-local makes the effective flag ``cloud_enabled AND NOT force_local``
    False, so the duration router treats nothing as "long" -- the file routes to the fileserver queue
    exactly like the all-local path, and is never parked in AWAITING_CLOUD.
    """
    monkeypatch.setattr(settings, "backends", [_COMPUTE_BACKEND])
    await _seed_route_control(session, force_local=True)

    uid = uuid.uuid4()
    long_file = FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.DISCOVERED,
    )
    session.add(long_file)
    await session.flush()
    session.add(FileMetadata(file_id=uid, duration=_LONG))
    await session.commit()

    await seed_active_agent(session, "nox", kind="fileserver")
    wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    # Routed LOCAL, nothing held for the cloud path.
    assert data["local"] == 1
    assert data["awaiting_cloud"] == 0

    await session.refresh(long_file)
    assert long_file.state != FileState.AWAITING_CLOUD
