"""Real capture/import/decision consumers, with external effects kept outside the DB boundary."""

from datetime import UTC, datetime
from fractions import Fraction
import uuid

import pytest
from sqlalchemy import select, update

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.provider_source import ProviderRecordingSelection, ProviderSourceObservation
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.cue_generator import generate_cue_content
from phaze.services.cue_review import build_cue_tracks_for_file
from phaze.services.local_source_import import decide_local_source, import_local_source
from phaze.services.pipeline.tracklists import get_untracked_files
from phaze.services.provider_persistence import add_candidate, select_observation, store_snapshot
from phaze.services.search_queries import SearchFacets, search
from phaze.services.selected_cue import build_selected_cue_artifact
from phaze.services.selected_source_consumers import tag_release_projection
from phaze.services.source_workspace import get_existing_cue_page, get_source_workspace_page
from phaze.services.stage_status import Stage, done_clause
from phaze.services.tag_comparison import _encode_tag_review_token, _get_tracklist_for_file, _get_tracklists_for_files, _tag_review_payload
from phaze.services.tag_proposal import compute_proposed_tags
from phaze.services.tag_writer import enqueue_tag_write
from phaze.services.track_segments import build_track_segments
from phaze.services.tracklist_review import get_file_tracklist_review
from phaze.tasks.proposal import validate_selected_proposal_context
from phaze.tracklist_providers.domain import ReleaseFact, Snapshot
from tests._queue_fakes import install_fake_queues
from tests.integration.test_local_source_import import TEXT, decision, inventory


async def test_actual_selected_embedded_workspace_current_and_changed_channel(session):
    media, _companion, _raw = await inventory(session)
    tags = FileMetadata(file_id=media.id, raw_tags={"comments": TEXT})
    session.add(tags)
    await session.flush()
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, embedded_channel="comments"))
    observation = await session.get(ProviderSourceObservation, imported.tracklist_observation_id)
    await decide_local_source(session, decision(media, media, observation.id, revision=observation.revision), actor="reviewer")
    current = (await get_source_workspace_page(session, "tracklist")).rows
    assert len(current) == 1 and current[0]["selected"] and current[0]["availability"] == "current"
    assert (await _get_tracklist_for_file(session, media.id)).eligible
    tags.raw_tags = {"comments": "01. Changed - Title\n"}
    await session.flush()
    changed = (await get_source_workspace_page(session, "tracklist")).rows
    assert len(changed) == 1 and changed[0]["availability"] == "stale"
    assert not (await _get_tracklist_for_file(session, media.id)).eligible
    tags.raw_tags = {"comments": TEXT}
    media.sha256_hash = "f" * 64
    await session.flush()
    assert (await get_source_workspace_page(session, "tracklist")).rows[0]["availability"] == "stale"
    media.sha256_hash = "a" * 64
    # Oversized current channel evidence is unknown/stale, never transported into a small summary.
    tags.raw_tags = {"comments": "x" * 2097153}
    await session.flush()
    assert (await get_source_workspace_page(session, "tracklist")).rows[0]["availability"] == "stale"
    assert not (await _get_tracklist_for_file(session, media.id)).eligible
    tags.raw_tags = {"comments": TEXT}
    media.missing_at = datetime.now(UTC)
    await session.flush()
    assert (await get_source_workspace_page(session, "tracklist")).rows[0]["availability"] == "missing"
    assert not (await _get_tracklist_for_file(session, media.id)).eligible
    media.missing_at = None
    media.file_type = "txt"
    await session.flush()
    assert (await get_source_workspace_page(session, "tracklist")).rows[0]["availability"] == "unavailable"
    assert not (await _get_tracklist_for_file(session, media.id)).eligible


CUE = 'PERFORMER "Reviewed Artist"\nTITLE "Reviewed Set"\nREM DATE 2024-01-02\nFILE "recording.mp3" MP3\n TRACK 01 AUDIO\n TITLE "First"\n INDEX 01 00:01:01\n TRACK 02 AUDIO\n TITLE "Second"\n INDEX 01 00:02:02\n'


async def test_actual_imported_unknown_clock_and_minute_sources_never_become_exact_boundaries(session):
    text = "01. [00:01] Artist - Unknown origin\n02. [clock 12:34] Artist - Clock\n03. [2 min] Artist - Coarse\n"
    media, companion, raw = await inventory(session, text, "txt")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id), actor="reviewer")
    review = await get_file_tracklist_review(session, media.id)
    assert [track.timestamp.kind for track in review.tracks] == ["unknown", "clock", "unknown"]
    assert review.tracks[2].timestamp.precision == "minute"
    assert [track.timestamp.original for track in review.tracks] == ["00:01", "clock 12:34", "2 min"]
    assert not build_track_segments(review.tracks, [], 30)
    assert all(track.timestamp_seconds is None for track in await build_cue_tracks_for_file(session, media.id))
    with pytest.raises(ValueError, match="no qualified recording offsets"):
        await build_selected_cue_artifact(session, media.id)


async def test_actual_multi_file_cue_without_reviewed_mapping_has_no_exact_projection(session):
    text = 'FILE "one.mp3" MP3\n TRACK 01 AUDIO\n TITLE "First"\n INDEX 01 00:01:01\nFILE "two.mp3" MP3\n TRACK 01 AUDIO\n TITLE "Second"\n INDEX 01 00:02:02\n'
    media, _, raw = await inventory(session, text, "cue")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    # The older persistence API can retain historical selections with no target mapping.
    await select_observation(session, media.id, imported.tracklist_observation_id, kind="tracklist", actor="historical-reviewer")
    review = await get_file_tracklist_review(session, media.id)
    assert review.selected_source.availability == "unknown_binding" and not review.tracks
    rows = (await get_source_workspace_page(session, "cue")).rows
    assert next(row for row in rows if row["selected"])["availability"] == "unknown_binding"
    assert not (await _get_tracklist_for_file(session, media.id)).eligible
    assert len(review.selected_source.snapshot.tracks) == 2
    assert not await build_cue_tracks_for_file(session, media.id)
    assert not build_track_segments(review.tracks, [], 30)
    with pytest.raises(ValueError, match="unknown_binding"):
        await build_selected_cue_artifact(session, media.id)


async def test_actual_selected_cue_consumers_and_stale_no_legacy_fallback(session):
    media, companion, raw = await inventory(session, CUE, "cue")
    legacy = Tracklist(file_id=media.id, source="manual", external_id="synthetic-legacy", artist="Legacy Artist", source_url="")
    session.add(legacy)
    await session.flush()
    version = TracklistVersion(tracklist_id=legacy.id, version_number=1)
    session.add(version)
    await session.flush()
    legacy.latest_version_id = version.id
    session.add(TracklistTrack(version_id=version.id, position=1, artist="Legacy", title="Old", timestamp="00:10"))
    await session.flush()
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert (await get_file_tracklist_review(session, media.id)).tracklist.id == legacy.id
    selected = await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id, ordinal=1), actor="reviewer")
    review = await get_file_tracklist_review(session, media.id)
    assert review.selected_source.observation_id == selected.observation_id and review.tracklist is None
    assert [track.title for track in review.tracks] == ["First", "Second"]
    segments = build_track_segments(review.tracks, [], 30)
    assert segments[0].start_sec == float(Fraction(76, 75))
    cue = await build_cue_tracks_for_file(session, media.id)
    assert cue[0].timestamp_seconds == Fraction(76, 75)
    output = generate_cue_content("recording.mp3", "mp3", cue)
    assert "INDEX 01 00:01:01" in output and "INDEX 01 00:02:02" in output
    # Repeated import and newer pending parser output cannot change authority.
    again = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert again.tracklist_observation_id == selected.observation_id
    assert generate_cue_content("recording.mp3", "mp3", await build_cue_tracks_for_file(session, media.id)) == output
    companion.sha256_hash = "b" * 64
    await session.flush()
    stale = await get_file_tracklist_review(session, media.id)
    assert stale.tracklist is None and stale.selected_source.availability == "stale"
    assert not build_track_segments(stale.tracks, [], 30)
    assert all(track.timestamp_seconds is None for track in await build_cue_tracks_for_file(session, media.id))
    assert (await session.get(ProviderSourceObservation, selected.observation_id)).payload["tracks"][0]["timestamp"]["offset"]["numerator"] == 76


async def test_release_selection_tag_reads_and_binding_review_evidence(session):
    media, companion, raw = await inventory(session, "Artist: Reviewed Artist\nAlbum: Reviewed Album\nDate: 2024-01-02\nGenre: House\n")
    metadata = FileMetadata(file_id=media.id, artist="Embedded Artist", album="Embedded Album")
    session.add(metadata)
    await session.flush()
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    before = compute_proposed_tags(metadata, await _get_tracklist_for_file(session, media.id), media.original_filename)
    assert before["artist"] == "Embedded Artist"
    await decide_local_source(session, decision(media, companion, imported.release_observation_id, kind="release_metadata"), actor="reviewer")
    single = await _get_tracklist_for_file(session, media.id)
    batch = (await _get_tracklists_for_files(session, [media.id]))[media.id]
    assert single == batch and single.eligible
    proposed = compute_proposed_tags(metadata, single, media.original_filename)
    assert proposed == {"artist": "Reviewed Artist", "album": "Reviewed Album", "year": 2024, "genre": "House"}
    media.file_metadata = metadata
    original_payload = _tag_review_payload(media, single, None, proposed)
    assert original_payload["sources"]["selected_source_evidence"][0]["availability"] == "current"
    companion.sha256_hash = "b" * 64
    await session.flush()
    stale = await _get_tracklist_for_file(session, media.id)
    assert not stale.eligible and stale.evidence[0]["availability"] == "stale"
    assert _tag_review_payload(media, stale, None, compute_proposed_tags(metadata, stale, media.original_filename)) != original_payload


async def test_pending_import_never_grants_stage_completion(session):
    media, companion, raw = await inventory(session)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert media.id in [item.id for item in await get_untracked_files(session)]
    assert not await session.scalar(select(done_clause(Stage.TRACKLIST)).where(FileRecord.id == media.id))
    await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id), actor="reviewer")
    assert media.id not in [item.id for item in await get_untracked_files(session)]


async def test_cached_inventory_and_selection_cannot_reuse_old_review_evidence(session):
    media, companion, raw = await inventory(session, "Artist: Reviewed Artist\nAlbum: Reviewed Album\n")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await decide_local_source(session, decision(media, companion, imported.release_observation_id, kind="release_metadata"), actor="reviewer")
    first = await _get_tracklist_for_file(session, media.id)
    context = {"reviewed_source_evidence": first.evidence}
    assert await validate_selected_proposal_context(session, [str(media.id)], [context])
    # An independently committed inventory update must beat an already-cached ORM file.
    # Core SQL deliberately leaves the identity map untouched, reproducing that cached state.
    await session.execute(
        update(FileRecord).where(FileRecord.id == media.id).values(sha256_hash="c" * 64).execution_options(synchronize_session=False)
    )
    assert media.sha256_hash == "a" * 64
    changed = await _get_tracklist_for_file(session, media.id)
    assert not changed.eligible and changed.evidence[0]["media_sha256"] == "c" * 64
    assert not await validate_selected_proposal_context(session, [str(media.id)], [context])
    await session.execute(
        update(FileRecord).where(FileRecord.id == media.id).values(sha256_hash="a" * 64).execution_options(synchronize_session=False)
    )
    selection = await session.scalar(select(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media.id))
    prior_token = selection.selection_token
    replacement_token = uuid.uuid4()
    await session.execute(
        update(ProviderRecordingSelection)
        .where(ProviderRecordingSelection.media_id == media.id)
        .values(selection_token=replacement_token)
        .execution_options(synchronize_session=False)
    )
    assert selection.selection_token == prior_token
    fresh = await _get_tracklist_for_file(session, media.id)
    assert fresh.evidence[0]["selection_token"] == str(replacement_token)
    assert not await validate_selected_proposal_context(session, [str(media.id)], [context])


async def test_search_uses_selected_facts_not_newer_candidates(session):
    media, companion, raw = await inventory(session, "Artist: Reviewed Artist\nAlbum: Reviewed Album\nGenre: House\n")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    pending, _ = await search(session, "Reviewed")
    assert not pending
    await decide_local_source(session, decision(media, companion, imported.release_observation_id, kind="release_metadata"), actor="reviewer")
    results, _ = await search(session, "Reviewed", facets=SearchFacets(artist="Reviewed Artist", genre="House"))
    assert len(results) == 1 and results[0].id == str(media.id)
    assert results[0].artist == "Reviewed Artist" and results[0].genre == "House"


async def test_workspaces_read_real_candidates_and_existing_cue_inventory(session):
    media, companion, raw = await inventory(session, CUE, "cue")
    inventory_page = await get_existing_cue_page(session, page_size=10)
    assert inventory_page.rows[0]["source_file_id"] == companion.id
    assert not (await get_source_workspace_page(session, "cue")).rows
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    tracks = await get_source_workspace_page(session, "tracklist")
    assert len(tracks.rows) == 1 and tracks.rows[0]["track_count"] == 2 and not tracks.rows[0]["selected"]
    assert len((await get_source_workspace_page(session, "release_metadata")).rows) == 1
    assert len((await get_source_workspace_page(session, "cue")).rows) == 2
    await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id, ordinal=1), actor="reviewer")
    reviewed = await get_source_workspace_page(session, "tracklist")
    assert reviewed.rows[0]["selected"] and reviewed.rows[0]["selected_actor"] == "reviewer"
    assert len(reviewed.rows[0]["text_preview"]) <= 512 and "payload" not in reviewed.rows[0]


async def test_tag_projection_discards_large_irrelevant_evidence_and_keeps_late_facts(session):
    media, companion, raw = await inventory(session, "Artist: First Artist\nAlbum: First Album\n")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    original = await session.get(ProviderSourceObservation, imported.release_observation_id)
    snapshot = Snapshot.model_validate({**original.payload, "retrieved_at": original.retrieved_at, "url": original.source_url})
    facts = (
        *(ReleaseFact(field="label", value="x" * 4096, original_value="o" * 4096, certainty="known", evidence=("e" * 4096,)) for _ in range(100)),
        ReleaseFact(field="artist", value="Earlier", certainty="known"),
        ReleaseFact(field="event", value="Earlier Event", certainty="known"),
        ReleaseFact(field="album", value="Late Album", certainty="known"),
        ReleaseFact(field="artist", value="Late Artist", certainty="known"),
        ReleaseFact(field="genre", value="House", certainty="known"),
    )
    stored = await store_snapshot(
        session, snapshot.model_copy(update={"release_facts": facts}), source_file_id=companion.id, channel="companion", parent_id=raw
    )
    await add_candidate(session, media.id, stored.observation.id)
    await decide_local_source(
        session,
        decision(media, companion, stored.observation.id, kind="release_metadata").model_copy(update={"accept_conflict": True}),
        actor="reviewer",
    )
    projected = await session.scalar(select(tag_release_projection()).where(ProviderSourceObservation.id == stored.observation.id))
    assert len(projected) == 5 and all(set(fact) == {"field", "certainty", "value"} for fact in projected)
    source = await _get_tracklist_for_file(session, media.id)
    assert source.tags == {"artist": "Late Artist", "album": "Late Album", "genre": "House"}
    assert len(str(projected)) < 4096 and len(str(stored.observation.payload)) > 1048576


async def test_actual_selected_tag_approval_and_write_boundary_revalidation(client, session):
    media, companion, raw = await inventory(session, "Artist: Reviewed Artist\nAlbum: Reviewed Album\n")
    metadata = FileMetadata(file_id=media.id, artist="Embedded Artist")
    session.add_all([metadata, RenameProposal(file_id=media.id, proposed_filename=media.original_filename, status=ProposalStatus.EXECUTED)])
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await decide_local_source(session, decision(media, companion, imported.release_observation_id, kind="release_metadata"), actor="reviewer")
    await session.flush()
    media.file_metadata = metadata
    source = await _get_tracklist_for_file(session, media.id)
    tags = compute_proposed_tags(metadata, source, media.original_filename)
    reviewed = _tag_review_payload(media, source, None, tags)
    token = _encode_tag_review_token(reviewed)
    _, router = install_fake_queues(client)
    # The real Changes Review token crosses JSON/form decoding and the actual write route.
    response = await client.post(f"/tags/{media.id}/write", data={"review_token": token})
    assert response.status_code == 200 and len(router.captures) == 1
    assert router.captures[0][2]["tags"]["artist"] == "Reviewed Artist"
    # A rendered bulk candidate can go stale after initial validation. The durable boundary
    # refuses it before creating its audit row or sending another agent job.
    import pytest
    from sqlalchemy import delete

    from phaze.models.tag_write_log import TagWriteLog

    await session.execute(delete(TagWriteLog).where(TagWriteLog.file_id == media.id))
    companion.sha256_hash = "b" * 64
    await session.flush()
    with pytest.raises(ValueError, match="Selected source binding changed"):
        await enqueue_tag_write(session, router, media, tags, "proposal", review_source_versions=reviewed["sources"])
    assert len(router.captures) == 1


@pytest.mark.parametrize(
    "case",
    [
        "raw",
        "unknown",
        "clock",
        "minute",
        "intrinsic",
        "other_target",
        "original_changed",
        "array",
        "boolean",
        "decimal",
        "zero_denominator",
        "extra",
        "long_evidence",
        "wrong_evidence_type",
        "oversized_integer",
        "empty_evidence",
        "second_fraction",
        "frame_fraction",
        "qualified",
    ],
)
async def test_legacy_exact_boundaries_require_typed_recording_review(session, make_file, case):
    """Actual PG eligibility and actual CUE/record joins agree; raw evidence never authorizes precision."""
    from phaze.routers.record import build_file_record_context
    from phaze.services.cue_review import eligible_tracklist_stmt, gated_tracklist_stmt
    from tests._timing import qualified_timing

    media = await make_file()
    session.add(FileMetadata(file_id=media.id, duration=20))
    session.add(RenameProposal(file_id=media.id, proposed_filename="recording.mp3", status="executed"))
    legacy = Tracklist(external_id=uuid.uuid4().hex, source_url="", file_id=media.id, source="manual", status="approved")
    session.add(legacy)
    await session.flush()
    version = TracklistVersion(tracklist_id=legacy.id, version_number=1)
    session.add(version)
    await session.flush()
    legacy.latest_version_id = version.id
    raw = "12:35"
    evidence = qualified_timing(raw, media.id, seconds=Fraction(76, 75))
    if case == "raw":
        evidence = None
    elif case == "unknown":
        evidence = {"original": raw}
    elif case == "clock":
        evidence.update(kind="clock", offset=None, offset_usability="unusable")
    elif case == "minute":
        evidence.update(precision="minute", offset={"numerator": 60, "denominator": 1}, offset_usability="approximate")
    elif case == "intrinsic":
        evidence["origin"] = "FILE:1:recording.mp3"
    elif case == "other_target":
        evidence["origin"] = f"recording:{uuid.uuid4()}"
    elif case == "original_changed":
        evidence["original"] = "different"
    elif case == "array":
        evidence = []
    elif case == "boolean":
        evidence["offset"]["numerator"] = True
    elif case == "decimal":
        evidence["offset"]["numerator"] = 76.5
    elif case == "zero_denominator":
        evidence["offset"]["denominator"] = 0
    elif case == "extra":
        evidence["unexpected"] = "not domain evidence"
    elif case == "long_evidence":
        evidence["evidence"] = ["x" * 4097]
    elif case == "wrong_evidence_type":
        evidence["evidence"] = [True]
    elif case == "empty_evidence":
        evidence["evidence"] = []
    elif case == "second_fraction":
        evidence["precision"] = "second"
    elif case == "frame_fraction":
        evidence["offset"]["denominator"] = 74
    elif case == "oversized_integer":
        evidence["offset"] = {"numerator": 10**4096, "denominator": 1}
    track = TracklistTrack(version_id=version.id, position=1, title="Retained", timestamp=raw, timestamp_evidence=evidence)
    session.add(track)
    await session.flush()
    original_id = track.id
    eligible = {row[0].id for row in (await session.execute(eligible_tracklist_stmt())).all()}
    gated = {row[0].id for row in (await session.execute(gated_tracklist_stmt())).all()}
    assert (legacy.id in eligible) == (case == "qualified")
    assert (legacy.id in gated) == (case != "qualified")
    cue = await build_cue_tracks_for_file(session, media.id)
    context = await build_file_record_context(media.id, session)
    if case == "qualified":
        assert cue[0].timestamp_seconds == Fraction(76, 75)
        assert "INDEX 01 00:01:01" in generate_cue_content("recording.mp3", "mp3", cue)
        assert context["track_segments"][0].start_sec == float(Fraction(76, 75))
    else:
        assert cue[0].timestamp_seconds is None
        output = generate_cue_content("recording.mp3", "mp3", cue)
        assert "INDEX 01" not in output and "TRACK 01" not in output
        assert not context["track_segments"]
    await session.refresh(track)
    assert track.id == original_id and track.timestamp == raw and track.timestamp_evidence == evidence


async def test_huge_qualified_legacy_offset_cannot_crash_analysis_boundary(session, make_file):
    from tests._timing import qualified_timing

    media = await make_file()
    track = TracklistTrack(
        version_id=uuid.uuid4(), position=1, timestamp="12:35", timestamp_evidence=qualified_timing("12:35", media.id, seconds=Fraction(10**400))
    )
    assert not build_track_segments([track], [], 20, media_id=media.id)


def test_huge_qualified_provider_offset_cannot_crash_analysis_boundary():
    from phaze.tracklist_providers.domain import ProviderTrack, Timestamp
    from tests._timing import qualified_timing

    media_id = uuid.uuid4()
    track = ProviderTrack(position=1, timestamp=Timestamp.model_validate(qualified_timing("00:01", media_id, seconds=Fraction(10**400))))
    assert not build_track_segments([track], [], 20)


async def test_real_selected_workspace_routes_and_match_all_retry(session, client, monkeypatch):
    """Real imported decisions drive served workspace rows and exhaustive controller enqueue pins."""
    from phaze.services.pipeline.tracklists import get_match_pending_recording_sources
    from tests._background_drain import drain_router_background_tasks

    media, companion, raw = await inventory(session, CUE, "cue")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    pending = await client.get("/pipeline/local-source-sets?kind=tracklist&page=bad&page_size=bad")
    assert pending.status_code == 200 and "pending candidate" in pending.text
    fallback = await client.get("/pipeline/local-source-sets?kind=unsupported")
    assert fallback.status_code == 200 and "Stored source" in fallback.text
    inventory_response = await client.get("/pipeline/local-source-sets?kind=cue&inventory=1")
    assert inventory_response.status_code == 200 and companion.original_filename in inventory_response.text
    assert "Inventoried companion" in inventory_response.text
    assert not await get_match_pending_recording_sources(session)
    await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id, ordinal=1), actor="reviewer")
    selection = await session.get(ProviderRecordingSelection, (media.id, "tracklist"))
    selection.selection_token = None  # retained historical selection, resolved by the real generic port
    await session.flush()
    pins = await get_match_pending_recording_sources(session)
    assert len(pins) == 1 and pins[0]["expected_observation_id"] == str(imported.tracklist_observation_id)
    assert pins[0]["expected_selection_token"]
    reviewed = await client.get("/pipeline/local-source-sets?kind=tracklist")
    assert "Selected authority" in reviewed.text and "reviewer" in reviewed.text
    queue, _ = install_fake_queues(client)
    original_enqueue = queue.enqueue

    async def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic broker unavailable")

    monkeypatch.setattr(queue, "enqueue", unavailable)
    first = await client.post("/pipeline/match-tracklists")
    await drain_router_background_tasks()
    assert first.status_code == 200 and not queue.captured
    assert await get_match_pending_recording_sources(session) == pins
    monkeypatch.setattr(queue, "enqueue", original_enqueue)
    retry = await client.post("/pipeline/match-tracklists")
    await drain_router_background_tasks()
    assert retry.status_code == 200
    assert len(queue.captured) == 1
    assert queue.captured == [("match_recording_source_to_discogs", pins[0])]
