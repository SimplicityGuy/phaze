"""Phase 88 (88-02, DRILL-01 / D-06 / D-07 / D-00b / D-03): lane-detail helpers + endpoint.

Task 1 (this wave, RED-first) locks the two degrade-safe lane-data helpers in ``services/backends.py``:

* ``get_lane_recent_completions(session, backend_id, kind, limit=20)`` -- the last ``LANE_RECENT_N``
  succeeded ``CloudJob`` rows for a compute/kueue lane, newest-first (D-07); ``[]`` for a ``local`` lane
  (Open Question 1: a LocalBackend writes no cloud_job row) and ``[]`` on any query error (D-00b degrade);
* ``get_lane_queue_depths(app_state, backend_id)`` -- per-lane-tier queue depth, each source degrading
  to 0 on a missing ``app.state`` attr / broker hiccup (never a 500 into the 5s tick).

Task 2 extends this module with the ``GET /pipeline/lanes/{backend_id}`` endpoint + ``_lane_detail.html``
body assertions (kind-adaptivity, offline empty state, own-tick).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.cloud_job import CloudJob, CloudJobStatus


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_succeeded(session: AsyncSession, make_file, backend_id: str, n: int, base: datetime) -> None:  # type: ignore[no-untyped-def]
    """Seed ``n`` succeeded CloudJob rows for ``backend_id`` with strictly-increasing ``updated_at``.

    ``updated_at`` is a naive TIMESTAMP column (no ``timezone=True``); seed NAIVE datetimes so the
    round-trip stays naive and the newest-first assertion compares like-for-like (naive footgun).
    """
    jobs = []
    for i in range(n):
        file = await make_file(original_filename="done.mp3")
        jobs.append(
            CloudJob(
                id=uuid.uuid4(),
                file_id=file.id,
                s3_key=f"staging/{file.id}",
                status=CloudJobStatus.SUCCEEDED.value,
                backend_id=backend_id,
                created_at=base + timedelta(seconds=i),
                updated_at=base + timedelta(seconds=i),
            )
        )
    session.add_all(jobs)
    await session.commit()


@pytest.mark.asyncio
async def test_recent_completions_bounded_newest_first(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """25 succeeded rows -> exactly LANE_RECENT_N (20) returned, newest-first (D-07)."""
    from phaze.services.backends import LANE_RECENT_N, get_lane_recent_completions

    assert LANE_RECENT_N == 20
    base = datetime(2026, 7, 11, 12, 0, 0)  # naive on purpose (naive TIMESTAMP column)
    await _seed_succeeded(session, make_file, "compute-x", 25, base)

    # Noise that MUST be excluded: an in-flight row on the same backend, and a succeeded row on another.
    running_file = await make_file(original_filename="running.mp3")
    other_file = await make_file(original_filename="other.mp3")
    session.add_all(
        [
            CloudJob(id=uuid.uuid4(), file_id=running_file.id, status=CloudJobStatus.RUNNING.value, backend_id="compute-x"),
            CloudJob(id=uuid.uuid4(), file_id=other_file.id, status=CloudJobStatus.SUCCEEDED.value, backend_id="compute-y"),
        ]
    )
    await session.commit()

    rows = await get_lane_recent_completions(session, "compute-x", "compute")
    assert len(rows) == 20
    ups = [r.updated_at for r in rows]
    assert ups == sorted(ups, reverse=True)  # newest-first
    # The 20 newest of the 25 seeded (i=5..24); the oldest returned is base + 5s.
    assert min(ups) == base + timedelta(seconds=5)
    assert all(r.status == CloudJobStatus.SUCCEEDED.value for r in rows)
    assert all(r.backend_id == "compute-x" for r in rows)


@pytest.mark.asyncio
async def test_recent_completions_local_is_empty(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """A local lane yields NO completions even if a succeeded row carries its id (OQ1: local writes none)."""
    from phaze.services.backends import get_lane_recent_completions

    file = await make_file(original_filename="local-done.mp3")
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.SUCCEEDED.value, backend_id="local"))
    await session.commit()

    assert await get_lane_recent_completions(session, "local", "local") == []


@pytest.mark.asyncio
async def test_recent_completions_degrades_on_error(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A forced query error degrades to [] with a guarded rollback -- never raises (D-00b)."""
    from unittest.mock import AsyncMock

    from phaze.services.backends import get_lane_recent_completions

    monkeypatch.setattr(session, "execute", AsyncMock(side_effect=RuntimeError("boom")))
    assert await get_lane_recent_completions(session, "compute-x", "compute") == []


@pytest.mark.asyncio
async def test_queue_depths_degrade_to_zero_without_app_state() -> None:
    """A missing ``app.state.task_router`` degrades every lane-tier depth to 0, never raises (D-00b)."""

    from phaze.services.backends import get_lane_queue_depths

    class _BareState:
        pass

    depths = await get_lane_queue_depths(_BareState(), "compute-x")
    assert set(depths) == {"analyze", "fingerprint", "meta", "io"}
    assert all(v == 0 for v in depths.values())
