"""Executable capture → candidate → explicit decision → pinned selected-source seam."""

from datetime import UTC, datetime
import hashlib
import uuid

from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.main import create_app
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import ProviderRecordingCandidate, ProviderSelectionEvent, ProviderSourceObservation
from phaze.models.tracklist import Tracklist
from phaze.schemas.agent_companion_capture import CaptureReport, CaptureTarget
from phaze.schemas.local_source_import import ImportLocalSource, SourceDecision
from phaze.services.companion_capture import store_capture_report
from phaze.services.local_source_import import decide_local_source, get_selected_recording_source, import_local_source, selection_token
from phaze.services.provider_persistence import select_observation
from phaze.tracklist_providers.domain import LoadBudget, SourceRead


TEXT = "01. Artist - First\n02. Other - Second\n"
CUE = 'PERFORMER "Set Artist"\nTITLE "Set"\nFILE "one.mp3" MP3\n TRACK 01 AUDIO\n TITLE "First"\n INDEX 01 00:01:01\nFILE "two.mp3" MP3\n TRACK 01 AUDIO\n TITLE "Second"\n INDEX 01 00:02:02\n'


async def inventory(
    session: AsyncSession, text: str = TEXT, extension: str = "txt", *, agent_id: str = "test-fileserver"
) -> tuple[FileRecord, FileRecord, uuid.UUID]:
    def file(name, kind, digest):
        return FileRecord(
            id=uuid.uuid4(),
            agent_id=agent_id,
            original_path="/synthetic/" + name,
            current_path="/synthetic/" + name,
            original_filename=name,
            file_type=kind,
            sha256_hash=digest,
            file_size=len(text.encode()),
        )

    media = file("recording.mp3", "mp3", "a" * 64)
    companion = file("notes." + extension, extension, hashlib.sha256(text.encode()).hexdigest())
    session.add_all([media, companion])
    await session.flush()
    session.add(FileCompanion(companion_id=companion.id, media_id=media.id))
    await session.flush()
    report = CaptureReport(
        target=CaptureTarget(
            file_id=companion.id,
            media_id=media.id,
            path=companion.current_path,
            expected_sha256=companion.sha256_hash,
            expected_size=companion.file_size,
        ),
        read=SourceRead(
            status="found",
            code="read",
            scope=str(companion.id),
            text=text,
            encoding="utf-8",
            revision=companion.sha256_hash,
            revision_scope="full",
            bytes_read=companion.file_size,
            evidence=("revision:sha256:full",),
            retrieved_at=datetime.now(UTC),
        ),
    )
    capture = await store_capture_report(session, media.agent_id, report)
    return media, companion, capture.observation_id


def decision(media, companion, observation, *, token=None, kind="tracklist", ordinal=None, action="select", identifier=None, revision=None):
    return SourceDecision(
        media_id=media.id,
        observation_id=observation,
        kind=kind,
        decision_id=identifier or uuid.uuid4(),
        expected_selection_token=token,
        expected_revision=revision or companion.sha256_hash,
        expected_source_sha256=companion.sha256_hash,
        expected_media_sha256=media.sha256_hash,
        cue_file_ordinal=ordinal,
        action=action,
    )


async def test_capture_import_select_reimport_preserves_legacy_and_review(session):
    media, companion, raw = await inventory(session)
    legacy = Tracklist(file_id=media.id, source="manual", external_id="synthetic-manual", source_url="", artist="Reviewed")
    session.add(legacy)
    first = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert first.tracklist_status == "found" and first.release_status == "found"
    assert await get_selected_recording_source(session, media.id) is None
    parsed = await session.get(ProviderSourceObservation, first.tracklist_observation_id)
    assert parsed.parent_id == raw
    command = decision(media, companion, parsed.id)
    selected = await decide_local_source(session, command, actor="reviewer")
    assert selected.snapshot.text == TEXT and selected.tracks[1].title == "Second"
    assert selected.selection_token == command.decision_id
    duplicate = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert duplicate == first
    replay = await decide_local_source(session, command, actor="reviewer")
    assert replay.selection_token == command.decision_id
    assert await session.scalar(select(func.count()).select_from(ProviderSelectionEvent)) == 1
    assert legacy.artist == "Reviewed" and legacy.provider_object_id is None
    with pytest.raises(ValueError, match="different intent"):
        await decide_local_source(session, command, actor="other")
    with pytest.raises(ValueError, match="changed; refresh"):
        await decide_local_source(session, decision(media, companion, parsed.id), actor="reviewer")


async def test_release_only_and_partial_never_grant_track_authority(session):
    media, companion, raw = await inventory(session, "Artist: Release Artist\nDate: 2024-01-01\n")
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert result.tracklist_status == "incomplete" and result.release_status == "found"
    with pytest.raises(ValueError, match="kind does not match"):
        await decide_local_source(session, decision(media, companion, result.release_observation_id), actor="reviewer")
    selected = await decide_local_source(
        session, decision(media, companion, result.release_observation_id, kind="release_metadata"), actor="reviewer"
    )
    assert selected.snapshot.release_facts[0].value == "Release Artist"
    assert await get_selected_recording_source(session, media.id) is None
    partial = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw, budget=LoadBudget(max_characters=10)))
    assert partial.tracklist_status == partial.release_status == "incomplete"
    with pytest.raises(ValueError, match="complete parsed"):
        await decide_local_source(
            session,
            decision(media, companion, partial.release_observation_id, kind="release_metadata", token=selected.selection_token),
            actor="reviewer",
        )
    assert (await get_selected_recording_source(session, media.id, kind="release_metadata")).selection_token == selected.selection_token


async def test_cue_one_source_distinct_targets_preserves_intrinsic_snapshot(session):
    media, companion, raw = await inventory(session, CUE, "cue")
    target = FileRecord(
        id=uuid.uuid4(),
        agent_id=media.agent_id,
        original_path="/synthetic/second.mp3",
        current_path="/synthetic/second.mp3",
        original_filename="second.mp3",
        file_type="mp3",
        sha256_hash="b" * 64,
        file_size=20,
    )
    session.add(target)
    await session.flush()
    session.add(FileCompanion(companion_id=companion.id, media_id=target.id))
    await session.flush()
    first = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    second = await import_local_source(session, ImportLocalSource(media_id=target.id, raw_observation_id=raw))
    assert first.tracklist_observation_id == second.tracklist_observation_id
    with pytest.raises(ValueError, match="explicit intrinsic"):
        await decide_local_source(session, decision(media, companion, first.tracklist_observation_id), actor="reviewer")
    with pytest.raises(ValueError, match="explicit intrinsic"):
        await decide_local_source(session, decision(media, companion, first.tracklist_observation_id, ordinal=3), actor="reviewer")
    selected1 = await decide_local_source(session, decision(media, companion, first.tracklist_observation_id, ordinal=1), actor="reviewer")
    selected2 = await decide_local_source(session, decision(target, companion, second.tracklist_observation_id, ordinal=2), actor="reviewer")
    assert [track.title for track in selected1.tracks] == ["First"]
    assert [track.title for track in selected2.tracks] == ["Second"]
    assert selected1.tracks[0].timestamp.offset.numerator == 76
    assert selected1.tracks[0].timestamp.origin == f"recording:{media.id}"
    assert selected1.snapshot.tracks[0].timestamp.origin.startswith("cue:")
    assert len(selected1.snapshot.tracks) == 2
    assert await session.scalar(select(func.count()).select_from(Tracklist)) == 0


async def test_embedded_same_provider_channel_digest_and_stale_tag_guard(session):
    media, _companion, _raw = await inventory(session)
    tags = FileMetadata(file_id=media.id, artist="Original", raw_tags={"comments": TEXT})
    session.add(tags)
    await session.flush()
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, embedded_channel="comments"))
    observation = await session.get(ProviderSourceObservation, result.tracklist_observation_id)
    assert observation.revision == hashlib.sha256(TEXT.encode()).hexdigest() != media.sha256_hash
    command = decision(media, media, observation.id, revision=observation.revision)
    selected = await decide_local_source(session, command, actor="reviewer")
    assert selected.identity.native_id == f"embedded:{media.id}:comments"
    assert selected.snapshot.source_format == "comments"
    tags.raw_tags = {"comments": "01. New - Title\n"}
    await session.flush()
    with pytest.raises(ValueError, match="Embedded channel changed"):
        await decide_local_source(session, command, actor="reviewer")
    assert (await get_selected_recording_source(session, media.id)).observation_id == observation.id
    assert tags.artist == "Original"


async def test_reviewed_history_readable_after_relink_missing_and_stale(session):
    media, companion, raw = await inventory(session)
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    command = decision(media, companion, result.tracklist_observation_id)
    await decide_local_source(session, command, actor="reviewer")
    companion.sha256_hash = "c" * 64
    await session.flush()
    assert (await get_selected_recording_source(session, media.id)).availability == "stale"
    with pytest.raises(ValueError, match="Capture lacks current"):
        await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    with pytest.raises(ValueError, match="Inventory revision changed"):
        await decide_local_source(session, command, actor="reviewer")
    companion.sha256_hash = command.expected_source_sha256
    await session.execute(delete(FileCompanion).where(FileCompanion.companion_id == companion.id))
    assert (await get_selected_recording_source(session, media.id)).availability == "unlinked"
    with pytest.raises(ValueError, match="no longer linked"):
        await decide_local_source(session, command, actor="reviewer")
    companion.missing_at = datetime.now(UTC)
    await session.flush()
    historical = await get_selected_recording_source(session, media.id)
    assert historical.availability == "missing" and historical.tracks[0].title == "First"


async def test_reject_pending_is_audited_and_does_not_clear_selected(session):
    media, companion, raw = await inventory(session)
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    first = await decide_local_source(session, decision(media, companion, result.tracklist_observation_id), actor="reviewer")
    reject = decision(media, companion, result.release_observation_id, kind="release_metadata", action="reject")
    assert await decide_local_source(session, reject, actor="reviewer") is None
    assert await decide_local_source(session, reject, actor="reviewer") is None
    row = await session.scalar(select(ProviderRecordingCandidate).where(ProviderRecordingCandidate.observation_id == result.release_observation_id))
    assert row.status == "rejected"
    assert (await get_selected_recording_source(session, media.id)).selection_token == first.selection_token
    with pytest.raises(ValueError, match="active selected"):
        await decide_local_source(
            session, decision(media, companion, result.tracklist_observation_id, action="reject", token=first.selection_token), actor="reviewer"
        )
    with pytest.raises(ValueError, match="complete parsed"):
        await decide_local_source(session, decision(media, companion, result.release_observation_id, kind="release_metadata"), actor="reviewer")


async def test_legacy_choice_token_preserved_until_explicit_replacement(session):
    media, companion, raw = await inventory(session)
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    legacy = await select_observation(session, media.id, result.tracklist_observation_id, kind="tracklist", actor="manual")
    token = selection_token(legacy)
    assert legacy.selection_token is None
    await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert (await get_selected_recording_source(session, media.id)).selection_token == token
    assert legacy.selection_token is None
    chosen = await decide_local_source(session, decision(media, companion, result.tracklist_observation_id, token=token), actor="reviewer")
    assert chosen.selection_token != token


@pytest.mark.parametrize("values", [{}, {"raw_observation_id": uuid.uuid4(), "embedded_channel": "comments"}])
def test_import_xor_is_validated_before_database(values):
    with pytest.raises(ValidationError, match="exactly one"):
        ImportLocalSource(media_id=uuid.uuid4(), **values)


async def test_actual_routes_import_select_read_and_validation(session):
    media, companion, raw = await inventory(session)
    app = create_app()

    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        malformed = await client.post("/api/local-sources/import", json={"media_id": str(media.id)})
        assert malformed.status_code == 422
        payload = ImportLocalSource(media_id=media.id, raw_observation_id=raw).model_dump(mode="json")
        result = await client.post("/api/local-sources/import", json=payload)
        assert result.status_code == 200
        observation = uuid.UUID(result.json()["tracklist_observation_id"])
        chosen = await client.post("/api/local-sources/decision", json=decision(media, companion, observation).model_dump(mode="json"))
        assert chosen.status_code == 200 and chosen.json()["actor"] == "admin"
        read = await client.get(f"/api/local-sources/recordings/{media.id}/selected")
        assert read.status_code == 200 and read.json()["observation_id"] == str(observation)
        repeat = await client.post("/api/local-sources/reimport", json=payload)
        assert repeat.status_code == 200
        rejected = await client.post(
            "/api/local-sources/import", json=ImportLocalSource(media_id=media.id, raw_observation_id=uuid.uuid4()).model_dump(mode="json")
        )
        assert rejected.status_code == 409
        stale = await client.post("/api/local-sources/decision", json=decision(media, companion, observation).model_dump(mode="json"))
        assert stale.status_code == 409


async def test_selected_cue_target_revision_changed_retains_history_without_exact_boundaries(session):
    media, companion, raw = await inventory(session, CUE, "cue")
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    chosen = await decide_local_source(session, decision(media, companion, result.tracklist_observation_id, ordinal=1), actor="reviewer")
    media.sha256_hash = "f" * 64
    await session.flush()
    stale = await get_selected_recording_source(session, media.id)
    assert stale.observation_id == chosen.observation_id and stale.availability == "stale"
    assert stale.tracks[0].timestamp.offset_usability == "unusable"
    assert stale.tracks[0].timestamp.origin == stale.snapshot.tracks[0].timestamp.origin
    assert stale.snapshot.tracks[0].timestamp.offset_usability == "qualified"
    media.missing_at = datetime.now(UTC)
    await session.flush()
    assert (await get_selected_recording_source(session, media.id)).availability == "missing"


@pytest.mark.parametrize("key", ["COMMENT", "Comments", "DESCRIPTION", "lyrics", "TrackList"])
async def test_exact_case_preserved_embedded_key_is_independent_source(session, key):
    media, _companion, _raw = await inventory(session)
    session.add(FileMetadata(file_id=media.id, raw_tags={key: TEXT, "comments": "01. Alternate - Choice\n"}))
    await session.flush()
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, embedded_channel=key))
    observation = await session.get(ProviderSourceObservation, result.tracklist_observation_id)
    chosen = await decide_local_source(session, decision(media, media, observation.id, revision=observation.revision), actor="reviewer")
    assert chosen.identity.native_id.endswith(":" + key)
    assert chosen.tracks[0].title == "First"


async def test_empty_release_notes_visible_but_cannot_replace_reviewed_metadata(session):
    media, companion, raw = await inventory(session, "An unstructured release note.\n")
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert result.release_status == "found"
    observation = await session.get(ProviderSourceObservation, result.release_observation_id)
    assert observation.decoded_text == "An unstructured release note.\n"
    with pytest.raises(ValueError, match="without structured facts"):
        await decide_local_source(session, decision(media, companion, observation.id, kind="release_metadata"), actor="reviewer")


async def test_embedded_changed_channel_read_is_stale_and_oversized_lists_refused_before_join(session):
    media, _companion, _raw = await inventory(session)
    metadata = FileMetadata(file_id=media.id, raw_tags={"comments": [TEXT]})
    session.add(metadata)
    await session.flush()
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, embedded_channel="comments"))
    observation = await session.get(ProviderSourceObservation, result.tracklist_observation_id)
    await decide_local_source(session, decision(media, media, observation.id, revision=observation.revision), actor="reviewer")
    metadata.raw_tags = {"comments": ["x" * 131072, "y" * 131072]}
    await session.flush()
    stale = await get_selected_recording_source(session, media.id)
    assert stale.availability == "stale" and stale.snapshot.text == TEXT
    with pytest.raises(ValueError, match="exceeds stored source bounds"):
        await import_local_source(session, ImportLocalSource(media_id=media.id, embedded_channel="comments"))


async def test_legacy_storage_helper_clears_old_mapping_and_replay_cannot_resurrect(session):
    media, companion, raw = await inventory(session)
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    command = decision(media, companion, result.tracklist_observation_id)
    await decide_local_source(session, command, actor="reviewer")
    legacy = await select_observation(session, media.id, result.tracklist_observation_id, kind="tracklist", actor="legacy-reviewer")
    assert legacy.selection_token is None and legacy.target_mapping is None
    assert (await get_selected_recording_source(session, media.id)).availability == "unknown_binding"
    with pytest.raises(ValueError, match="no longer current"):
        await decide_local_source(session, command, actor="reviewer")


async def test_association_unknown_history_until_actual_derivation_and_dry_run(session):
    from phaze.services.companion import associate_companions
    from tests.discovery.services.test_companion import _features

    media, companion, _raw = await inventory(session)
    link = await session.scalar(select(FileCompanion).where(FileCompanion.companion_id == companion.id))
    assert link.derivation_method is None
    await associate_companions(session, agent_id=media.agent_id)
    await session.refresh(link)
    assert link.derivation_method is None
    session.add(_features(companion, media.original_filename, is_tracklist=True))
    await session.flush()
    await associate_companions(session, agent_id=media.agent_id, apply=False)
    await session.refresh(link)
    assert link.derivation_method is None
    await associate_companions(session, agent_id=media.agent_id)
    await session.refresh(link)
    assert link.derivation_method == "reference"
    assert link.derivation_revision == companion.sha256_hash
    assert link.derivation_evidence[0] == "linking-chain:reference"
    assert link.derived_at is not None


@pytest.mark.parametrize("actor", ["", "x" * 129, "bad\0actor"])
async def test_decision_actor_bound_rejected_before_storage(session, actor):
    media, companion, raw = await inventory(session)
    with pytest.raises(ValueError, match="bounded actor"):
        await decide_local_source(session, decision(media, companion, raw), actor=actor)


@pytest.mark.parametrize("state", ["missing-metadata", "missing-channel", "wrong-type", "missing-file"])
async def test_invalid_embedded_inventory_has_no_candidates(session, state):
    media, _companion, _raw = await inventory(session)
    if state != "missing-metadata":
        session.add(FileMetadata(file_id=media.id, raw_tags={"comments": TEXT} if state != "missing-channel" else {}))
        await session.flush()
    if state == "wrong-type":
        media.file_type = "txt"
        await session.flush()
    requested = uuid.uuid4() if state == "missing-file" else media.id
    with pytest.raises(ValueError):
        await import_local_source(session, ImportLocalSource(media_id=requested, embedded_channel="comments"))
    assert await session.scalar(select(func.count()).select_from(ProviderRecordingCandidate)) == 0


async def test_decision_observation_candidate_revision_and_mapping_guards(session):
    media, companion, raw = await inventory(session)
    with pytest.raises(ValueError, match="retained local source"):
        await decide_local_source(session, decision(media, companion, uuid.uuid4()), actor="reviewer")
    with pytest.raises(ValueError, match="not a candidate"):
        await decide_local_source(session, decision(media, companion, raw), actor="reviewer")
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    with pytest.raises(ValueError, match="revision or availability"):
        await decide_local_source(session, decision(media, companion, result.tracklist_observation_id, revision="changed"), actor="reviewer")
    with pytest.raises(ValueError, match="only valid for CUE"):
        await decide_local_source(session, decision(media, companion, result.tracklist_observation_id, ordinal=1), actor="reviewer")
    with pytest.raises(ValueError, match="without track mapping"):
        await decide_local_source(
            session, decision(media, companion, result.release_observation_id, kind="release_metadata", ordinal=1), actor="reviewer"
        )
    companion.missing_at = datetime.now(UTC)
    await session.flush()
    with pytest.raises(ValueError, match="unavailable"):
        await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))


async def test_failed_read_candidate_reject_is_typed_and_does_not_clear_reviewed_selection(session):
    media, companion, raw = await inventory(session)
    first = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    selected = await decide_local_source(session, decision(media, companion, first.tracklist_observation_id), actor="reviewer")
    report = CaptureReport(
        target=CaptureTarget(
            file_id=companion.id, path=companion.current_path, expected_sha256=companion.sha256_hash, expected_size=companion.file_size
        ),
        read=SourceRead(status="unavailable", code="offline", scope=str(companion.id), retrieved_at=datetime.now(UTC)),
    )
    failed = await store_capture_report(session, media.agent_id, report)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=failed.observation_id))
    assert imported.tracklist_status == imported.release_status == "unavailable"
    assert imported.tracklist_observation_id is None and imported.release_observation_id is None
    rejected = decision(media, companion, failed.observation_id, token=selected.selection_token, action="reject").model_copy(
        update={"expected_revision": None}
    )
    assert (await decide_local_source(session, rejected, actor="reviewer")).selection_token == selected.selection_token
    assert (await get_selected_recording_source(session, media.id)).observation_id == first.tracklist_observation_id


@pytest.mark.parametrize("budget", [LoadBudget(max_bytes=20), LoadBudget(max_lines=1)])
async def test_negotiated_partial_parse_retains_rows_but_is_not_selectable(session, budget):
    media, companion, raw = await inventory(session)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw, budget=budget))
    assert imported.tracklist_status == "incomplete"
    parsed = await session.get(ProviderSourceObservation, imported.tracklist_observation_id)
    assert parsed.payload["completeness"]["state"] == "incomplete"
    with pytest.raises(ValueError, match="complete parsed"):
        await decide_local_source(session, decision(media, companion, parsed.id), actor="reviewer")
    parent = await session.get(ProviderSourceObservation, raw)
    assert parent.decoded_text == TEXT


async def test_conflicting_same_revision_requires_explicit_ack_and_old_decision_replay_refused(session):
    media, companion, raw = await inventory(session)
    first = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    initial_command = decision(media, companion, first.tracklist_observation_id)
    chosen = await decide_local_source(session, initial_command, actor="reviewer")
    original = await session.get(ProviderSourceObservation, raw)
    from phaze.services.provider_persistence import store_source_read
    from phaze.tracklist_providers.domain import SourceIdentity

    # Contradictory agent evidence at the same full revision is retained as a conflict.
    changed = SourceRead.model_validate({**original.payload, "text": "01. Artist - Changed\n", "retrieved_at": datetime.now(UTC)})
    stored = await store_source_read(
        session,
        SourceIdentity(provider_id="local", native_id=f"companion:{companion.id}"),
        changed,
        parser_version="source-read-v1",
        source_file_id=companion.id,
        channel="companion",
    )
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=stored.observation.id))
    replacement = decision(media, companion, result.tracklist_observation_id, token=chosen.selection_token)
    with pytest.raises(ValueError, match="acknowledgement"):
        await decide_local_source(session, replacement, actor="reviewer")
    chosen2 = await decide_local_source(session, replacement.model_copy(update={"accept_conflict": True}), actor="reviewer")
    assert chosen2.tracks[0].title == "Changed"
    with pytest.raises(ValueError, match="no longer current"):
        await decide_local_source(session, initial_command, actor="reviewer")


async def test_untimed_cue_rows_keep_explicit_file_mapping_without_invented_offset(session):
    text = 'FILE "one.mp3" MP3\n TRACK 01 AUDIO\n TITLE "First"\n'
    media, companion, raw = await inventory(session, text, "cue")
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert result.tracklist_status == "found"
    chosen = await decide_local_source(session, decision(media, companion, result.tracklist_observation_id, ordinal=1), actor="reviewer")
    assert chosen.tracks[0].timestamp is None
    assert "FILE:1:one.mp3" in chosen.tracks[0].evidence


async def test_selected_read_target_type_owner_and_source_revision_checks(session):
    from phaze.models.agent import Agent

    media, companion, raw = await inventory(session)
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await decide_local_source(session, decision(media, companion, result.tracklist_observation_id), actor="reviewer")
    media.file_type = "txt"
    await session.flush()
    assert (await get_selected_recording_source(session, media.id)).availability == "unavailable"
    media.file_type = "mp3"
    session.add(Agent(id="other-synthetic", name="other-synthetic", kind="fileserver", scan_roots=[]))
    await session.flush()
    media.agent_id = "other-synthetic"
    await session.flush()
    assert (await get_selected_recording_source(session, media.id)).availability == "unlinked"
    with pytest.raises(ValueError, match="same owning agent"):
        await decide_local_source(session, decision(media, companion, result.tracklist_observation_id), actor="reviewer")


async def test_release_serialized_expansion_retains_bounded_incomplete_prefix(session):
    media, _companion, _raw = await inventory(session)
    content = ("Artist: " + "\U0001f600" * 4080 + "\n") * 64
    session.add(FileMetadata(file_id=media.id, raw_tags={"description": content}))
    await session.flush()
    result = await import_local_source(
        session,
        ImportLocalSource(
            media_id=media.id, embedded_channel="description", budget=LoadBudget(max_bytes=1048576, max_characters=262144, max_lines=16384)
        ),
    )
    assert result.release_status == "incomplete"
    observation = await session.get(ProviderSourceObservation, result.release_observation_id)
    assert observation.decoded_text == content
    assert "limit:release_snapshot_output" in observation.payload["completeness"]["evidence"]
    assert 0 < len(observation.payload["release_facts"]) < 64
    assert len(__import__("json").dumps(observation.payload, ensure_ascii=False, separators=(",", ":")).encode()) < 4194304
