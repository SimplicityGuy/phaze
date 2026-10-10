"""Real bounded stored detail projections and exact historical membership."""

from datetime import UTC, datetime, timedelta
import re
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import delete, event, update
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.provider_source import ProviderRecordingSelection, ProviderSelectionEvent, ProviderSourceObservation
from phaze.schemas.companion_details import CompanionAttemptSummary, DetailPage, StoredTextRequest
from phaze.services.companion_details import (
    get_companion_details,
    get_companion_media,
    get_source_observations,
    get_stored_observation,
    get_stored_text,
)
from phaze.services.provider_persistence import add_candidate, select_observation, store_snapshot, store_source_read
from phaze.tracklist_providers.domain import Completeness, ProviderTrack, ReleaseFact, Snapshot, SourceIdentity, SourceRead


def file(name: str, kind: str = "mp3", agent: str = "test-fileserver") -> FileRecord:
    return FileRecord(
        id=uuid.uuid4(),
        agent_id=agent,
        sha256_hash="a" * 64,
        original_path=f"/synthetic/{name}",
        current_path=f"/synthetic/{name}",
        original_filename=name,
        file_type=kind,
        file_size=40,
    )


async def source(
    session: AsyncSession, media: FileRecord, companion: FileRecord, *, native: str = "source", text: str = "Original notes", revision: str = "a" * 64
) -> ProviderSourceObservation:
    snapshot = Snapshot(
        identity=SourceIdentity(provider_id="local", native_id=native),
        revision=revision,
        revision_scope="full",
        retrieved_at=datetime.now(UTC),
        text=text,
        encoding="utf-8",
        source_format=companion.file_type,
        parser_version="local-test-v1",
        tracks=tuple(ProviderTrack(position=i + 1, artist="Example", title=f"Track {i + 1}") for i in range(4)),
        release_facts=tuple(
            ReleaseFact(
                field="label",
                value=f"Label {i}",
                original_value=f"LABEL {i}",
                normalized_value=f"Label {i}",
                certainty="conflicting",
                source_line=i + 1,
            )
            for i in range(3)
        ),
        completeness=Completeness(state="complete", reason="synthetic", evidence=("line:5:unrecognized",)),
        provenance=("revision:sha256:full",),
    )
    stored = await store_snapshot(session, snapshot, source_file_id=companion.id, channel="companion")
    await add_candidate(session, media.id, stored.observation.id)
    return stored.observation


async def test_inventory_unparsed_sources_history_and_bounded_page(session: AsyncSession) -> None:
    media = file("set.mp3")
    companions = [file(f"notes-{i}.nfo", "nfo") for i in range(4)]
    session.add_all([media, *companions])
    await session.flush()
    session.add_all(
        [
            FileCompanion(
                media_id=media.id,
                companion_id=c.id,
                derivation_method="reference" if i == 0 else None,
                derivation_evidence=["synthetic"] if i == 0 else None,
            )
            for i, c in enumerate(companions)
        ]
    )
    await session.flush()
    observation = await source(session, media, companions[0], text="x" * 5000)
    summary = await get_companion_details(session, media.id, link_page=DetailPage(limit=2))
    assert summary is not None and len(summary.links) == 2 and summary.next_link_offset == 2
    all_links = await get_companion_details(session, media.id)
    assert all_links is not None and len(all_links.links) == 4
    assert {link.derivation_method for link in all_links.links} == {"reference", "historical_unknown"}
    content = all_links.sources[0].latest_stored_content
    assert content is not None and len(content.text_preview) == 512 and content.text_length == 5000
    assert content.track_count == 4 and content.release_fact_count == 3 and all_links.sources[0].attempt_history == "unknown"
    detail = await get_stored_observation(session, media.id, observation.id, track_page=DetailPage(limit=2), fact_page=DetailPage(limit=2))
    assert detail is not None and len(detail.tracks) == 2 and detail.next_track_offset == 2 and detail.next_fact_offset == 2
    assert detail.release_facts[0].original_value == "LABEL 0" and detail.release_facts[0].certainty == "conflicting"
    assert "line:5:unrecognized" in detail.evidence
    tail = await get_stored_text(session, media.id, observation.id, chunk=StoredTextRequest(offset=4995, length=10))
    assert tail is not None and tail.text == "xxxxx" and tail.next_offset is None
    own = await get_stored_text(session, companions[0].id, observation.id)
    assert own is not None and own.text is not None
    assert await get_source_observations(session, media.id, uuid.uuid4()) is None
    assert await get_companion_details(session, uuid.uuid4()) is None


async def test_selection_pinned_and_inventory_states_never_choose_new_candidate(session: AsyncSession) -> None:
    media, companion = file("set.mp4", "mp4"), file("sheet.cue", "cue")
    session.add_all([media, companion])
    await session.flush()
    session.add(FileCompanion(media_id=media.id, companion_id=companion.id))
    await session.flush()
    original = await source(session, media, companion)
    chosen = await select_observation(session, media.id, original.id, kind="tracklist", actor="reviewer")
    chosen.target_mapping = {"media_sha256": media.sha256_hash, "source_sha256": companion.sha256_hash}
    await session.flush()
    newer = await source(session, media, companion, text="New pending notes", revision="b" * 64)
    overview = await get_companion_details(session, media.id)
    assert overview is not None and overview.selected[0].observation_id == original.id and overview.selected[0].availability == "current"
    assert overview.sources[0].latest_stored_content.observation_id == newer.id
    detail = await get_stored_observation(session, media.id, original.id)
    assert detail is not None and detail.selected_availability == "current"
    companion.sha256_hash = "c" * 64
    await session.flush()
    stale = await get_companion_details(session, media.id)
    assert stale is not None and stale.selected[0].availability == "stale" and stale.selected[0].observation_id == original.id
    companion.missing_at = datetime.now(UTC)
    await session.flush()
    missing = await get_companion_details(session, media.id)
    assert missing is not None and missing.links[0].availability == "missing" and missing.selected[0].availability == "missing"
    companion.missing_at = None
    companion.companion_ambiguous_at = datetime.now(UTC)
    await session.flush()
    ambiguous = await get_companion_details(session, media.id)
    assert ambiguous is not None and ambiguous.links[0].availability == "ambiguous"
    await session.execute(delete(FileCompanion).where(FileCompanion.media_id == media.id))
    await session.flush()
    unlinked = await get_companion_details(session, media.id)
    assert unlinked is not None and not unlinked.links and unlinked.selected[0].observation_id == original.id
    assert await get_stored_text(session, media.id, original.id) is not None


async def test_exact_history_membership_denies_future_other_target_content(session: AsyncSession) -> None:
    media, second, companion = file("one.mp3"), file("two.mp3"), file("notes.txt", "txt")
    session.add_all([media, second, companion])
    await session.flush()
    session.add(FileCompanion(media_id=media.id, companion_id=companion.id))
    await session.flush()
    historical = await source(session, media, companion)
    await session.execute(delete(FileCompanion).where(FileCompanion.media_id == media.id))
    future = await source(session, second, companion, text="Future private notes", revision="new-token")
    assert await get_stored_text(session, media.id, historical.id) is not None
    assert await get_stored_text(session, media.id, future.id) is None
    assert await get_stored_observation(session, media.id, future.id) is None
    history = await get_source_observations(session, media.id, historical.object_id)
    assert history is not None and [row.observation_id for row in history.observations] == [historical.id]
    empty_page = await get_source_observations(session, media.id, historical.object_id, page=DetailPage(offset=10))
    assert empty_page is not None and empty_page.observations == ()
    # Retained selection-event membership remains exact even after the candidate is removed.
    from phaze.models.provider_source import ProviderRecordingCandidate

    session.add(ProviderSelectionEvent(media_id=media.id, kind="tracklist", observation_id=historical.id, actor="reviewer"))
    await session.flush()
    await session.execute(delete(ProviderRecordingCandidate).where(ProviderRecordingCandidate.media_id == media.id))
    assert await get_stored_text(session, media.id, historical.id) is not None


async def test_reverse_links_same_agent_and_deleted_source_identity(session: AsyncSession) -> None:
    from phaze.models.agent import Agent

    other_agent = Agent(id="other-details-agent", name="Other")
    session.add(other_agent)
    await session.flush()
    media, foreign, companion = file("one.mp3"), file("other.mp3", agent=other_agent.id), file("notes.txt", "txt")
    session.add_all([media, foreign, companion])
    await session.flush()
    session.add_all([FileCompanion(media_id=media.id, companion_id=companion.id), FileCompanion(media_id=foreign.id, companion_id=companion.id)])
    await session.flush()
    reverse = await get_companion_media(session, companion.id)
    assert reverse is not None and [row.media_id for row in reverse.media] == [media.id]
    assert not (await get_companion_details(session, foreign.id)).links
    historical = await source(session, media, companion)
    await session.execute(delete(FileRecord).where(FileRecord.id == companion.id))
    await session.flush()
    retained = await get_companion_details(session, media.id)
    assert retained is not None and not retained.links and retained.sources[0].filename is None
    assert retained.sources[0].original_file_id == companion.id and retained.sources[0].inventory_availability == "missing"
    assert (await get_stored_text(session, media.id, historical.id)).text == "Original notes"
    assert await get_companion_media(session, uuid.uuid4()) is None


async def test_received_attempt_order_separate_from_immutable_content_and_batched_queries(session: AsyncSession) -> None:
    media, companion = file("set.mp3"), file("notes.txt", "txt")
    session.add_all([media, companion])
    await session.flush()
    session.add(FileCompanion(media_id=media.id, companion_id=companion.id))
    await session.flush()
    identity = SourceIdentity(provider_id="local", native_id="capture-attempt")
    failed_read = SourceRead(status="unavailable", code="offline", scope="synthetic", retrieved_at=datetime.now(UTC) - timedelta(minutes=2))
    failed = await store_source_read(
        session, identity, failed_read, parser_version="source-read-v1", source_file_id=companion.id, channel="companion"
    )
    success_read = SourceRead(
        status="found", code="captured", scope="synthetic", text="Succeeded", retrieved_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    success = await store_source_read(
        session, identity, success_read, parser_version="source-read-v1", source_file_id=companion.id, channel="companion"
    )
    retry = await store_source_read(
        session,
        identity,
        failed_read.model_copy(update={"retrieved_at": datetime.now(UTC)}),
        parser_version="source-read-v1",
        source_file_id=companion.id,
        channel="companion",
    )
    assert retry.reused and retry.observation.id == failed.observation.id

    async def reader(_session: AsyncSession, ids: list[uuid.UUID]) -> dict:
        assert ids == [failed.source.id]
        return {
            failed.source.id: CompanionAttemptSummary(
                source_object_id=failed.source.id,
                attempt_id=uuid.uuid4(),
                observation_id=failed.observation.id,
                received_at=datetime.now(UTC),
                attempted_at=datetime.now(UTC),
                ordinal=3,
                status="unavailable",
                code="offline",
                revision=None,
                revision_scope="unknown",
                truncated=False,
                freshness="unavailable",
                origin="capture",
            )
        }

    engine = session.get_bind()
    statements = []

    def trace(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", trace)
    try:
        overview = await get_companion_details(session, media.id, attempt_reader=reader)
    finally:
        event.remove(engine, "before_cursor_execute", trace)
    assert overview is not None and overview.sources[0].latest_stored_content.observation_id == success.observation.id
    assert overview.sources[0].latest_received_attempt.ordinal == 3 and overview.sources[0].latest_received_attempt.status == "unavailable"
    assert len(statements) <= 7
    full_column = re.compile(r"(?:^SELECT|,)\s+provider_source_observations\.(?:payload|decoded_text)(?:\s+AS\s+\w+)?(?:,|\s+FROM)")
    assert all(full_column.search(statement) is None for statement in statements)


async def test_parameter_validation_before_db_and_missing_endpoint_status(client: AsyncClient, session: AsyncSession) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        await get_companion_details(session, uuid.uuid4(), link_page=DetailPage.model_construct(limit=51))
    with pytest.raises(ValidationError):
        await get_stored_text(session, uuid.uuid4(), uuid.uuid4(), chunk=StoredTextRequest.model_construct(length=32769))
    file_id, obs_id = uuid.uuid4(), uuid.uuid4()
    for url in [
        f"/files/{file_id}/companion-details",
        f"/files/{file_id}/companion-observations/{obs_id}",
        f"/files/{file_id}/companion-observations/{obs_id}/text",
        f"/files/{file_id}/linked-media",
        f"/files/{file_id}/companion-sources/{obs_id}/observations",
    ]:
        assert (await client.get(url)).status_code == 404
    assert (await client.get(f"/files/{file_id}/companion-details?limit=51")).status_code == 422
    assert (await client.get(f"/files/{file_id}/companion-observations/{obs_id}/text?length=32769")).status_code == 422


async def test_generic_legacy_and_embedded_authority_availability(session: AsyncSession) -> None:
    from phaze.models.metadata import FileMetadata
    from phaze.services.local_source_import import embedded_revision

    media = file("embedded.mp3")
    session.add(media)
    await session.flush()
    tags = {"comments": "01. Example - One\n02. Example - Two\n03. Example - Three"}
    metadata = FileMetadata(file_id=media.id, raw_tags=tags)
    session.add(metadata)
    await session.flush()
    identity = SourceIdentity(provider_id="local", native_id=f"embedded:{media.id}:comments")
    snapshot = Snapshot(
        identity=identity,
        revision=embedded_revision(tags, "comments"),
        revision_scope="full",
        retrieved_at=datetime.now(UTC),
        text=tags["comments"],
        source_format="txt",
        parser_version="local-test",
        tracks=tuple(ProviderTrack(position=i + 1, title=f"Track {i}") for i in range(4)),
        completeness=Completeness(state="complete", reason="synthetic"),
    )
    stored = await store_snapshot(session, snapshot, source_file_id=media.id, channel="embedded")
    await add_candidate(session, media.id, stored.observation.id)
    selection = await select_observation(session, media.id, stored.observation.id, kind="tracklist", actor="reviewer")
    legacy = await get_companion_details(session, media.id)
    assert legacy.selected[0].availability == "unknown_binding"
    selection.target_mapping = {"media_sha256": media.sha256_hash, "source_sha256": media.sha256_hash}
    await session.flush()
    detail = await get_stored_observation(session, media.id, stored.observation.id, track_page=DetailPage(limit=2))
    assert detail.selected_availability == "current" and len(detail.selected_tracks) == 2 and detail.selected_track_count == 4
    assert detail.next_selected_track_offset == 2
    end = await get_stored_observation(session, media.id, stored.observation.id, track_page=DetailPage(offset=2, limit=2))
    assert end.next_selected_track_offset is None and end.selected_tracks[0].position == 3
    release = await select_observation(session, media.id, stored.observation.id, kind="release_metadata", actor="reviewer")
    release.target_mapping = selection.target_mapping
    await session.flush()
    assert (await get_stored_observation(session, media.id, stored.observation.id)).selected_track_count == 4
    media.file_type = "txt"
    await session.flush()
    assert all(authority.availability == "unavailable" for authority in (await get_companion_details(session, media.id)).selected)
    media.file_type = "mp3"
    metadata.raw_tags = {"comments": "Changed"}
    await session.flush()
    assert (await get_companion_details(session, media.id)).selected[0].availability == "stale"
    assert (await get_source_observations(session, media.id, stored.source.id)).observations[0].availability == "stale"
    metadata.raw_tags = {"comments": "x" * 2097153, "unrelated": "x" * 100000}
    await session.flush()
    assert (await get_stored_observation(session, media.id, stored.observation.id)).summary.availability == "stale"
    metadata.raw_tags = None
    await session.flush()
    assert (await get_companion_details(session, media.id)).selected[0].availability == "stale"
    metadata.failed_at = datetime.now(UTC)
    await session.flush()
    assert (await get_companion_details(session, media.id)).selected[0].availability == "stale"
    media.missing_at = datetime.now(UTC)
    await session.flush()
    assert (await get_companion_details(session, media.id)).selected[0].availability == "missing"
    media.missing_at = None
    media.sha256_hash = "b" * 64
    await session.flush()
    assert (await get_companion_details(session, media.id)).selected[0].availability == "stale"
    generic = await store_snapshot(
        session,
        snapshot.model_copy(
            update={"identity": SourceIdentity(provider_id="example_catalog", native_id="generic"), "revision": None, "revision_scope": "unknown"}
        ),
    )
    await add_candidate(session, media.id, generic.observation.id)
    session.add(ProviderRecordingSelection(media_id=media.id, kind="unsupported_future_kind", observation_id=generic.observation.id, actor="legacy"))
    await session.flush()
    generic_detail = await get_stored_observation(session, media.id, generic.observation.id)
    assert generic_detail.summary.availability == "current" and generic_detail.summary.selected_kinds == ()
    assert len((await get_companion_details(session, media.id)).selected) == 2


async def test_unavailable_failed_unlinked_and_foreign_attempts(session: AsyncSession) -> None:
    from phaze.models.agent import Agent

    other = Agent(id="details-moved-owner", name="Other")
    session.add(other)
    media, second, companion = file("one.mp3"), file("second.mp3"), file("notes.txt", "txt")
    session.add_all([media, second, companion])
    await session.flush()
    session.add(FileCompanion(media_id=media.id, companion_id=companion.id))
    await session.flush()
    original = await source(session, media, companion)
    identity = SourceIdentity(provider_id="local", native_id="errors")
    read = SourceRead(status="unavailable", code="offline", scope="source", retrieved_at=datetime.now(UTC))
    failed = await store_source_read(session, identity, read, parser_version="source-read-v1", source_file_id=companion.id, channel="companion")
    assert (await get_stored_observation(session, media.id, failed.observation.id)).summary.availability == "unavailable"
    chunk = await get_stored_text(session, media.id, failed.observation.id)
    assert chunk.text is None and chunk.total_characters == 0
    await session.execute(delete(FileCompanion).where(FileCompanion.media_id == media.id))
    assert (await get_stored_observation(session, media.id, original.id)).summary.availability == "unlinked"
    future = await source(session, second, companion, text="Future other target", revision="future")
    companion.agent_id = other.id
    await session.flush()
    overview = await get_companion_details(session, media.id)
    assert overview.sources[0].filename is None and overview.sources[0].inventory_availability == "unavailable"
    assert overview.sources[0].latest_stored_content.availability == "unavailable"

    async def forbidden_attempt(_session, ids):
        return {
            original.object_id: CompanionAttemptSummary(
                source_object_id=original.object_id,
                attempt_id=uuid.uuid4(),
                observation_id=future.id,
                received_at=datetime.now(UTC),
                attempted_at=datetime.now(UTC),
                ordinal=10,
                status="found",
                code="read",
                revision="future",
                revision_scope="full",
                truncated=False,
                freshness="current",
                origin="capture",
            )
        }

    isolated = await get_companion_details(session, media.id, attempt_reader=forbidden_attempt)
    assert isolated.sources[0].latest_received_attempt is None and isolated.sources[0].attempt_history == "unknown"
    assert await get_stored_text(session, media.id, future.id) is None


async def test_lazy_detail_rechecks_live_membership(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from phaze.models.provider_source import ProviderRecordingCandidate
    from phaze.services import companion_details

    media, companion = file("set.mp3"), file("notes.txt", "txt")
    session.add_all([media, companion])
    await session.flush()
    session.add(FileCompanion(media_id=media.id, companion_id=companion.id))
    await session.flush()
    observation = await source(session, media, companion)
    await session.execute(delete(ProviderRecordingCandidate).where(ProviderRecordingCandidate.media_id == media.id))
    original = companion_details._authorities

    async def revoked(*args):
        authorities = await original(*args)
        await session.execute(delete(FileCompanion).where(FileCompanion.media_id == media.id))
        return authorities

    monkeypatch.setattr(companion_details, "_authorities", revoked)
    assert await get_stored_observation(session, media.id, observation.id) is None
    assert await get_stored_text(session, media.id, observation.id) is None


async def test_complete_browse_multiple_sources_and_reverse_targets(session: AsyncSession) -> None:
    media, second = file("set.mp3"), file("video.mp4", "mp4")
    companions = [file(f"source-{i}.txt", "txt") for i in range(3)]
    session.add_all([media, second, *companions])
    await session.flush()
    for index, companion in enumerate(companions):
        session.add(FileCompanion(media_id=media.id, companion_id=companion.id))
        await session.flush()
        await source(session, media, companion, native=f"source-{index}")
    session.add(FileCompanion(media_id=second.id, companion_id=companions[0].id))
    await session.flush()
    first = await get_companion_details(session, media.id, link_page=DetailPage(limit=1), source_page=DetailPage(limit=1))
    last = await get_companion_details(session, media.id, link_page=DetailPage(offset=1, limit=2), source_page=DetailPage(offset=1, limit=2))
    assert first.next_link_offset == first.next_source_offset == 1
    assert last.next_link_offset is None and last.next_source_offset is None
    assert len({source.source_object_id for source in (*first.sources, *last.sources)}) == 3
    assert len({link.file_id for link in (*first.links, *last.links)}) == 3
    reverse = await get_companion_media(session, companions[0].id, page=DetailPage(limit=1))
    reverse_end = await get_companion_media(session, companions[0].id, page=DetailPage(offset=1, limit=1))
    assert reverse.next_offset == 1 and reverse_end.next_offset is None
    assert {reverse.media[0].media_id, reverse_end.media[0].media_id} == {media.id, second.id}


async def test_live_membership_and_reverse_use_current_owners(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from phaze.models.agent import Agent
    from phaze.models.provider_source import ProviderRecordingCandidate
    from phaze.services import companion_details

    other = Agent(id="reowned-details-agent", name="Other")
    session.add(other)
    media, companion = file("set.mp3"), file("notes.txt", "txt")
    session.add_all([media, companion])
    await session.flush()
    session.add(FileCompanion(media_id=media.id, companion_id=companion.id))
    await session.flush()
    observation = await source(session, media, companion)
    await session.execute(delete(ProviderRecordingCandidate).where(ProviderRecordingCandidate.media_id == media.id))
    original = companion_details._file

    async def reowned(session, file_id):
        loaded = await original(session, file_id)
        await session.execute(
            update(FileRecord).where(FileRecord.id == file_id).values(agent_id=other.id).execution_options(synchronize_session=False)
        )
        return loaded

    monkeypatch.setattr(companion_details, "_file", reowned)
    assert await get_stored_text(session, media.id, observation.id) is None
    await session.execute(
        update(FileRecord).where(FileRecord.id == media.id).values(agent_id="test-fileserver").execution_options(synchronize_session=False)
    )
    assert await get_stored_observation(session, media.id, observation.id) is None
    await session.execute(
        update(FileRecord).where(FileRecord.id == media.id).values(agent_id="test-fileserver").execution_options(synchronize_session=False)
    )
    assert (await get_companion_media(session, companion.id)).media == ()
