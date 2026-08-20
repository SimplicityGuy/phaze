"""Controller-side tests for `routers/pipeline/tracklists.py` (split from test_pipeline.py, phaze-7l8jh).

POST /pipeline/match-tracklists, the per-file prioritize/refresh/unprioritize actions, and the continuous-drain arm/disarm controls -- `routers/pipeline/tracklists.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.routers.pipeline._shared import (
    FileMetadata,
    Tracklist,
    _cloud_compute_registry,  # noqa: F401 -- autouse fixture, never referenced by name
    _link_propagated_tracklist,
    _link_tracklist,
    _make_tracklist,
    _seed_live_set_file,
    drain_router_background_tasks,
    pytest,
    select,
    uuid,
    wire_fakes,
)


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_the_retired_bulk_scrape_triggers_are_gone(client: AsyncClient) -> None:
    """phaze-2akf: re-adding an unbounded bulk fan-out at 1001Tracklists must fail loudly.

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


@pytest.mark.asyncio
async def test_prioritize_persists_flag_and_enqueues_one_bounded_slice(client: AsyncClient, session: AsyncSession) -> None:
    """POST .../prioritize persists the flag AND enqueues exactly one drain_tracklists(limit=1) job.

    The persistence is the point of phaze-fq9h.8: without it, the flag would last exactly as long
    as this one job (the phaze-fq9h.7 gap the bead exists to close).
    """
    from phaze.services.tracklist_priority import load_flagged_file_ids

    file_rec = await _seed_live_set_file(session)
    capture = wire_fakes(client)

    response = await client.post(f"/pipeline/tracklists/{file_rec.id}/prioritize")
    assert response.status_code == 200
    assert "Lookup queued for this exact set" in response.text
    assert "Look up now" not in response.text or "cancel" in response.text.lower()

    assert await load_flagged_file_ids(session) == {file_rec.id}
    assert {(q, t) for q, t, _ in capture} == {("controller", "drain_tracklists")}
    assert capture[0][2].get("limit") == 1
    assert capture[0][2].get("target_file_ids") == [str(file_rec.id)]


@pytest.mark.asyncio
async def test_prioritize_ineligible_file_flags_nothing_and_enqueues_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """A file that would never enter the drain queue (too short -> TRACK) is not flagged.

    Flagging it would have zero effect on ordering while a limit=1 slice would spend its one
    request on a completely unrelated set at the front of the queue -- a misattributed lookup the
    endpoint refuses to cause.
    """
    from phaze.services.tracklist_priority import load_flagged_file_ids

    file_rec = await _seed_live_set_file(session, duration=120.0)
    capture = wire_fakes(client)

    response = await client.post(f"/pipeline/tracklists/{file_rec.id}/prioritize")
    assert response.status_code == 200
    assert "Not yet looked up" in response.text

    assert await load_flagged_file_ids(session) == set()
    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_prioritize_file_with_embedded_tracklist_flags_nothing_and_enqueues_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """A file already answered by an embedded tracklist (no ``tracklists`` row) is not flagged.

    This is the exact scope-creep shape a full-suite reviewer flagged: a long-duration, set-shaped
    file with no ``tracklists`` row classifies LIVE_SET on duration+filename alone, but the
    corpus-wide funnel excludes it BEFORE classification because it already carries an embedded
    tracklist. If the endpoint only checked classification, it would flag this file and enqueue a
    limit=1 slice that could never answer it -- spending a live request on an unrelated set while
    reporting success for this one.
    """
    from phaze.services.tracklist_priority import load_flagged_file_ids

    file_rec = await _seed_live_set_file(session)
    file_metadata_result = await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_rec.id))
    metadata = file_metadata_result.scalar_one()
    metadata.raw_tags = {"comment": "00:00 Opener\n05:00 Second track\n10:00 Third track"}
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post(f"/pipeline/tracklists/{file_rec.id}/prioritize")
    assert response.status_code == 200
    assert "excluded from the drain queue" in response.text

    assert await load_flagged_file_ids(session) == set()
    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_prioritize_already_tracklisted_file_is_a_noop(client: AsyncClient, session: AsyncSession) -> None:
    """A file that already has a tracklist is not (re-)flagged and nothing is enqueued."""
    from phaze.services.tracklist_priority import load_flagged_file_ids

    file_rec = await _seed_live_set_file(session)
    session.add(_link_tracklist(file_rec))
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post(f"/pipeline/tracklists/{file_rec.id}/prioritize")
    assert response.status_code == 200

    assert await load_flagged_file_ids(session) == set()
    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_prioritize_a_cache_suppressed_negative_flags_nothing_and_enqueues_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-z8xq7: a live set whose lookup cached NOT_FOUND (still inside its 180-day TTL) is
    ``eligible`` by classification but must NOT be flagged or enqueued -- the drain keeps a
    cache-suppressed set out of the queue even when force-flagged, so doing either would upsert an
    inert flag and spend the enqueued limit=1 slice's one request on an unrelated set while
    falsely claiming a lookup was dispatched.
    """
    from dataclasses import replace

    from phaze.enums.tracklist_candidate import LookupOutcome
    from phaze.services.tracklist_candidates import CandidateSignals, group_unique_sets
    from phaze.services.tracklist_lookup_cache import record_outcome
    from phaze.services.tracklist_priority import load_flagged_file_ids
    from phaze.services.tracklist_query import derive_query

    file_rec = await _seed_live_set_file(session)
    signals = CandidateSignals(file_id=file_rec.id, filename=file_rec.original_filename, sha256_hash=file_rec.sha256_hash, duration_seconds=7200.0)
    derived = derive_query(signals.filename)
    signals = replace(signals, derived_query=derived.query)
    key = group_unique_sets([signals])[0].key
    await record_outcome(session, set_key=key, query_text="prioritize suppressed test", outcome=LookupOutcome.NOT_FOUND)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post(f"/pipeline/tracklists/{file_rec.id}/prioritize")
    assert response.status_code == 200
    assert "Prioritized and queued" not in response.text
    assert "Look up again becomes available" in response.text

    assert await load_flagged_file_ids(session) == set()
    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_refresh_rearms_the_drain_for_an_already_tracklisted_file(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-2akf: REFRESH is what Prioritize is for a file that already HAS a tracklist.

    The endpoint spends no host request itself -- it drops the cached answer, flags the file, and
    enqueues ONE bounded drain slice that pays for the re-read through the same path and the same
    whole-host budget as every other lookup. Asserting the enqueue is ``drain_tracklists`` on the
    CONTROLLER queue is the load-bearing part: routing it anywhere else, or fanning it out per row,
    would rebuild the unbounded second consumer this bead exists to remove.
    """
    from phaze.services.tracklist_priority import load_flagged_file_ids

    file_rec = await _seed_live_set_file(session)
    session.add(_link_tracklist(file_rec))
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post(f"/pipeline/tracklists/{file_rec.id}/refresh")
    assert response.status_code == 200
    assert "Refresh requested" in response.text

    assert await load_flagged_file_ids(session) == {file_rec.id}
    await drain_router_background_tasks()
    assert {(q, t) for q, t, _ in capture} == {("controller", "drain_tracklists")}
    assert [c[2].get("limit") for c in capture] == [1]
    assert [c[2].get("target_file_ids") for c in capture] == [[str(file_rec.id)]]


@pytest.mark.asyncio
async def test_refresh_rearms_the_drain_for_a_propagated_files_button(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-vtovq / phaze-97pkq: REFRESH from a PROPAGATED (duplicate) file's own button must work.

    The record page renders the "Refresh from 1001Tracklists" button for ANY file that has a
    tracklist, including a propagated projection -- so the button posts the DUPLICATE's own file
    id, not the canonical row's. Before phaze-97pkq's fix, ``_resolve_targets``'s file-id branch
    ANDed the canonical filter directly onto that file-id predicate, and the row in scope was
    itself a projection -- so the intersection was always empty: ``refreshed`` stayed 0, no cache
    row was cleared, neither file was flagged, and the button silently did nothing. A status-only
    assertion would have passed against that exact bug (the sibling test above's neighbour,
    ``test_refresh_unknown_file_renders_fragment_not_dropped_404``, warns about the same shape of
    trap), so this asserts the FULL outcome: the response confirms a refresh happened -- and since
    exactly one canonical page is in play here, "Refresh requested" appearing IS ``refreshed == 1``
    -- BOTH the canonical file and the duplicate end up flagged, the positive cache row for the
    shared page is gone, and exactly one ``drain_tracklists`` slice is enqueued on the controller
    queue with ``limit=1``.
    """
    from phaze.enums.tracklist_candidate import LookupOutcome
    from phaze.models.tracklist_lookup_cache import TracklistLookupCache
    from phaze.services.tracklist_lookup_cache import record_outcome
    from phaze.services.tracklist_priority import load_flagged_file_ids

    external_id = f"shared-{uuid.uuid4().hex[:12]}"
    set_key = f"set-{uuid.uuid4().hex[:12]}"
    canonical_file = await _seed_live_set_file(session)
    duplicate_file = await _seed_live_set_file(session)
    session.add(
        Tracklist(
            external_id=external_id,
            source_url=f"https://www.1001tracklists.com/tracklist/{external_id}/x.html",
            file_id=canonical_file.id,
        )
    )
    session.add(_link_propagated_tracklist(duplicate_file, external_id=external_id, set_key=set_key))
    await session.commit()
    await record_outcome(session, set_key=set_key, query_text="propagated refresh test", outcome=LookupOutcome.FOUND, external_id=external_id)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post(f"/pipeline/tracklists/{duplicate_file.id}/refresh")
    assert response.status_code == 200
    assert "Refresh requested" in response.text

    assert await load_flagged_file_ids(session) == {canonical_file.id, duplicate_file.id}

    cache_rows = (await session.execute(select(TracklistLookupCache).where(TracklistLookupCache.external_id == external_id))).scalars().all()
    assert cache_rows == [], "the positive cache row for the shared page must be cleared"

    await drain_router_background_tasks()
    assert {(q, t) for q, t, _ in capture} == {("controller", "drain_tracklists")}
    assert [c[2].get("limit") for c in capture] == [1]
    assert [c[2].get("target_file_ids") for c in capture] == [[str(duplicate_file.id)]]


@pytest.mark.asyncio
async def test_refresh_on_a_file_with_no_tracklist_does_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """Nothing to re-read means nothing to spend -- the control is not offered in this state either."""
    from phaze.services.tracklist_priority import load_flagged_file_ids

    file_rec = await _seed_live_set_file(session)
    capture = wire_fakes(client)

    response = await client.post(f"/pipeline/tracklists/{file_rec.id}/refresh")
    assert response.status_code == 200
    assert "Refresh requested" not in response.text

    assert await load_flagged_file_ids(session) == set()
    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_refresh_unknown_file_renders_fragment_not_dropped_404(client: AsyncClient) -> None:
    """phaze-9xyjp: an unknown file renders the review fragment (200), never a 404 htmx drops.

    This handler's response swaps into ``#tracklist-review-{file_id}`` and htmx 2.x's stock
    ``responseHandling`` does not swap a 4xx body (response_shape.py rule 3) -- a bare 404
    here is silently discarded and the pane sits unchanged with no operator feedback. A
    status-only assertion would have passed against that exact bug, so this asserts the body
    htmx actually swaps: the fragment's own "File not found." rendering.
    """
    response = await client.post(f"/pipeline/tracklists/{uuid.uuid4()}/refresh")
    assert response.status_code == 200, response.text
    assert "file not found" in response.text.lower()


@pytest.mark.asyncio
async def test_prioritize_unknown_file_renders_fragment_not_dropped_404(client: AsyncClient) -> None:
    """phaze-9xyjp: same vanished-file race as refresh -- see that test's rationale."""
    response = await client.post(f"/pipeline/tracklists/{uuid.uuid4()}/prioritize")
    assert response.status_code == 200, response.text
    assert "file not found" in response.text.lower()


@pytest.mark.asyncio
async def test_unprioritize_clears_a_flag(client: AsyncClient, session: AsyncSession) -> None:
    from phaze.services.tracklist_priority import flag_file_for_lookup, load_flagged_file_ids

    file_rec = await _seed_live_set_file(session)
    await flag_file_for_lookup(session, file_rec.id)
    await session.commit()

    response = await client.post(f"/pipeline/tracklists/{file_rec.id}/unprioritize")
    assert response.status_code == 200
    assert "Look up now" in response.text

    assert await load_flagged_file_ids(session) == set()


@pytest.mark.asyncio
async def test_unprioritize_unknown_file_renders_fragment_not_dropped_404(client: AsyncClient) -> None:
    """phaze-9xyjp: same vanished-file race as refresh/prioritize -- see that test's rationale."""
    response = await client.post(f"/pipeline/tracklists/{uuid.uuid4()}/unprioritize")
    assert response.status_code == 200, response.text
    assert "file not found" in response.text.lower()


@pytest.mark.asyncio
async def test_tracklist_drain_status_fragment_renders_the_honest_ceiling_and_eta(client: AsyncClient, session: AsyncSession) -> None:
    """GET /pipeline/tracklist-drain-status renders queue depth, the daily ceiling, and an ETA.

    Reads through phaze.tasks.tracklist_drain.tracklist_drain_status directly (a request-free
    read) rather than a second, hand-rolled status query.
    """
    await _seed_live_set_file(session)

    response = await client.get("/pipeline/tracklist-drain-status")
    assert response.status_code == 200
    assert "lookups/day" in response.text
    assert "Queued" in response.text


@pytest.mark.asyncio
async def test_run_tracklist_drain_enqueues_one_job_on_the_controller_queue(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/run-tracklist-drain enqueues exactly one drain_tracklists job (no bulk loop)."""
    capture = wire_fakes(client)

    response = await client.post("/pipeline/run-tracklist-drain")
    assert response.status_code == 200
    assert "Queued" in response.text

    assert len(capture) == 1
    assert capture[0][0] == "controller"
    assert capture[0][1] == "drain_tracklists"


# ---------------------------------------------------------------------------
# phaze-6nrrf: the continuous-drain ARM/DISARM operator control.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arm_persists_armed_state_and_enqueues_nothing_itself(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/arm-tracklist-drain flips the durable flag but does NOT enqueue a slice --
    that is the continue_armed_tracklist_drain CronJob's job, on its own next tick."""
    capture = wire_fakes(client)

    response = await client.post("/pipeline/arm-tracklist-drain")
    assert response.status_code == 200, response.text
    assert "Armed" in response.text
    assert capture == []

    from phaze.services.tracklist_drain_arm import get_arm_state

    state = await get_arm_state(session)
    assert state.armed is True


@pytest.mark.asyncio
async def test_disarm_persists_disarmed_state_with_operator_reason(client: AsyncClient, session: AsyncSession) -> None:
    from phaze.services.tracklist_drain_arm import arm_drain, get_arm_state

    await arm_drain(session)
    await session.commit()

    response = await client.post("/pipeline/disarm-tracklist-drain")
    assert response.status_code == 200, response.text
    assert "Disarmed" in response.text

    state = await get_arm_state(session)
    assert state.armed is False
    assert state.disarmed_reason == "operator"


@pytest.mark.asyncio
async def test_disarm_when_already_disarmed_is_a_no_op_not_an_error(client: AsyncClient) -> None:
    response = await client.post("/pipeline/disarm-tracklist-drain")
    assert response.status_code == 200, response.text
    assert "Disarmed" in response.text


@pytest.mark.asyncio
async def test_drain_status_fragment_renders_disarmed_by_default(client: AsyncClient, session: AsyncSession) -> None:
    """A fresh workspace load must show Disarmed -- the DEFAULT OFF safety invariant, visible to
    the operator, not just true in the database."""
    await _seed_live_set_file(session)

    response = await client.get("/pipeline/tracklist-drain-status")
    assert response.status_code == 200
    assert "Disarmed" in response.text
    assert "Armed" not in response.text  # "Disarmed" does not contain the substring "Armed"


@pytest.mark.asyncio
async def test_drain_status_fragment_renders_armed_after_arming(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_live_set_file(session)

    arm_response = await client.post("/pipeline/arm-tracklist-drain")
    assert "Armed" in arm_response.text

    status_response = await client.get("/pipeline/tracklist-drain-status")
    assert status_response.status_code == 200
    assert "Armed since" in status_response.text
