"""Transactional provider storage. Callers own transactions and approval policy.

Lock order: provider/native digest bucket, source object, recording, canonical tracklist.
Digest indexes accelerate lookup only; complete opaque identity and payload determine equality.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

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
from phaze.tracklist_providers.domain import LoadOutcome, OutcomeStatus, Snapshot, SourceIdentity, SourceRead


MAX_NATIVE_BYTES = 16384
MAX_REVISION_BYTES = 8192
MAX_TEXT_BYTES = 1048576
MAX_PAYLOAD_BYTES = 4194304


@dataclass(frozen=True)
class StoredObservation:
    source: ProviderSourceObject
    observation: ProviderSourceObservation
    reused: bool
    conflict: bool


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _payload_bytes(payload: dict[str, object]) -> bytes:
    _reject_nul(payload)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _reject_nul(value: object) -> None:
    if isinstance(value, str) and "\0" in value:
        raise ValueError("Payload contains NUL")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nul(key)
            _reject_nul(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_nul(item)


def _bound(value: str | None, maximum: int, label: str) -> None:
    if value is not None and ("\0" in value or len(value.encode("utf-8")) > maximum):
        raise ValueError(f"{label} exceeds its storage bound or contains NUL")


async def resolve_source(
    session: AsyncSession,
    identity: SourceIdentity,
    *,
    source_file_id: uuid.UUID | None = None,
    channel: str | None = None,
) -> ProviderSourceObject:
    """Resolve full UTF-8 identity under a digest-bucket advisory lock; never commit."""
    identity = SourceIdentity.model_validate(identity)
    _bound(identity.native_id, MAX_NATIVE_BYTES, "native identity")
    if channel not in {None, "companion", "embedded"} or (source_file_id is None) != (channel is None):
        raise ValueError("Local source binding requires a file and companion/embedded channel")
    raw_key = identity.provider_id.encode() + b"\0" + identity.native_id.encode("utf-8")
    digest = _digest(raw_key)
    # Hash-lock collisions merely serialize unrelated buckets; equality never uses the lock key.
    lock_key = int.from_bytes(hashlib.sha256(identity.provider_id.encode() + digest.encode()).digest()[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    rows = (
        await session.scalars(
            select(ProviderSourceObject)
            .where(
                ProviderSourceObject.provider_id == identity.provider_id,
                ProviderSourceObject.native_digest == digest,
            )
            .with_for_update()
        )
    ).all()
    for row in rows:
        if row.native_id.encode("utf-8") == identity.native_id.encode("utf-8"):
            if row.original_file_id != source_file_id or row.channel != channel:
                raise ValueError("Source identity cannot be rebound to another file or channel")
            return row
    row = ProviderSourceObject(
        provider_id=identity.provider_id,
        native_id=identity.native_id,
        native_digest=digest,
        source_file_id=source_file_id,
        original_file_id=source_file_id,
        channel=channel,
    )
    session.add(row)
    await session.flush()
    return row


async def _store(
    session: AsyncSession,
    identity: SourceIdentity,
    payload: dict[str, object],
    *,
    status: str,
    code: str,
    revision: str | None,
    revision_scope: str,
    parser_version: str,
    decoded_text: str | None,
    encoding: str | None,
    truncated: bool,
    retrieved_at: datetime,
    source_url: str | None,
    source_file_id: uuid.UUID | None,
    channel: str | None,
    parent_id: uuid.UUID | None,
    snapshot: bool,
) -> StoredObservation:
    _bound(revision, MAX_REVISION_BYTES, "revision")
    _bound(decoded_text, MAX_TEXT_BYTES, "decoded text")
    _bound(parser_version, 512, "parser version")
    serialized = _payload_bytes(payload)
    if len(serialized) > MAX_PAYLOAD_BYTES:
        raise ValueError("Normalized payload exceeds 4MiB")
    source = await resolve_source(session, identity, source_file_id=source_file_id, channel=channel)
    if parent_id is not None:
        parent = await session.get(ProviderSourceObservation, parent_id)
        if parent is None or parent.object_id != source.id:
            raise ValueError("Parent observation must belong to this source")
    rows = (
        await session.scalars(
            select(ProviderSourceObservation)
            .where(
                ProviderSourceObservation.object_id == source.id,
            )
            .order_by(ProviderSourceObservation.retrieved_at, ProviderSourceObservation.id)
        )
    ).all()
    conflict_with: uuid.UUID | None = None
    for row in rows:
        same_revision = row.revision == revision and row.revision_scope == revision_scope
        if (
            snapshot
            and same_revision
            and revision is not None
            and revision_scope == "full"
            and "tracks" in row.payload
            and row.conflict_with_id is None
        ):
            if row.decoded_text != decoded_text:
                conflict_with = row.id
            elif row.parser_version != parser_version and parent_id is None:
                parent_id = row.id
        if row.parser_version != parser_version or not same_revision or row.status != status:
            continue
        if row.payload == payload:
            return StoredObservation(source, row, True, row.conflict_with_id is not None)
        if snapshot and revision is not None and revision_scope == "full" and "tracks" in row.payload and row.conflict_with_id is None:
            conflict_with = row.id
    observation = ProviderSourceObservation(
        object_id=source.id,
        status=status,
        code=code,
        revision=revision,
        revision_scope=revision_scope,
        parser_version=parser_version,
        content_digest=_digest(serialized),
        payload=payload,
        decoded_text=decoded_text,
        encoding=encoding,
        truncated=truncated,
        source_url=source_url,
        retrieved_at=retrieved_at,
        parent_id=parent_id,
        conflict_with_id=conflict_with,
    )
    session.add(observation)
    await session.flush()
    return StoredObservation(source, observation, False, conflict_with is not None)


async def store_snapshot(
    session: AsyncSession,
    snapshot: Snapshot,
    *,
    status: str | None = None,
    source_file_id: uuid.UUID | None = None,
    channel: str | None = None,
    parent_id: uuid.UUID | None = None,
) -> StoredObservation:
    """Append/reuse source content without changing candidates, selections or legacy pointers."""
    snapshot = Snapshot.model_validate(snapshot)
    status = status or ("found" if snapshot.completeness.state == "complete" else "incomplete")
    if status not in {"found", "incomplete", "ambiguous"} or (status == "found" and snapshot.completeness.state != "complete"):
        raise ValueError("Snapshot status is incompatible with completeness")
    LoadOutcome(status=OutcomeStatus(status), code="snapshot", scope="source", snapshot=snapshot)
    return await _store(
        session,
        snapshot.identity,
        snapshot.normalized_payload(),
        status=status,
        code="snapshot",
        revision=snapshot.revision,
        revision_scope=snapshot.revision_scope,
        parser_version=snapshot.parser_version,
        decoded_text=snapshot.text,
        encoding=snapshot.encoding,
        truncated=False,
        retrieved_at=snapshot.retrieved_at,
        source_url=snapshot.url,
        source_file_id=source_file_id,
        channel=channel,
        parent_id=parent_id,
        snapshot=True,
    )


async def store_source_read(
    session: AsyncSession,
    identity: SourceIdentity,
    read: SourceRead,
    *,
    parser_version: str,
    source_file_id: uuid.UUID | None = None,
    channel: str | None = None,
) -> StoredObservation:
    """Retain bounded read/failure evidence; failures never fabricate empty parsed snapshots."""
    read = SourceRead.model_validate(read)
    if not parser_version or len(parser_version) > 128:
        raise ValueError("Parser version must contain 1..128 characters")
    payload = read.model_dump(mode="json", exclude={"retrieved_at"})
    return await _store(
        session,
        identity,
        payload,
        status=read.status.value,
        code=read.code,
        revision=read.revision,
        revision_scope=read.revision_scope,
        parser_version=parser_version,
        decoded_text=read.text,
        encoding=read.encoding,
        truncated=read.truncated,
        retrieved_at=read.retrieved_at,
        source_url=None,
        source_file_id=source_file_id,
        channel=channel,
        parent_id=None,
        snapshot=False,
    )


async def add_candidate(
    session: AsyncSession,
    media_id: uuid.UUID,
    observation_id: uuid.UUID,
    *,
    evidence: tuple[str, ...] = (),
) -> ProviderRecordingCandidate:
    """Idempotently record target-specific pending evidence, never implicit authority."""
    if len(evidence) > 64 or any(len(value) > 4096 for value in evidence):
        raise ValueError("Candidate evidence exceeds contract bounds")
    statement = (
        insert(ProviderRecordingCandidate)
        .values(
            id=uuid.uuid4(),
            media_id=media_id,
            observation_id=observation_id,
            status="pending",
            evidence=list(evidence),
        )
        .on_conflict_do_nothing(index_elements=["media_id", "observation_id"])
    )
    await session.execute(statement)
    return (
        await session.scalars(
            select(ProviderRecordingCandidate).where(
                ProviderRecordingCandidate.media_id == media_id,
                ProviderRecordingCandidate.observation_id == observation_id,
            )
        )
    ).one()


async def source_availability(session: AsyncSession, observation: ProviderSourceObservation, media_id: uuid.UUID) -> str:
    """Current availability is separate from retained reviewed history; perform no I/O reads."""
    source = await session.scalar(
        select(ProviderSourceObject).where(ProviderSourceObject.id == observation.object_id).execution_options(populate_existing=True)
    )
    if source is None:
        return "missing"
    if source.channel is None:
        return "current" if observation.status in {"found", "incomplete", "ambiguous"} else observation.status
    file = (
        await session.scalar(select(FileRecord).where(FileRecord.id == source.source_file_id).execution_options(populate_existing=True))
        if source.source_file_id is not None
        else None
    )
    if file is None or file.missing_at is not None:
        return "missing"
    if file.companion_ambiguous_at is not None:
        return "ambiguous"
    if source.channel == "embedded":
        if file.id != media_id:
            return "unlinked"
    elif not await session.scalar(select(FileCompanion.id).where(FileCompanion.companion_id == file.id, FileCompanion.media_id == media_id)):
        return "unlinked"
    if (
        source.channel == "companion"
        and "revision:sha256:full" in observation.payload.get("provenance", observation.payload.get("evidence", []))
        and observation.revision_scope == "full"
        and observation.revision is not None
        and len(observation.revision) == 64
        and all(character in "0123456789abcdef" for character in observation.revision)
        and file.sha256_hash is not None
        and file.sha256_hash != observation.revision
    ):
        return "stale"
    if observation.status not in {"found", "incomplete", "ambiguous"}:
        return observation.status
    return "current"


async def select_observation(
    session: AsyncSession,
    media_id: uuid.UUID,
    observation_id: uuid.UUID,
    *,
    kind: str,
    actor: str,
    accept_conflict: bool = False,
) -> ProviderRecordingSelection:
    """Record an explicit reviewed choice; do not project or overwrite legacy tracklists."""
    if kind not in {"tracklist", "release_metadata"} or not actor or len(actor) > 128:
        raise ValueError("Selection requires a known kind and bounded nonempty actor")
    observation = await session.get(ProviderSourceObservation, observation_id)
    candidate = await session.scalar(
        select(ProviderRecordingCandidate.id).where(
            ProviderRecordingCandidate.media_id == media_id,
            ProviderRecordingCandidate.observation_id == observation_id,
        )
    )
    if observation is None or candidate is None or observation.status != "found" or "tracks" not in observation.payload:
        raise ValueError("Only a complete parsed candidate can be selected")
    if observation.conflict_with_id is not None and not accept_conflict:
        raise ValueError("Conflicting source requires explicit acknowledgement")
    source = await session.scalar(
        select(ProviderSourceObject).where(ProviderSourceObject.id == observation.object_id).execution_options(populate_existing=True)
    )
    file_ids = {media_id}
    if source is not None and source.source_file_id is not None:
        file_ids.add(source.source_file_id)
    # Match scan_deletion's global original_path ordering before touching companion pairs.
    locked_files = (
        await session.scalars(
            select(FileRecord)
            .where(FileRecord.id.in_(file_ids))
            .order_by(FileRecord.original_path)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    if {file.id for file in locked_files} != file_ids:
        raise ValueError("Source or recording disappeared before selection")
    if source is not None and source.source_file_id is not None:
        await session.execute(
            select(FileCompanion.id)
            .where(
                FileCompanion.companion_id == source.source_file_id,
                FileCompanion.media_id == media_id,
            )
            .with_for_update(read=True)
        )
    if await source_availability(session, observation, media_id) != "current":
        raise ValueError("Source is not currently available for this target")
    session.add(ProviderSelectionEvent(media_id=media_id, kind=kind, observation_id=observation_id, actor=actor))
    candidate_row = (await session.scalars(select(ProviderRecordingCandidate).where(ProviderRecordingCandidate.id == candidate))).one()
    candidate_row.status = "accepted"
    await session.flush()
    statement = (
        insert(ProviderRecordingSelection)
        .values(
            media_id=media_id,
            kind=kind,
            observation_id=observation_id,
            actor=actor,
            selected_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            index_elements=["media_id", "kind"],
            set_={
                "observation_id": observation_id,
                "actor": actor,
                "selected_at": datetime.now(UTC),
                "tracklist_id": None,
                "version_id": None,
            },
        )
    )
    await session.execute(statement)
    return (
        await session.scalars(
            select(ProviderRecordingSelection)
            .where(
                ProviderRecordingSelection.media_id == media_id,
                ProviderRecordingSelection.kind == kind,
            )
            .execution_options(populate_existing=True)
        )
    ).one()


async def backfill_legacy_identities(session: AsyncSession, *, batch_size: int = 100) -> int:
    """Resumable identity expansion; preserve legacy content, links and unknown sources exactly."""
    if not 1 <= batch_size <= 1000:
        raise ValueError("Legacy identity batch must contain 1..1000 rows")
    rows = (
        await session.scalars(
            select(Tracklist).where(Tracklist.provider_object_id.is_(None), Tracklist.is_canonical()).order_by(Tracklist.id).limit(batch_size)
        )
    ).all()
    for row in rows:
        # Historical namespaces are inert, never an adapter activation or guessed provider mapping.
        namespace = "legacy_" + hashlib.sha256(row.source.encode()).hexdigest()[:48]
        native_id = f"manual:{row.id}" if row.source == "manual" else row.external_id
        source = await resolve_source(session, SourceIdentity(provider_id=namespace, native_id=native_id))
        await session.execute(select(Tracklist.id).where(Tracklist.id == row.id).with_for_update())
        row.provider_object_id = source.id
        await session.flush()
        projections = (
            await session.scalars(
                select(Tracklist).where(
                    Tracklist.provider_object_id.is_(None),
                    Tracklist.is_propagated(),
                    Tracklist.external_id == row.external_id,
                    Tracklist.source == row.source,
                )
            )
        ).all()
        for projection in projections:
            projection.provider_object_id = source.id
    await session.flush()
    return len(rows)
