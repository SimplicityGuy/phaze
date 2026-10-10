"""Real PostgreSQL storage: opaque identity, revisions, review authority and lifecycle."""

import asyncio
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.provider_source import (
    ProviderRecordingCandidate,
    ProviderRecordingSelection,
    ProviderSelectionEvent,
    ProviderSourceObject,
    ProviderSourceObservation,
)
from phaze.models.tracklist import Tracklist
from phaze.services import provider_persistence as storage
from phaze.services.scan_deletion import delete_file_cascade
from phaze.tracklist_providers.domain import Completeness, ProviderTrack, ReleaseFact, Snapshot, SourceIdentity, SourceRead


def snapshot(native: str = "set-01", provider: str = "example_catalog", revision: str | None = "rev-a", **kwargs: object) -> Snapshot:
    return Snapshot.model_validate(
        {
            "identity": {"provider_id": provider, "native_id": native},
            "revision": revision,
            "revision_scope": "full" if revision else "unknown",
            "retrieved_at": datetime.now(UTC),
            "source_format": "txt",
            "parser_version": "v1",
            "text": "01. Example Artist - Opening",
            "tracks": [ProviderTrack(position=1, title="Opening"), ProviderTrack(position=2)],
            "release_facts": [
                ReleaseFact(field="genre", value="House", original_value="HOUSE", normalized_value="House", certainty="known", source_line=3)
            ],
            "completeness": Completeness(state="complete", reason="grammar exhausted"),
            **kwargs,
        }
    )


def file(name: str, kind: str = "mp3") -> FileRecord:
    return FileRecord(
        id=uuid.uuid4(),
        agent_id="test-fileserver",
        sha256_hash="a" * 64,
        original_path=f"/synthetic/{name}",
        current_path=f"/synthetic/{name}",
        original_filename=name,
        file_type=kind,
        file_size=40,
    )


async def test_scoped_full_keys_forced_collisions_and_revisionless_idempotence(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage, "_digest", lambda _value: "f" * 64)
    first = await storage.store_snapshot(session, snapshot("opaque/" + "🎵" * 1000))
    other = await storage.store_snapshot(session, snapshot("different"))
    provider = await storage.store_snapshot(session, snapshot("different", provider="second_catalog"))
    assert len({first.source.id, other.source.id, provider.source.id}) == 3
    assert first.source.native_id == "opaque/" + "🎵" * 1000
    same = await storage.store_snapshot(
        session, snapshot("different", retrieved_at=datetime.now(UTC) + timedelta(days=1), url="https://example.invalid/new")
    )
    assert same.reused and same.observation.id == other.observation.id
    unknown = await storage.store_snapshot(session, snapshot("revisionless", revision=None))
    retry = await storage.store_snapshot(session, snapshot("revisionless", revision=None))
    assert retry.reused and retry.observation.id == unknown.observation.id and retry.observation.revision is None
    changed = await storage.store_snapshot(session, snapshot("revisionless", revision=None, text="changed"))
    assert changed.observation.id != unknown.observation.id and not changed.conflict


async def test_revision_conflicts_reinterpretation_and_parent_validation(session: AsyncSession) -> None:
    first = await storage.store_snapshot(session, snapshot())
    changed = await storage.store_snapshot(session, snapshot(text="changed"))
    assert changed.conflict and changed.observation.conflict_with_id == first.observation.id
    retry = await storage.store_snapshot(session, snapshot(text="changed"))
    assert retry.reused and retry.conflict
    reparse = await storage.store_snapshot(session, snapshot(parser_version="v2", tracks=[ProviderTrack(position=1, title="Reinterpreted")]))
    assert not reparse.conflict and reparse.observation.parent_id == first.observation.id
    bad_reparse = await storage.store_snapshot(session, snapshot(parser_version="v3", text="changed again"))
    assert bad_reparse.conflict
    next_revision = await storage.store_snapshot(session, snapshot(revision="rev-b", text="changed"))
    assert not next_revision.conflict and next_revision.observation.id != first.observation.id
    stranger = await storage.store_snapshot(session, snapshot("other"))
    with pytest.raises(ValueError, match="Parent"):
        await storage.store_snapshot(session, snapshot(revision="rev-c"), parent_id=stranger.observation.id)
    explicit_parent = await storage.store_snapshot(session, snapshot(revision="rev-d"), parent_id=first.observation.id)
    assert explicit_parent.observation.parent_id == first.observation.id
    assert first.observation.decoded_text == "01. Example Artist - Opening"
    assert first.observation.payload["tracks"][1]["title"] is None
    assert first.observation.payload["release_facts"][0]["source_line"] == 3


async def test_error_observations_and_parse_incompleteness_are_separate(session: AsyncSession) -> None:
    identity = SourceIdentity(provider_id="example_catalog", native_id="failed")
    for status in ("retry", "unavailable", "absent", "ambiguous", "incomplete", "unsupported", "contract_error"):
        read = SourceRead(
            status=status, code="read-result", scope="source", truncated=status == "incomplete", text="head", retrieved_at=datetime.now(UTC)
        )
        stored = await storage.store_source_read(session, identity, read, parser_version="reader-v1")
        assert stored.observation.status == status and "tracks" not in stored.observation.payload
        assert await storage.source_availability(session, stored.observation, uuid.uuid4()) == (
            "current" if status in {"incomplete", "ambiguous"} else status
        )
        assert stored.observation.truncated == read.truncated
        assert (await storage.store_source_read(session, identity, read, parser_version="reader-v1")).reused
    partial = await storage.store_snapshot(
        session, snapshot("partial", completeness=Completeness(state="incomplete", reason="unrecognized track-like line"))
    )
    assert partial.observation.status == "incomplete" and not partial.observation.truncated
    with pytest.raises(ValueError, match="incompatible"):
        await storage.store_snapshot(session, snapshot("partial", completeness=Completeness(state="incomplete", reason="cap")), status="found")
    with pytest.raises(ValueError):
        await storage.store_snapshot(session, snapshot(), status="incomplete")
    with pytest.raises(ValueError, match="empty-track"):
        await storage.store_snapshot(session, snapshot(tracks=[]))
    with pytest.raises(ValueError, match="Parser"):
        await storage.store_source_read(session, identity, read, parser_version="")


async def test_selection_retains_audit_and_other_targets_across_relink_delete(session: AsyncSession) -> None:
    media, other_media, companion = file("set.mp3"), file("other.mp3"), file("set.txt", "txt")
    session.add_all([media, other_media, companion])
    await session.flush()
    session.add_all([FileCompanion(companion_id=companion.id, media_id=media.id), FileCompanion(companion_id=companion.id, media_id=other_media.id)])
    await session.flush()
    first = await storage.store_snapshot(
        session,
        snapshot(f"companion:{companion.id}", revision="a" * 64, provenance=["revision:sha256:full"]),
        source_file_id=companion.id,
        channel="companion",
    )
    await storage.add_candidate(session, media.id, first.observation.id)
    assert (await storage.add_candidate(session, media.id, first.observation.id)).status == "pending"
    selected = await storage.select_observation(session, media.id, first.observation.id, kind="tracklist", actor="reviewer-a")
    assert selected.observation_id == first.observation.id
    await storage.add_candidate(session, other_media.id, first.observation.id)
    await storage.select_observation(session, other_media.id, first.observation.id, kind="tracklist", actor="reviewer-b")
    next_observation = await storage.store_snapshot(
        session,
        snapshot(f"companion:{companion.id}", revision="b" * 64, text="new", provenance=["revision:sha256:full"]),
        source_file_id=companion.id,
        channel="companion",
    )
    assert selected.observation_id == first.observation.id
    await storage.add_candidate(session, media.id, next_observation.observation.id)
    companion.sha256_hash = "b" * 64
    await session.flush()
    assert await storage.source_availability(session, first.observation, media.id) == "stale"
    await storage.select_observation(session, media.id, next_observation.observation.id, kind="tracklist", actor="reviewer-c")
    await storage.select_observation(session, media.id, next_observation.observation.id, kind="release_metadata", actor="reviewer-c")
    history = (await session.scalars(select(ProviderSelectionEvent).where(ProviderSelectionEvent.media_id == media.id))).all()
    assert {event.actor for event in history} == {"reviewer-a", "reviewer-c"} and len(history) == 3
    assert (await session.scalars(select(ProviderRecordingCandidate).where(ProviderRecordingCandidate.media_id == media.id))).all()[
        0
    ].status == "accepted"
    await session.execute(delete(FileCompanion).where(FileCompanion.media_id == media.id))
    assert await storage.source_availability(session, next_observation.observation, media.id) == "unlinked"
    assert await storage.source_availability(session, next_observation.observation, other_media.id) == "current"
    with pytest.raises(ValueError, match="currently"):
        await storage.select_observation(session, media.id, next_observation.observation.id, kind="tracklist", actor="reviewer")
    companion.missing_at = datetime.now(UTC)
    assert await storage.source_availability(session, first.observation, other_media.id) == "missing"
    companion.missing_at = None
    companion.companion_ambiguous_at = datetime.now(UTC)
    assert await storage.source_availability(session, first.observation, other_media.id) == "ambiguous"
    await delete_file_cascade(session, companion.id)
    await session.refresh(first.source)
    assert first.source.source_file_id is None and first.source.original_file_id == companion.id
    assert await storage.source_availability(session, first.observation, other_media.id) == "missing"
    assert await session.get(ProviderSourceObservation, first.observation.id) is not None
    assert await session.get(ProviderRecordingSelection, (other_media.id, "tracklist")) is not None
    await delete_file_cascade(session, media.id)
    assert await session.get(ProviderSourceObservation, first.observation.id) is not None
    assert (await session.scalar(select(func.count()).select_from(ProviderSelectionEvent))) == 4


async def test_selection_guards_and_embedded_revision_scope(session: AsyncSession) -> None:
    media, other = file("set.mp3"), file("other.mp3")
    session.add_all([media, other])
    await session.flush()
    value = await storage.store_snapshot(session, snapshot("embedded", revision="b" * 64), source_file_id=media.id, channel="embedded")
    assert await storage.source_availability(session, value.observation, media.id) == "current"
    assert await storage.source_availability(session, value.observation, other.id) == "unlinked"
    with pytest.raises(ValueError, match="candidate"):
        await storage.select_observation(session, media.id, value.observation.id, kind="tracklist", actor="reviewer")
    await storage.add_candidate(session, media.id, value.observation.id)
    with pytest.raises(ValueError, match="known kind"):
        await storage.select_observation(session, media.id, value.observation.id, kind="unknown", actor="reviewer")
    conflict = await storage.store_snapshot(
        session, snapshot("embedded", revision="b" * 64, text="other"), source_file_id=media.id, channel="embedded"
    )
    await storage.add_candidate(session, media.id, conflict.observation.id)
    with pytest.raises(ValueError, match="acknowledgement"):
        await storage.select_observation(session, media.id, conflict.observation.id, kind="tracklist", actor="reviewer")
    await storage.select_observation(session, media.id, conflict.observation.id, kind="tracklist", actor="reviewer", accept_conflict=True)
    with pytest.raises(ValueError, match="rebound"):
        await storage.store_snapshot(session, snapshot("embedded"), source_file_id=other.id, channel="embedded")


async def test_bounds_and_resumable_legacy_identity_preserve_projections(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="binding"):
        await storage.resolve_source(session, SourceIdentity(provider_id="example_catalog", native_id="one"), channel="companion")
    with pytest.raises(ValueError, match="NUL"):
        await storage.store_snapshot(session, snapshot(text="bad\0text"))
    with pytest.raises(ValueError, match="NUL"):
        await storage.store_snapshot(session, snapshot(release_facts=[ReleaseFact(field="genre", evidence=("bad\0evidence",))]))
    literal = await storage.store_snapshot(session, snapshot("escaped-nul", text=r"literal \u0000"))
    assert literal.observation.decoded_text == r"literal \u0000"
    with pytest.raises(ValueError):
        await storage.store_snapshot(session, snapshot("x" * 4097))
    canonical = Tracklist(external_id="legacy", source_url="", source="historical-provider")
    projection = Tracklist(external_id="legacy", source_url="", source="historical-provider", propagated_from_set_key="k" * 64)
    manual = Tracklist(external_id="manual", source_url="", source="manual")
    session.add_all([canonical, projection, manual])
    await session.flush()
    original_ids = (canonical.id, projection.id, manual.id)
    assert await storage.backfill_legacy_identities(session, batch_size=1) == 1
    assert await storage.backfill_legacy_identities(session, batch_size=1) == 1
    assert await storage.backfill_legacy_identities(session, batch_size=1) == 0
    assert canonical.provider_object_id == projection.provider_object_id and manual.provider_object_id != canonical.provider_object_id
    assert (canonical.id, projection.id, manual.id) == original_ids
    assert projection.propagated_from_set_key == "k" * 64
    with pytest.raises(ValueError, match="batch"):
        await storage.backfill_legacy_identities(session, batch_size=0)
    with pytest.raises(ValueError, match="evidence"):
        await storage.add_candidate(session, uuid.uuid4(), uuid.uuid4(), evidence=("x" * 4097,))


async def test_concurrent_equal_and_distinct_imports_converge(async_engine) -> None:
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    native = str(uuid.uuid4())

    async def import_one(revision: str) -> tuple[uuid.UUID, uuid.UUID]:
        async with maker.begin() as db:
            stored = await storage.store_snapshot(db, snapshot(native, revision=revision))
            return stored.source.id, stored.observation.id

    try:
        equal = await asyncio.gather(*(import_one("a") for _ in range(6)))
        assert len(set(equal)) == 1
        distinct = await asyncio.gather(import_one("b"), import_one("c"))
        assert len({row[1] for row in distinct}) == 2 and {row[0] for row in distinct} == {equal[0][0]}
        async with maker() as db:
            assert (
                await db.scalar(select(func.count()).select_from(ProviderSourceObservation).where(ProviderSourceObservation.object_id == equal[0][0]))
                == 3
            )
    finally:
        async with maker.begin() as db:
            ids = select(ProviderSourceObject.id).where(ProviderSourceObject.native_id == native)
            await db.execute(delete(ProviderSourceObservation).where(ProviderSourceObservation.object_id.in_(ids)))
            await db.execute(delete(ProviderSourceObject).where(ProviderSourceObject.native_id == native))


async def test_source_deletion_race_refuses_new_selection_and_preserves_observation(async_engine) -> None:
    """Selection waits behind actual deletion locks, then rechecks refreshed source/link state."""
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    media, companion = file(f"{uuid.uuid4()}.mp3"), file(f"{uuid.uuid4()}.txt", "txt")
    identity = str(uuid.uuid4())
    source_id = None
    try:
        async with maker.begin() as db:
            db.add_all([media, companion])
            await db.flush()
            db.add(FileCompanion(companion_id=companion.id, media_id=media.id))
            await db.flush()
            stored = await storage.store_snapshot(db, snapshot(identity), source_file_id=companion.id, channel="companion")
            source_id, observation_id = stored.source.id, stored.observation.id
            await storage.add_candidate(db, media.id, observation_id)
        locked, release = asyncio.Event(), asyncio.Event()

        async def delete_source() -> None:
            async with maker.begin() as db:
                await db.execute(select(FileRecord.id).where(FileRecord.id == companion.id).with_for_update())
                locked.set()
                await release.wait()
                await delete_file_cascade(db, companion.id)

        async def select_source() -> None:
            async with maker.begin() as db:
                await storage.select_observation(db, media.id, observation_id, kind="tracklist", actor="reviewer")

        deletion = asyncio.create_task(delete_source())
        await locked.wait()
        selection = asyncio.create_task(select_source())
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(selection), timeout=0.05)
        finally:
            release.set()
        await asyncio.wait_for(deletion, timeout=5)
        with pytest.raises(ValueError, match=r"disappeared|currently"):
            await asyncio.wait_for(selection, timeout=5)
        async with maker() as db:
            assert await db.get(ProviderSourceObservation, observation_id) is not None
            assert await db.get(ProviderRecordingSelection, (media.id, "tracklist")) is None
    finally:
        async with maker.begin() as db:
            await delete_file_cascade(db, media.id)
            await delete_file_cascade(db, companion.id)
            if source_id is not None:
                await db.execute(delete(ProviderSourceObservation).where(ProviderSourceObservation.object_id == source_id))
                await db.execute(delete(ProviderSourceObject).where(ProviderSourceObject.id == source_id))
