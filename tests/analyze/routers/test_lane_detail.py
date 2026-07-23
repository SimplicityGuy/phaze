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
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


def _render_lane_detail(**context: object) -> str:
    """Render the _lane_detail.html body directly (no request global) for template-level assertions."""
    from phaze.routers.pipeline import templates

    return templates.get_template("pipeline/partials/_lane_detail.html").render(**context)


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


async def _seed_succeeded_tied(session: AsyncSession, make_file, backend_id: str, job_ids: list[uuid.UUID], tied_at: datetime) -> None:  # type: ignore[no-untyped-def]
    """Seed one succeeded CloudJob per id in ``job_ids``, ALL sharing the SAME explicit ``updated_at``.

    ``job_ids`` order is the INSERTION order, deliberately unrelated to id order, so a query
    that fell back to heap/insertion order on the ``updated_at`` tie would produce a different
    sequence than the id-descending tiebreaker and the regression assertion would fail.
    """
    jobs = []
    for job_id in job_ids:
        file = await make_file(original_filename=f"tied-{job_id}.mp3")
        jobs.append(
            CloudJob(
                id=job_id,
                file_id=file.id,
                s3_key=f"staging/{file.id}",
                status=CloudJobStatus.SUCCEEDED.value,
                backend_id=backend_id,
                created_at=tied_at,
                updated_at=tied_at,
            )
        )
    session.add_all(jobs)
    await session.commit()


@pytest.mark.asyncio
async def test_recent_completions_tiebreaker_orders_tied_updated_at_by_id_desc(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """Rows with an IDENTICAL updated_at come back ordered by CloudJob.id DESC, not heap order.

    Seeds ``LANE_RECENT_N`` + 1 (21) succeeded rows sharing ONE explicit ``updated_at`` -- so
    ``updated_at`` alone leaves every row tied -- with ids assigned in a SCRAMBLED order
    relative to insertion (the exact flaky-test mistake this regression guards against: no
    clock-raced seeding, no hoping timestamps differ). Only the ``CloudJob.id`` tiebreaker on
    ``services.backends.get_lane_recent_completions`` makes the LIMIT-20 boundary total and
    deterministic.

    Regression guard for phaze-c6j5: reverting the ``, CloudJob.id.desc()`` suffix makes both
    the boundary membership and the in-page order depend on Postgres heap layout (verified:
    this assertion fails without the tiebreaker for the scrambled ids below).
    """
    from phaze.services.backends import LANE_RECENT_N, get_lane_recent_completions

    tied_at = datetime(2026, 7, 20, 12, 0, 0)  # naive on purpose (naive TIMESTAMP column)
    seed_count = LANE_RECENT_N + 1
    ids = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{i:02d}") for i in range(seed_count)]
    scrambled = ids[::2] + ids[1::2]  # e.g. [0,2,4,...,20,1,3,...,19]

    await _seed_succeeded_tied(session, make_file, "compute-x", scrambled, tied_at)

    rows = await get_lane_recent_completions(session, "compute-x", "compute")
    actual_ids = [row.id for row in rows]

    # LIMIT is LANE_RECENT_N (20 of the 21 seeded); the boundary + in-page order come entirely
    # from the id tiebreaker: the 20 LARGEST ids, strictly descending.
    assert len(actual_ids) == LANE_RECENT_N
    assert actual_ids == sorted(ids, reverse=True)[:LANE_RECENT_N]


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


# ---------------------------------------------------------------------------
# Task 2: GET /pipeline/lanes/{backend_id} endpoint + _lane_detail.html body.
# ---------------------------------------------------------------------------


async def _first_lane(session: AsyncSession) -> dict:  # type: ignore[type-arg]
    """Return the first degrade-safe snapshot lane (default registry -> a 'local' lane)."""
    from phaze.services.backends import get_backend_lane_snapshot

    lanes = await get_backend_lane_snapshot(session)
    if not lanes:
        pytest.skip("no backend lanes resolved in this environment")
    return lanes[0]


@pytest.mark.asyncio
async def test_lane_detail_known_lane_renders_fields(client: AsyncClient, session: AsyncSession) -> None:
    """A known lane -> 200 fragment with the RANK label, in-flight/cap, and the own 5s tick (DRILL-01)."""
    lane = await _first_lane(session)
    response = await client.get(f"/pipeline/lanes/{lane['id']}")
    assert response.status_code == 200, response.text
    body = response.text

    assert f"· {lane['id']}" in body
    assert f"RANK {lane['rank']}" in body
    assert f"{lane['in_flight']}/{lane['cap']}" in body
    # Queue depths degrade to 0 (test client skips the lifespan -> no task_router) but still render.
    assert "Queue depth" in body
    assert "Recent completions" in body
    # D-03 own-tick: the body self-refreshes on its own bounded 5s tick scoped to this lane.
    assert 'hx-trigger="every 5s"' in body
    assert f'hx-get="/pipeline/lanes/{lane["id"]}"' in body
    assert 'hx-target="#detail-pane"' in body


@pytest.mark.asyncio
async def test_lane_detail_unknown_lane_is_friendly_offline(client: AsyncClient, session: AsyncSession) -> None:
    """An unknown backend_id -> a friendly "Lane offline" fragment (200 HTML), never a 500 / JSON (T-88-03)."""
    await _first_lane(session)  # ensure the registry resolves in this env
    response = await client.get("/pipeline/lanes/__nope__")
    assert response.status_code == 200, response.text
    body = response.text
    assert "Lane offline" in body
    assert "offline" in body
    # Never a JSON error body.
    assert response.headers["content-type"].startswith("text/html")
    assert '{"detail"' not in body


@pytest.mark.asyncio
async def test_lane_detail_local_lane_no_completions_empty_state(client: AsyncClient, session: AsyncSession) -> None:
    """A local lane shows the "No completions in the last 20" empty state (OQ1: local writes none)."""
    lane = await _first_lane(session)
    if lane["kind"] != "local":
        pytest.skip("default registry did not resolve a local lane first")
    response = await client.get(f"/pipeline/lanes/{lane['id']}")
    assert response.status_code == 200, response.text
    assert "No completions in the last 20." in response.text


@pytest.mark.asyncio
async def test_lane_detail_template_kueue_shows_inadmissible() -> None:
    """A kueue lane renders quota-waiting + Inadmissible under the D-06 kueue-only branch."""
    kueue_lane = {"id": "k8s-a", "kind": "kueue", "rank": 3, "cap": 8, "in_flight": 2, "available": True, "quota_wait": 4, "inadmissible": 2}
    body = _render_lane_detail(lane=kueue_lane, recent_completions=[], queue_depths={}, refreshed_at=None, recent_n=20)
    assert "inadmissible" in body
    assert "waiting" in body
    assert 'role="alert"' in body  # inadmissible > 0 -> amber alert


@pytest.mark.asyncio
async def test_lane_detail_template_non_kueue_has_no_inadmissible() -> None:
    """A local/compute lane renders NO quota/Inadmissible row (D-06 -- no fabricated n/a fillers)."""
    for kind in ("local", "compute"):
        lane = {"id": f"{kind}-1", "kind": kind, "rank": 1, "cap": 4, "in_flight": 0, "available": True, "quota_wait": 0, "inadmissible": 0}
        body = _render_lane_detail(lane=lane, recent_completions=[], queue_depths={}, refreshed_at=None, recent_n=20)
        assert "inadmissible" not in body
        assert 'role="alert"' not in body


@pytest.mark.asyncio
async def test_lane_detail_unavailable_lane_renders_true_in_flight_not_zero() -> None:
    """phaze-pc2q: an unavailable lane must render its TRUE in_flight/cap, never a fabricated "0".

    Mirrors _lane_card.html's phaze-xd8k fix, reintroduced here: a lane can be unreachable for
    NEW dispatch (``available=False``) while still draining work it already accepted
    (``in_flight > 0``). A literal "0" reads as "nothing is running" and contradicts the sibling
    lane card, which already renders the true count for the identical offline state.
    """
    lane = {"id": "cloud-a", "kind": "compute", "rank": 2, "cap": 8, "in_flight": 3, "available": False, "quota_wait": 0, "inadmissible": 0}
    body = _render_lane_detail(lane=lane, recent_completions=[], queue_depths={}, refreshed_at=None, recent_n=20)
    assert "3/8" in body
    # The fabricated zero must not appear as the header numerator (a bare "0" span).
    assert ">0</span>" not in body


@pytest.mark.asyncio
async def test_lane_detail_no_unsafe_filter(client: AsyncClient, session: AsyncSession) -> None:
    """Operator-declared lane id/kind stay Jinja-autoescaped -- never |safe (T-88-05)."""
    lane = await _first_lane(session)
    response = await client.get(f"/pipeline/lanes/{lane['id']}")
    assert response.status_code == 200
    assert "|safe" not in response.text
