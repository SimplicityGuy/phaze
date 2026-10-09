"""Controller-side tests for `routers/pipeline/tracklists.py` (split from test_pipeline.py, phaze-7l8jh).

POST /pipeline/match-tracklists, the per-file prioritize/refresh/unprioritize actions, and the continuous-drain arm/disarm controls -- `routers/pipeline/tracklists.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.routers.pipeline._shared import (
    _cloud_compute_registry,  # noqa: F401 -- autouse fixture, never referenced by name
    _make_tracklist,
    drain_router_background_tasks,
    pytest,
    uuid,
    wire_fakes,
)


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_the_retired_bulk_scrape_triggers_are_gone(client: AsyncClient) -> None:
    """phaze-2akf: re-adding an unbounded bulk fan-out at manual must fail loudly.

    Asserted as 404s rather than merely by omission. The whole reason the drain exists is that the
    host budget is ~1 request / 8 s for the entire system, so a "just enqueue one per file" button
    is not a convenience -- it is the shape that made the legacy path unschedulable. A future
    "restore the old triggers" change should break a test, not quietly ship.
    """
    for path in ("/pipeline/search-tracklists", "/pipeline/scrape-tracklists"):
        assert (await client.post(path)).status_code == 404, path


@pytest.mark.asyncio
async def test_match_tracklists_routes_to_controller_queue(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/match-tracklists enqueues match_tracklist_to_discogs on the controller queue.

    match_tracklist_to_discogs is a CONTROLLER task (Phase-30 rule). The capture must be exactly
    {("controller","match_tracklist_to_discogs")} — never the consumer-less default queue (T-41-04).
    """
    tracklists = [_make_tracklist(i) for i in range(3)]
    session.add_all(tracklists)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/pipeline/match-tracklists")
    assert response.status_code == 200

    await drain_router_background_tasks()
    assert len(capture) == 3
    assert {(q, t) for q, t, _ in capture} == {("controller", "match_tracklist_to_discogs")}
    assert all(q != "default" for q, _, _ in capture)
    assert {c[2]["tracklist_id"] for c in capture} == {str(tl.id) for tl in tracklists}


@pytest.mark.asyncio
async def test_match_tracklists_excludes_discogs_reachable(client: AsyncClient, session: AsyncSession) -> None:
    """A tracklist already reachable from discogs_links is skipped from the match pending set."""
    from phaze.models.discogs_link import DiscogsLink
    from phaze.models.tracklist import TracklistTrack, TracklistVersion

    pending = _make_tracklist(1)
    linked = _make_tracklist(2)
    session.add_all([pending, linked])
    await session.flush()
    linked_version = TracklistVersion(id=uuid.uuid4(), tracklist_id=linked.id, version_number=1)
    session.add(linked_version)
    await session.flush()
    track = TracklistTrack(id=uuid.uuid4(), version_id=linked_version.id, position=1)
    session.add(track)
    await session.flush()
    session.add(DiscogsLink(id=uuid.uuid4(), track_id=track.id, discogs_release_id="r1", confidence=0.9))
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/pipeline/match-tracklists")
    assert response.status_code == 200

    await drain_router_background_tasks()
    assert len(capture) == 1
    assert capture[0][2]["tracklist_id"] == str(pending.id)


@pytest.mark.asyncio
async def test_match_tracklists_no_pending_returns_200(client: AsyncClient) -> None:
    """A zero-pending POST returns 200 and enqueues nothing (renders the tracklist-unit empty-state)."""
    capture = wire_fakes(client)
    response = await client.post("/pipeline/match-tracklists")
    assert response.status_code == 200
    assert "No tracklists ready for matching" in response.text

    await drain_router_background_tasks()
    assert capture == []
