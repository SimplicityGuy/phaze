"""Local import creates candidates; explicit optimistic decisions create authority.

No disk/network or media writes. Callers own transactions. Composed writes lock source
buckets, globally sorted inventory, then source objects/candidates/selections/events.
"""

from datetime import UTC, datetime
import hashlib
import re
from typing import Literal
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import (
    ProviderRecordingCandidate,
    ProviderRecordingSelection,
    ProviderSelectionEvent,
    ProviderSourceObject,
    ProviderSourceObservation,
)
from phaze.schemas.local_source_import import ImportLocalSource, ImportResult, SelectedRecordingSource, SourceDecision, SourceKind
from phaze.services.companion_content import MEDIA_FILE_TYPES
from phaze.services.provider_persistence import add_candidate, lock_source_bucket, source_availability, store_snapshot, store_source_read
from phaze.tracklist_providers import local_registry
from phaze.tracklist_providers.domain import (
    Completeness,
    DiscoveryBudget,
    OutcomeStatus,
    ProviderCandidate,
    ProviderTrack,
    ReleaseFact,
    Snapshot,
    SourceIdentity,
    SourceRead,
    SourceReference,
    Timestamp,
)
from phaze.tracklist_providers.local_parser import LocalSourceInterpreter
from phaze.tracklist_providers.local_release import RELEASE_PARSER_VERSION, ReleaseExtraction, extract_release_metadata


class _StoredEffects:
    """Only the explicitly supplied DB content can be read by the local provider."""

    def __init__(self, reference: SourceReference, read: SourceRead) -> None:
        self.reference = reference
        self.content = read

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        if source != self.reference:
            raise PermissionError("Unauthorized stored source")
        return _bounded_read(self.content, budget)


def _bounded_read(read: SourceRead, budget: DiscoveryBudget) -> SourceRead:
    """Clip already stored text for negotiated parsing; preserve full original observation."""
    if read.text is None:
        return read
    text = read.text[: budget.max_characters]
    encoded = text.encode("utf-8")
    if len(encoded) > budget.max_bytes:
        text = encoded[: budget.max_bytes].decode("utf-8", errors="ignore")
    lines = text.splitlines(keepends=True)
    if len(lines) > budget.max_lines:
        text = "".join(lines[: budget.max_lines])
    limited = text != read.text or read.bytes_read > budget.max_bytes
    values = read.model_dump()
    values.update(text=text, bytes_read=min(read.bytes_read, budget.max_bytes))
    if limited:
        values.update(status=OutcomeStatus.INCOMPLETE, truncated=True, code="import_input_cap", evidence=(*read.evidence[:63], "import:input_cap"))
    return SourceRead.model_validate(values)


def _embedded_text(tags: dict | None, channel: str) -> str:
    """Read an exact, explicit descriptive tag channel; do not guess tag prefixes."""
    value = (tags or {}).get(channel)
    parts = [value] if isinstance(value, str) else value if isinstance(value, list) else None
    if parts is None or len(parts) > 262144:
        raise ValueError("Embedded channel has no bounded stored text")
    characters = max(0, len(parts) - 1)
    for part in parts:
        if not isinstance(part, str) or len(part) > 262144:
            raise ValueError("Embedded channel exceeds stored source bounds")
        characters += len(part)
        if characters > 262144:
            raise ValueError("Embedded channel exceeds stored source bounds")
    # At most 262144 Unicode code points (including separators) bound UTF8 to 1MiB.
    return "\n".join(parts)


def embedded_revision(tags: dict | None, channel: str) -> str:
    return hashlib.sha256(_embedded_text(tags, channel).encode("utf-8")).hexdigest()


async def _locked_inventory(
    session: AsyncSession, identity: SourceIdentity, media_id: uuid.UUID, source_id: uuid.UUID
) -> tuple[FileRecord, FileRecord]:
    await lock_source_bucket(session, identity)
    rows = (
        await session.scalars(
            select(FileRecord)
            .where(FileRecord.id.in_({media_id, source_id}))
            .order_by(FileRecord.original_path, FileRecord.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    by_id = {row.id: row for row in rows}
    if media_id not in by_id or source_id not in by_id:
        raise ValueError("Source or recording no longer exists")
    media, source = by_id[media_id], by_id[source_id]
    if media.file_type not in MEDIA_FILE_TYPES or media.agent_id != source.agent_id:
        raise ValueError("Source and recording must have the same owning agent")
    if media.missing_at is not None or source.missing_at is not None or source.companion_ambiguous_at is not None:
        raise ValueError("Source or recording is unavailable")
    return media, source


async def _live_link(session: AsyncSession, source_id: uuid.UUID, media_id: uuid.UUID) -> FileCompanion:
    link = await session.scalar(
        select(FileCompanion)
        .where(FileCompanion.companion_id == source_id, FileCompanion.media_id == media_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if link is None:
        raise ValueError("Source is no longer linked to this recording")
    return link


async def import_local_source(session: AsyncSession, command: ImportLocalSource) -> ImportResult:
    """Consume an immutable capture or explicit embedded channel, never choose a source."""
    command = ImportLocalSource.model_validate(command)
    channel: Literal["companion", "embedded"]
    embedded_tags: dict | None = None
    raw: ProviderSourceObservation
    if command.raw_observation_id is not None:
        captured = await session.get(ProviderSourceObservation, command.raw_observation_id)
        source_object = await session.get(ProviderSourceObject, captured.object_id) if captured else None
        if (
            captured is None
            or source_object is None
            or source_object.provider_id != "local"
            or source_object.channel != "companion"
            or source_object.source_file_id is None
            or "tracks" in captured.payload
        ):
            raise ValueError("Import requires an owned raw companion observation")
        raw = captured
        identity = SourceIdentity(provider_id="local", native_id=source_object.native_id)
        source_id = source_object.source_file_id
        read = SourceRead.model_validate({**raw.payload, "retrieved_at": raw.retrieved_at})
        channel = "companion"
    else:
        source_id = command.media_id
        identity = SourceIdentity(provider_id="local", native_id=f"embedded:{source_id}:{command.embedded_channel}")
        metadata = await session.scalar(select(FileMetadata).where(FileMetadata.file_id == source_id).execution_options(populate_existing=True))
        if metadata is None or metadata.failed_at is not None:
            raise ValueError("Embedded metadata is unavailable")
        embedded_tags = metadata.raw_tags
        text = _embedded_text(embedded_tags, str(command.embedded_channel))
        # Never scan beyond the bounded stored channel for parser input. Full revision hashes
        # the channel (already in DB), not media bytes; oversized text remains incomplete.
        read = SourceRead(
            status=OutcomeStatus.FOUND,
            code="embedded_stored",
            scope=identity.native_id,
            text=text,
            encoding="utf-8",
            revision=embedded_revision(embedded_tags, str(command.embedded_channel)),
            revision_scope="full",
            bytes_read=len(text.encode("utf-8")),
            evidence=(f"embedded:channel:{command.embedded_channel}", "revision:embedded-text:sha256"),
            retrieved_at=datetime.now(UTC),
        )
        channel = "embedded"
    media, file = await _locked_inventory(session, identity, command.media_id, source_id)
    evidence: tuple[str, ...]
    if channel == "companion":
        link = await _live_link(session, source_id, media.id)
        inventory_markers = [
            value.removeprefix("capture:inventory_sha256:") for value in read.evidence if value.startswith("capture:inventory_sha256:")
        ]
        full_revision = read.revision if "revision:sha256:full" in read.evidence and read.revision_scope == "full" else None
        expected_inventory = inventory_markers[0] if len(inventory_markers) == 1 else full_revision
        if expected_inventory is None or expected_inventory != file.sha256_hash:
            raise ValueError("Capture lacks current inventory revision evidence; recapture required")
        if await source_availability(session, raw, media.id) != "current":
            # Failed raw reads remain visible as candidates; stale/missing inventory cannot import.
            availability = await source_availability(session, raw, media.id)
            if availability not in {"absent", "unavailable", "retry", "unsupported", "contract_error"}:
                raise ValueError("Captured source is not current for this target")
        evidence = (f"association:{link.derivation_method or 'unknown'}", f"association:revision:{link.derivation_revision or 'unknown'}")
        source_format = file.file_type
    else:
        metadata = await session.scalar(
            select(FileMetadata).where(FileMetadata.file_id == source_id).with_for_update().execution_options(populate_existing=True)
        )
        if metadata is None or metadata.failed_at is not None or metadata.raw_tags != embedded_tags:
            raise ValueError("Embedded metadata changed before import")
        stored_raw = await store_source_read(session, identity, read, parser_version="source-read-v1", source_file_id=source_id, channel=channel)
        raw = stored_raw.observation
        evidence = (f"embedded:channel:{command.embedded_channel}",)
        source_format = str(command.embedded_channel)
    reference = SourceReference(identity=identity, channel=channel, format=source_format, revision=read.revision)
    candidate = ProviderCandidate(identity=identity, source=reference, observed_revision=read.revision)
    effects = _StoredEffects(reference, read)
    outcome = await local_registry(LocalSourceInterpreter()).load(candidate, command.budget, effects)
    candidate_ids: list[uuid.UUID] = []
    track_observation_id: uuid.UUID | None = None
    if outcome.snapshot is not None:
        stored = await store_snapshot(
            session, outcome.snapshot, status=outcome.status.value, source_file_id=source_id, channel=channel, parent_id=raw.id
        )
        track_observation_id = stored.observation.id
        row = await add_candidate(session, media.id, stored.observation.id, evidence=(*evidence, "kind:tracklist"))
        candidate_ids.append(row.id)
    else:
        row = await add_candidate(session, media.id, raw.id, evidence=(*evidence, f"tracklist:{outcome.status.value}"))
        candidate_ids.append(row.id)
    bounded = _bounded_read(read, command.budget)
    release = extract_release_metadata(bounded, source_format=source_format if channel == "companion" else "txt", budget=command.budget)
    release_observation_id: uuid.UUID | None = None
    release_status = bounded.status.value if bounded.status not in {OutcomeStatus.FOUND, OutcomeStatus.INCOMPLETE} else "incomplete"
    if bounded.text is not None and bounded.status in {OutcomeStatus.FOUND, OutcomeStatus.INCOMPLETE}:
        snapshot = _release_snapshot(identity, bounded, source_format, release)
        stored = await store_snapshot(session, snapshot, source_file_id=source_id, channel=channel, parent_id=raw.id)
        release_observation_id = stored.observation.id
        release_status = stored.observation.status
        row = await add_candidate(session, media.id, stored.observation.id, evidence=(*evidence, "kind:release_metadata"))
        candidate_ids.append(row.id)
    return ImportResult(
        raw_observation_id=raw.id,
        tracklist_observation_id=track_observation_id,
        release_observation_id=release_observation_id,
        candidate_ids=tuple(candidate_ids),
        tracklist_status=outcome.status.value,
        release_status=release_status,
    )


def _release_snapshot(identity: SourceIdentity, read: SourceRead, source_format: str, release: ReleaseExtraction) -> Snapshot:
    """Original values repeat in normalized facts; bound aggregate serialized output too."""
    base = Snapshot(
        identity=identity,
        revision=read.revision,
        revision_scope=read.revision_scope,
        retrieved_at=read.retrieved_at,
        text=read.text,
        encoding=read.encoding,
        source_format=source_format,
        parser_version=RELEASE_PARSER_VERSION,
        completeness=release.completeness,
        provenance=(*read.evidence[:63], "kind:release_metadata"),
    )
    size = len(base.model_dump_json().encode("utf-8"))
    facts: list[ReleaseFact] = []
    completeness = release.completeness
    for fact in release.facts:
        size += len(fact.model_dump_json().encode("utf-8")) + 1
        if size > 4194304 - 4096:
            completeness = Completeness(
                state="incomplete",
                reason="Release metadata output bounded",
                evidence=(*release.completeness.evidence[:63], "limit:release_snapshot_output"),
            )
            break
        facts.append(fact)
    return Snapshot.model_validate({**base.model_dump(), "release_facts": tuple(facts), "completeness": completeness})


def selection_token(selection: ProviderRecordingSelection) -> uuid.UUID:
    """Legacy selections are identified without rewriting historical choices."""
    return selection.selection_token or uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"phaze:{selection.media_id}:{selection.kind}:{selection.observation_id}:{selection.selected_at.isoformat()}:{selection.actor}",
    )


def _snapshot(observation: ProviderSourceObservation) -> Snapshot:
    return Snapshot.model_validate({**observation.payload, "retrieved_at": observation.retrieved_at, "url": observation.source_url})


def _cue_ordinal(track: ProviderTrack) -> int | None:
    for item in track.evidence:
        match = re.match(r"^FILE:(\d+):", item)
        if match:
            return int(match.group(1))
    return None


def _project_tracks(snapshot: Snapshot, media_id: uuid.UUID, mapping: dict | None, availability: str = "current") -> tuple[ProviderTrack, ...]:
    result: list[ProviderTrack] = []
    for track in snapshot.tracks:
        if snapshot.source_format == "cue":
            if mapping is None or _cue_ordinal(track) != mapping.get("cue_file_ordinal"):
                continue
            timestamp = track.timestamp
            if timestamp is not None and timestamp.offset_usability == "qualified":
                values = timestamp.model_dump()
                values.update(
                    origin=f"recording:{media_id}" if availability == "current" else timestamp.origin,
                    offset_usability="qualified" if availability == "current" else "unusable",
                    evidence=(
                        *timestamp.evidence[:62],
                        f"intrinsic-origin:{timestamp.origin}",
                        f"reviewed-file-mapping:{mapping['cue_file_ordinal']}",
                    ),
                )
                track = track.model_copy(update={"timestamp": Timestamp.model_validate(values)})
        result.append(track)
    return tuple(result)


async def get_selected_recording_source(
    session: AsyncSession, media_id: uuid.UUID, *, kind: SourceKind = "tracklist"
) -> SelectedRecordingSource | None:
    """Read pinned authority from DB even if inventory/source is now unavailable."""
    selection = await session.scalar(
        select(ProviderRecordingSelection)
        .where(ProviderRecordingSelection.media_id == media_id, ProviderRecordingSelection.kind == kind)
        .execution_options(populate_existing=True)
    )
    if selection is None:
        return None
    observation = await session.get(ProviderSourceObservation, selection.observation_id)
    source = await session.get(ProviderSourceObject, observation.object_id) if observation else None
    if observation is None or source is None:
        return None
    snapshot = _snapshot(observation)
    availability = await source_availability(session, observation, media_id)
    mapping = selection.target_mapping
    media = await session.scalar(select(FileRecord).where(FileRecord.id == media_id).execution_options(populate_existing=True))
    if media is None or media.missing_at is not None:
        availability = "missing"
    elif media.file_type not in MEDIA_FILE_TYPES:
        availability = "unavailable"
    elif mapping is None or "media_sha256" not in mapping:
        if availability == "current":
            availability = "unknown_binding"
    elif media.sha256_hash != mapping["media_sha256"]:
        availability = "stale"
    if availability == "current" and source.source_file_id is not None:
        source_file = await session.scalar(select(FileRecord).where(FileRecord.id == source.source_file_id).execution_options(populate_existing=True))
        if source_file is None or media is None or source_file.agent_id != media.agent_id:
            availability = "unlinked"
        elif mapping is not None and source_file.sha256_hash != mapping.get("source_sha256"):
            availability = "stale"
    if source.channel == "embedded" and availability == "current":
        metadata = await session.scalar(select(FileMetadata).where(FileMetadata.file_id == media_id).execution_options(populate_existing=True))
        try:
            if (
                metadata is None
                or metadata.failed_at is not None
                or embedded_revision(metadata.raw_tags, source.native_id.split(":", 2)[-1]) != observation.revision
            ):
                availability = "stale"
        except ValueError:
            availability = "stale"
    return SelectedRecordingSource(
        media_id=media_id,
        kind=kind,
        observation_id=observation.id,
        source_object_id=source.id,
        identity=snapshot.identity,
        channel=source.channel,
        selection_token=selection_token(selection),
        actor=selection.actor,
        selected_at=selection.selected_at,
        availability=availability,
        target_mapping=selection.target_mapping,
        snapshot=snapshot,
        tracks=_project_tracks(snapshot, media_id, selection.target_mapping, availability),
    )


async def decide_local_source(session: AsyncSession, command: SourceDecision, *, actor: str) -> SelectedRecordingSource | None:
    """Compare-and-select/reject under current inventory locks; replay cannot resurrect authority."""
    command = SourceDecision.model_validate(command)
    if not actor or len(actor) > 128 or "\0" in actor:
        raise ValueError("Decision requires a bounded actor")
    # Serialize the decision identity before source buckets so cross-target UUID reuse cannot
    # race the unique audit constraint or produce an untyped IntegrityError.
    decision_lock = int.from_bytes(hashlib.sha256(f"local-decision:{command.decision_id}".encode()).digest()[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": decision_lock})
    observation = await session.get(ProviderSourceObservation, command.observation_id)
    source = await session.get(ProviderSourceObject, observation.object_id) if observation else None
    if observation is None or source is None or source.provider_id != "local" or source.source_file_id is None:
        raise ValueError("Decision requires a retained local source")
    identity = SourceIdentity(provider_id=source.provider_id, native_id=source.native_id)
    media, file = await _locked_inventory(session, identity, command.media_id, source.source_file_id)
    if media.sha256_hash != command.expected_media_sha256 or file.sha256_hash != command.expected_source_sha256:
        raise ValueError("Inventory revision changed before decision")
    candidate = await session.scalar(
        select(ProviderRecordingCandidate)
        .where(ProviderRecordingCandidate.media_id == media.id, ProviderRecordingCandidate.observation_id == observation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if candidate is None:
        raise ValueError("Observation is not a candidate for this recording")
    candidate_kind = "release_metadata" if observation.parser_version == RELEASE_PARSER_VERSION else "tracklist"
    if command.kind != candidate_kind:
        raise ValueError("Decision kind does not match this candidate")
    if source.channel == "companion":
        await _live_link(session, file.id, media.id)
    elif source.channel != "embedded" or file.id != media.id:
        raise ValueError("Source is not applicable to this recording")
    availability = await source_availability(session, observation, media.id)
    rejectable_error = command.action == "reject" and availability in {"absent", "unavailable", "retry", "unsupported", "contract_error"}
    if observation.revision != command.expected_revision or (availability != "current" and not rejectable_error):
        raise ValueError("Source revision or availability changed before decision")
    if source.channel == "embedded":
        channel = source.native_id.split(":", 2)[-1]
        metadata = await session.scalar(
            select(FileMetadata).where(FileMetadata.file_id == media.id).with_for_update().execution_options(populate_existing=True)
        )
        if metadata is None or metadata.failed_at is not None or embedded_revision(metadata.raw_tags, channel) != observation.revision:
            raise ValueError("Embedded channel changed before decision")
    current = await session.scalar(
        select(ProviderRecordingSelection)
        .where(ProviderRecordingSelection.media_id == media.id, ProviderRecordingSelection.kind == command.kind)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    payload = {**command.model_dump(mode="json"), "actor": actor}
    previous = await session.scalar(select(ProviderSelectionEvent).where(ProviderSelectionEvent.decision_id == command.decision_id))
    if previous is not None:
        if previous.decision_payload != payload:
            raise ValueError("Decision UUID was reused with different intent")
        if (
            command.action == "select"
            and current is not None
            and current.selection_token == command.decision_id
            and current.observation_id == command.observation_id
            and current.actor == actor
            and current.target_mapping == previous.target_mapping
        ):
            return await get_selected_recording_source(session, media.id, kind=command.kind)
        if (
            command.action == "reject"
            and candidate.status == "rejected"
            and (selection_token(current) if current else None) == command.expected_selection_token
        ):
            return await get_selected_recording_source(session, media.id, kind=command.kind)
        raise ValueError("Decision replay is no longer current")
    if (selection_token(current) if current else None) != command.expected_selection_token:
        raise ValueError("Selected source changed; refresh before reviewing")
    mapping: dict[str, str | int] | None = None
    if command.action == "select":
        if candidate.status == "rejected" or observation.status != "found" or "tracks" not in observation.payload:
            raise ValueError("Only a complete parsed candidate can be selected")
        snapshot = _snapshot(observation)
        mapping = {
            "media_id": str(media.id),
            "media_sha256": media.sha256_hash,
            "source_sha256": file.sha256_hash,
            "source_revision": observation.revision or "unknown",
        }
        if snapshot.completeness.state != "complete":
            raise ValueError("Incomplete candidate cannot be selected")
        if observation.conflict_with_id is not None and not command.accept_conflict:
            raise ValueError("Conflicting source requires explicit acknowledgement")
        if command.kind == "tracklist":
            if not snapshot.tracks or snapshot.parser_version == RELEASE_PARSER_VERSION:
                raise ValueError("Release-only notes cannot be a selected tracklist")
            if snapshot.source_format == "cue":
                if command.cue_file_ordinal is None or not any(_cue_ordinal(track) == command.cue_file_ordinal for track in snapshot.tracks):
                    raise ValueError("CUE requires an explicit intrinsic FILE-to-recording mapping")
                mapping["cue_file_ordinal"] = command.cue_file_ordinal
            elif command.cue_file_ordinal is not None:
                raise ValueError("FILE mapping is only valid for CUE tracklists")
        elif snapshot.parser_version != RELEASE_PARSER_VERSION or command.cue_file_ordinal is not None:
            raise ValueError("Release selection requires release extraction without track mapping")
        elif not snapshot.release_facts:
            raise ValueError("Release notes without structured facts cannot replace reviewed metadata")
        if current is None:
            current = ProviderRecordingSelection(
                media_id=media.id, kind=command.kind, observation_id=observation.id, actor=actor, selected_at=datetime.now(UTC)
            )
            session.add(current)
        current.observation_id = observation.id
        current.actor = actor
        current.selected_at = datetime.now(UTC)
        current.selection_token = command.decision_id
        current.target_mapping = mapping
        current.tracklist_id = None
        current.version_id = None
        candidate.status = "accepted"
    else:
        if current is not None and current.observation_id == observation.id:
            raise ValueError("Rejecting an active selected source requires choosing a replacement")
        candidate.status = "rejected"
    session.add(
        ProviderSelectionEvent(
            media_id=media.id,
            kind=command.kind,
            observation_id=observation.id,
            actor=actor,
            decision_id=command.decision_id,
            action=command.action,
            target_mapping=mapping,
            decision_payload=payload,
        )
    )
    await session.flush()
    return await get_selected_recording_source(session, media.id, kind=command.kind)
