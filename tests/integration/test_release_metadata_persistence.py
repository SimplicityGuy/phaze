"""Release parsing composes with capture/storage without replacing reviewed data."""

from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import ProviderRecordingSelection, ProviderSourceObservation
from phaze.models.tracklist import Tracklist
from phaze.services.provider_persistence import add_candidate, select_observation, store_snapshot, store_source_read
from phaze.tracklist_providers.domain import Snapshot, SourceIdentity, SourceRead
from phaze.tracklist_providers.local_release import RELEASE_PARSER_VERSION, extract_release_metadata


def snapshot(identity: SourceIdentity, content: SourceRead, *, version: str = RELEASE_PARSER_VERSION) -> Snapshot:
    extracted = extract_release_metadata(content, source_format="nfo")
    return Snapshot(
        identity=identity,
        revision=content.revision,
        revision_scope=content.revision_scope,
        retrieved_at=content.retrieved_at,
        text=content.text,
        encoding=content.encoding,
        source_format="nfo",
        parser_version=version,
        release_facts=extracted.facts,
        completeness=extracted.completeness,
        provenance=content.evidence,
    )


async def test_captured_release_facts_idempotence_conflicts_and_review_preservation(session: AsyncSession) -> None:
    media = FileRecord(
        id=uuid.uuid4(),
        agent_id="test-fileserver",
        sha256_hash="a" * 64,
        original_path="/synthetic/set.mp3",
        current_path="/synthetic/set.mp3",
        original_filename="set.mp3",
        file_type="mp3",
        file_size=40,
    )
    legacy = Tracklist(file_id=media.id, source="manual", external_id="manual-synthetic", source_url="", artist="Reviewed Artist")
    session.add(media)
    await session.flush()
    session.add(legacy)
    tags = FileMetadata(file_id=media.id, artist="Embedded Artist", bitrate=128000, raw_tags={"artist": "Embedded Artist"})
    session.add(tags)
    await session.flush()
    identity = SourceIdentity(provider_id="local", native_id="release-synthetic")
    content = SourceRead(
        status="found",
        code="captured",
        scope=identity.native_id,
        text="Artist: Original Artist\nDate: 03/04/2024\nLabel: First\nLabel: Second\nBitrate: 320 kbps\n",
        encoding="cp437",
        revision="full-token",
        revision_scope="full",
        retrieved_at=datetime.now(UTC),
    )
    raw = await store_source_read(session, identity, content, parser_version="source-read-v1")
    parsed = await store_snapshot(session, snapshot(identity, content), parent_id=raw.observation.id)
    assert parsed.observation.parent_id == raw.observation.id and not parsed.conflict
    assert parsed.observation.payload["release_facts"][1]["certainty"] == "unknown"
    assert parsed.observation.payload["release_facts"][2]["certainty"] == "conflicting"
    assert parsed.observation.payload["release_facts"][4]["normalized_value"] == "320000"
    assert parsed.observation.decoded_text == raw.observation.decoded_text == content.text
    await add_candidate(session, media.id, parsed.observation.id)
    selected = await select_observation(session, media.id, parsed.observation.id, kind="release_metadata", actor="reviewer")
    repeat = content.model_copy(update={"retrieved_at": datetime.now(UTC) + timedelta(days=1)})
    duplicate = await store_snapshot(session, snapshot(identity, repeat), parent_id=raw.observation.id)
    assert duplicate.reused and duplicate.observation.id == parsed.observation.id
    reinterpreted = await store_snapshot(session, snapshot(identity, content, version="local-release-v2"), parent_id=parsed.observation.id)
    assert not reinterpreted.conflict and reinterpreted.observation.parent_id == parsed.observation.id
    changed = content.model_copy(update={"text": "Artist: Changed\n"})
    conflict = await store_snapshot(session, snapshot(identity, changed))
    assert conflict.conflict and conflict.observation.id != parsed.observation.id
    await session.refresh(selected)
    await session.refresh(legacy)
    await session.refresh(tags)
    assert selected.observation_id == parsed.observation.id and selected.actor == "reviewer"
    assert legacy.artist == "Reviewed Artist" and legacy.provider_object_id is None
    assert tags.artist == "Embedded Artist" and tags.bitrate == 128000 and tags.raw_tags == {"artist": "Embedded Artist"}
    assert (
        await session.scalar(
            select(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media.id, ProviderRecordingSelection.kind == "tracklist")
        )
        is None
    )


async def test_unrecognized_and_partial_text_remain_stored_without_fabricated_facts(session: AsyncSession) -> None:
    for index, content in enumerate(
        [
            SourceRead(
                status="found",
                code="captured",
                scope="source",
                text="Meaningful unrecognized prose\nWebsite: example.invalid\n",
                retrieved_at=datetime.now(UTC),
            ),
            SourceRead(
                status="incomplete",
                code="byte_cap",
                scope="source",
                text="Label: Complete\nArtist: Par",
                truncated=True,
                revision_scope="bounded",
                retrieved_at=datetime.now(UTC),
            ),
        ]
    ):
        identity = SourceIdentity(provider_id="local", native_id=f"release-notes-{index}")
        raw = await store_source_read(session, identity, content, parser_version="source-read-v1")
        parsed = await store_snapshot(session, snapshot(identity, content), parent_id=raw.observation.id)
        assert parsed.observation.decoded_text == content.text and raw.observation.truncated == content.truncated
        assert not parsed.observation.truncated  # Parse completeness is independent of raw read truncation.
        assert parsed.observation.payload["tracks"] == []
        if index == 0:
            assert parsed.observation.payload["release_facts"] == [] and parsed.observation.status == "found"
        else:
            assert len(parsed.observation.payload["release_facts"]) == 1 and parsed.observation.status == "incomplete"
        reloaded = await session.scalar(
            select(ProviderSourceObservation).where(ProviderSourceObservation.id == parsed.observation.id).execution_options(populate_existing=True)
        )
        assert reloaded is not None and reloaded.payload == parsed.observation.payload


async def test_complete_text_with_bounded_revision_preserves_last_fact_and_revision(session: AsyncSession) -> None:
    identity = SourceIdentity(provider_id="local", native_id="bounded-revision-complete-text")
    content = SourceRead(
        status="found",
        code="captured",
        scope="source",
        text="Artist: Full\nLabel: Complete",
        revision="head-digest",
        revision_scope="bounded",
        retrieved_at=datetime.now(UTC),
    )
    parsed_snapshot = snapshot(identity, content)
    assert parsed_snapshot.completeness.state == "complete" and parsed_snapshot.release_facts[-1].value == "Complete"
    stored = await store_snapshot(session, parsed_snapshot)
    assert stored.observation.revision == "head-digest" and stored.observation.revision_scope == "bounded"
    assert stored.observation.payload["release_facts"][-1]["original_value"] == "Complete"
