"""Bounded, DB-only companion detail reads with exact-observation membership."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast as type_cast
import uuid

from sqlalchemy import Text, and_, case, cast, exists, func, literal, or_, select, union_all
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
from phaze.schemas.companion_details import (
    CompanionAttemptSummary,
    CompanionDetails,
    CompanionLinkSummary,
    CompanionSourceSummary,
    DetailPage,
    ObservationPage,
    ObservationSummary,
    ReverseMediaLink,
    ReverseMediaPage,
    SelectedAuthority,
    StoredObservationDetail,
    StoredTextChunk,
    StoredTextRequest,
)
from phaze.services.companion_content import MEDIA_FILE_TYPES
from phaze.services.local_source_import import embedded_revision, get_selected_recording_source, selection_token
from phaze.tracklist_providers.domain import ProviderTrack, ReleaseFact


if TYPE_CHECKING:
    from phaze.schemas.local_source_import import SourceKind


AttemptReader = Callable[[AsyncSession, Sequence[uuid.UUID]], Awaitable[Mapping[uuid.UUID, object]]]
Observation = ProviderSourceObservation
Source = ProviderSourceObject


def _array(value: Any) -> Any:
    return case((func.jsonb_typeof(value) == "array", value), else_=cast(literal("[]"), JSONB))


def _current_owner(file_id: Any) -> Any:
    inventory = aliased(FileRecord)
    return select(inventory.agent_id).where(inventory.id == file_id).correlate(FileRecord).scalar_subquery()


def _live_source(media: FileRecord | type[FileRecord]) -> Any:
    owner = aliased(FileRecord)
    owner_ok = exists(select(owner.id).where(owner.id == Source.source_file_id, owner.agent_id == _current_owner(media.id)))
    linked = exists(select(FileCompanion.id).where(FileCompanion.media_id == media.id, FileCompanion.companion_id == Source.source_file_id))
    return and_(
        owner_ok,
        or_(
            and_(Source.channel == "companion", or_(linked, Source.source_file_id == media.id)),
            and_(Source.channel == "embedded", Source.source_file_id == media.id),
        ),
    )


def _authorized(media: FileRecord | type[FileRecord]) -> Any:
    # Historical permission is observation-specific, never a grant to all later object content.
    candidate = exists(
        select(ProviderRecordingCandidate.id)
        .correlate_except(ProviderRecordingCandidate)
        .where(ProviderRecordingCandidate.media_id == media.id, ProviderRecordingCandidate.observation_id == Observation.id)
    )
    selected = exists(
        select(ProviderRecordingSelection.media_id)
        .correlate_except(ProviderRecordingSelection)
        .where(ProviderRecordingSelection.media_id == media.id, ProviderRecordingSelection.observation_id == Observation.id)
    )
    event = exists(
        select(ProviderSelectionEvent.id).where(ProviderSelectionEvent.media_id == media.id, ProviderSelectionEvent.observation_id == Observation.id)
    )
    return or_(_live_source(media), candidate, selected, event)


async def _file(session: AsyncSession, file_id: uuid.UUID) -> FileRecord | None:
    return await session.scalar(select(FileRecord).where(FileRecord.id == file_id).execution_options(populate_existing=True))


def _inventory(missing: Any, ambiguous: Any, source_id: Any, same_owner: bool = True) -> str:
    if source_id is None or missing is not None:
        return "missing"
    if not same_owner:
        return "unavailable"
    return "ambiguous" if ambiguous is not None else "unverified"


def _observation_query(media: FileRecord | type[FileRecord]) -> Any:
    source_file = aliased(FileRecord)
    linked = exists(select(FileCompanion.id).where(FileCompanion.media_id == media.id, FileCompanion.companion_id == Source.source_file_id))
    candidate_status = (
        select(ProviderRecordingCandidate.status)
        .correlate_except(ProviderRecordingCandidate)
        .where(ProviderRecordingCandidate.media_id == media.id, ProviderRecordingCandidate.observation_id == Observation.id)
        .scalar_subquery()
    )
    marker = func.jsonb_path_query_first(Observation.payload, cast('$.provenance[*] ? (@ like_regex "^capture:inventory_sha256:")', JSONPATH))
    read_marker = func.jsonb_path_query_first(Observation.payload, cast('$.evidence[*] ? (@ like_regex "^capture:inventory_sha256:")', JSONPATH))
    return (
        select(
            Observation.id.label("observation_id"),
            Observation.object_id.label("source_object_id"),
            Observation.status,
            Observation.code,
            Observation.revision,
            Observation.revision_scope,
            Observation.parser_version,
            Observation.retrieved_at,
            Observation.encoding,
            Observation.truncated,
            func.substring(Observation.decoded_text, 1, 512).label("text_preview"),
            func.coalesce(func.char_length(cast(Observation.decoded_text, Text)), 0).label("text_length"),
            func.jsonb_array_length(_array(Observation.payload["tracks"])).label("track_count"),
            func.jsonb_array_length(_array(Observation.payload["release_facts"])).label("release_fact_count"),
            Observation.payload["completeness"]["state"].as_string().label("completeness_state"),
            func.substring(Observation.payload["completeness"]["reason"].as_string(), 1, 512).label("completeness_reason"),
            Observation.parent_id,
            Observation.conflict_with_id,
            Source.channel,
            Source.native_id,
            Source.source_file_id,
            source_file.agent_id.label("source_agent"),
            source_file.missing_at,
            source_file.companion_ambiguous_at,
            source_file.sha256_hash,
            linked.label("linked"),
            candidate_status.label("candidate_status"),
            or_(
                Observation.payload["provenance"].contains(["revision:sha256:full"]),
                Observation.payload["evidence"].contains(["revision:sha256:full"]),
            ).label("full_sha_evidence"),
            func.coalesce(marker, read_marker).label("inventory_marker"),
        )
        .join(Source, Source.id == Observation.object_id)
        .outerjoin(source_file, source_file.id == Source.source_file_id)
        .where(_authorized(media))
    )


class MediaIdentity(Protocol):
    @property
    def id(self) -> uuid.UUID: ...

    @property
    def agent_id(self) -> str: ...


def _summary(row: Mapping[Any, Any], media: MediaIdentity, authorities: Sequence[SelectedAuthority]) -> ObservationSummary:
    channel = row["channel"]
    if channel is None:
        availability = "current"
    elif row["source_file_id"] is None or row["source_agent"] is None or row["missing_at"] is not None:
        availability = "missing"
    elif row["source_agent"] != media.agent_id:
        availability = "unavailable"
    elif row["companion_ambiguous_at"] is not None:
        availability = "ambiguous"
    elif (channel == "companion" and not row["linked"] and row["source_file_id"] != media.id) or (
        channel == "embedded" and row["source_file_id"] != media.id
    ):
        availability = "unlinked"
    else:
        availability = "current"
    marker = row["inventory_marker"]
    revision = row["revision"]
    if (
        availability == "current"
        and channel == "companion"
        and (
            (isinstance(marker, str) and marker.removeprefix("capture:inventory_sha256:") != row["sha256_hash"])
            or (
                row["full_sha_evidence"]
                and row["revision_scope"] == "full"
                and revision is not None
                and len(revision) == 64
                and all(c in "0123456789abcdef" for c in revision)
                and revision != row["sha256_hash"]
            )
        )
    ):
        availability = "stale"
    if availability == "current" and channel == "embedded" and not row.get("embedded_current", False):
        availability = "stale"
    if availability == "current" and row["status"] not in {"found", "incomplete", "ambiguous"}:
        availability = row["status"]
    fields = {key: row[key] for key in ObservationSummary.model_fields if key in row}
    fields.update(availability=availability, selected_kinds=tuple(a.kind for a in authorities if a.observation_id == row["observation_id"]))
    return ObservationSummary.model_validate(fields)


async def _embedded_freshness(session: AsyncSession, media: FileRecord, rows: Sequence[Mapping[Any, Any]]) -> list[dict[Any, Any]]:
    """Fetch only exact descriptive channels, with a SQL byte cap before materialization."""
    channels = sorted({row["native_id"].split(":", 2)[-1] for row in rows if row["channel"] == "embedded"})
    revisions: dict[str, str | None] = {}
    if channels:
        columns = []
        for channel in channels:
            value = FileMetadata.raw_tags[channel]
            # JSON serialization of bounded Unicode text/list may expand escapes. Cap it independently.
            columns.append(case((func.octet_length(cast(value, Text)) <= 2097152, value), else_=None))
        metadata = (await session.execute(select(FileMetadata.failed_at, *columns).where(FileMetadata.file_id == media.id))).one_or_none()
        for index, channel in enumerate(channels):
            try:
                revisions[channel] = (
                    embedded_revision({channel: metadata[index + 1]}, channel) if metadata is not None and metadata[0] is None else None
                )
            except ValueError:
                revisions[channel] = None
    return [
        dict(row, embedded_current=row["revision"] == revisions.get(row["native_id"].split(":", 2)[-1]) and row["revision"] is not None)
        for row in rows
    ]


async def _authorities(session: AsyncSession, media: FileRecord) -> tuple[SelectedAuthority, ...]:
    selections = (
        await session.scalars(
            select(ProviderRecordingSelection)
            .where(ProviderRecordingSelection.media_id == media.id, ProviderRecordingSelection.kind.in_(("tracklist", "release_metadata")))
            .order_by(ProviderRecordingSelection.kind)
        )
    ).all()
    if not selections:
        return ()
    rows: Sequence[Mapping[Any, Any]] = (
        (await session.execute(_observation_query(media).where(Observation.id.in_([selection.observation_id for selection in selections]))))
        .mappings()
        .all()
    )
    rows = await _embedded_freshness(session, media, rows)
    by_id = {row["observation_id"]: row for row in rows}
    result = []
    for selection in selections:
        row = by_id.get(selection.observation_id)
        if row is None:
            continue
        availability = _summary(row, media, ()).availability
        mapping = selection.target_mapping
        if media.missing_at is not None:
            availability = "missing"
        elif media.file_type not in MEDIA_FILE_TYPES:
            availability = "unavailable"
        elif mapping is None or "media_sha256" not in mapping:
            if availability == "current":
                availability = "unknown_binding"
        elif media.sha256_hash != mapping["media_sha256"]:
            availability = "stale"
        if (
            availability == "current"
            and row["source_file_id"] is not None
            and mapping is not None
            and row["sha256_hash"] != mapping.get("source_sha256")
        ):
            availability = "stale"
        result.append(
            SelectedAuthority(
                kind=type_cast("SourceKind", selection.kind),
                observation_id=selection.observation_id,
                source_object_id=row["source_object_id"],
                selection_token=selection_token(selection),
                actor=selection.actor,
                selected_at=selection.selected_at,
                availability=availability,
                target_mapping=mapping,
            )
        )
    return tuple(result)


def _next(page: DetailPage, rows: Sequence[Any]) -> int | None:
    return page.offset + page.limit if len(rows) > page.limit else None


def _source_ids(media: FileRecord) -> Any:
    owner = aliased(FileRecord)
    current = (
        select(Source.id)
        .join(owner, owner.id == Source.source_file_id)
        .where(
            owner.agent_id == _current_owner(media.id),
            Source.channel.in_(("companion", "embedded")),
            or_(
                Source.source_file_id == media.id,
                Source.source_file_id.in_(select(FileCompanion.companion_id).where(FileCompanion.media_id == media.id)),
            ),
        )
    )
    candidate = (
        select(Observation.object_id)
        .join(ProviderRecordingCandidate, ProviderRecordingCandidate.observation_id == Observation.id)
        .where(ProviderRecordingCandidate.media_id == media.id)
    )
    selected = (
        select(Observation.object_id)
        .join(ProviderRecordingSelection, ProviderRecordingSelection.observation_id == Observation.id)
        .where(ProviderRecordingSelection.media_id == media.id)
    )
    events = (
        select(Observation.object_id)
        .join(ProviderSelectionEvent, ProviderSelectionEvent.observation_id == Observation.id)
        .where(ProviderSelectionEvent.media_id == media.id)
    )
    return union_all(current, candidate, selected, events)


async def get_companion_details(
    session: AsyncSession,
    file_id: uuid.UUID,
    *,
    link_page: DetailPage | None = None,
    source_page: DetailPage | None = None,
    attempt_reader: AttemptReader | None = None,
) -> CompanionDetails | None:
    """Inventory and retained content are separate from received attempts and reviewed authority."""
    link_page = DetailPage.model_validate(link_page or DetailPage())
    source_page = DetailPage.model_validate(source_page or DetailPage())
    media = await _file(session, file_id)
    if media is None:
        return None
    companion = aliased(FileRecord)
    links = (
        await session.execute(
            select(FileCompanion, companion)
            .join(companion, companion.id == FileCompanion.companion_id)
            .where(FileCompanion.media_id == file_id, companion.agent_id == _current_owner(media.id))
            .order_by(companion.id, FileCompanion.id)
            .offset(link_page.offset)
            .limit(link_page.limit + 1)
        )
    ).all()
    link_summaries = tuple(
        CompanionLinkSummary(
            link_id=link.id,
            file_id=file.id,
            filename=file.original_filename,
            file_type=file.file_type,
            location=file.current_path,
            availability=_inventory(file.missing_at, file.companion_ambiguous_at, file.id),
            derivation_method=link.derivation_method or "historical_unknown",
            derivation_revision=link.derivation_revision,
            derivation_evidence=tuple((link.derivation_evidence or [])[:64]),
            derived_at=link.derived_at,
        )
        for link, file in links[: link_page.limit]
    )
    source_file = aliased(FileRecord)
    sources = (
        await session.execute(
            select(Source, source_file)
            .outerjoin(source_file, source_file.id == Source.source_file_id)
            .where(Source.id.in_(_source_ids(media)))
            .order_by(Source.id)
            .offset(source_page.offset)
            .limit(source_page.limit + 1)
        )
    ).all()
    source_ids = [source.id for source, _file_row in sources[: source_page.limit]]
    authorities = await _authorities(session, media)
    content: dict[uuid.UUID, ObservationSummary] = {}
    if source_ids:
        # PostgreSQL DISTINCT ON returns one authorized content observation per source, not all payload history.
        rows: Sequence[Mapping[Any, Any]] = (
            (
                await session.execute(
                    _observation_query(media)
                    .where(Observation.object_id.in_(source_ids))
                    .distinct(Observation.object_id)
                    .order_by(Observation.object_id, Observation.retrieved_at.desc(), Observation.id.desc())
                )
            )
            .mappings()
            .all()
        )
        rows = await _embedded_freshness(session, media, rows)
        content = {row["source_object_id"]: _summary(row, media, authorities) for row in rows}
    attempts = await attempt_reader(session, source_ids) if attempt_reader is not None and source_ids else {}
    normalized_attempts = {key: CompanionAttemptSummary.model_validate(value) for key, value in attempts.items() if key in source_ids}
    attempt_observations = [value.observation_id for value in normalized_attempts.values()]
    allowed_attempts: set[tuple[uuid.UUID, uuid.UUID]] = set()
    if attempt_observations:
        allowed_attempts = set(
            (
                await session.execute(
                    select(Observation.id, Observation.object_id)
                    .join(Source, Source.id == Observation.object_id)
                    .where(Observation.id.in_(attempt_observations), _authorized(media))
                )
            )
            .tuples()
            .all()
        )
    source_summaries = []
    for source, file in sources[: source_page.limit]:
        own_file = file if file is not None and file.agent_id == media.agent_id else None
        attempt = normalized_attempts.get(source.id)
        # An attempt's semantic observation must itself be authorized, even for historical objects.
        authorized_attempt = attempt is not None and attempt.source_object_id == source.id and (attempt.observation_id, source.id) in allowed_attempts
        source_summaries.append(
            CompanionSourceSummary(
                source_object_id=source.id,
                provider_id=source.provider_id,
                native_id=source.native_id,
                channel=source.channel,
                source_file_id=source.source_file_id,
                original_file_id=source.original_file_id,
                filename=own_file.original_filename if own_file else None,
                file_type=own_file.file_type if own_file else None,
                location=own_file.current_path if own_file else None,
                inventory_availability=_inventory(
                    file.missing_at if file else None, file.companion_ambiguous_at if file else None, source.source_file_id, own_file is not None
                ),
                latest_stored_content=content.get(source.id),
                latest_received_attempt=CompanionAttemptSummary.model_validate(attempt) if authorized_attempt else None,
                attempt_history="recorded" if authorized_attempt else "unknown",
            )
        )
    return CompanionDetails(
        file_id=file_id,
        links=link_summaries,
        sources=tuple(source_summaries),
        selected=authorities,
        next_link_offset=_next(link_page, links),
        next_source_offset=_next(source_page, sources),
    )


async def get_source_observations(
    session: AsyncSession, file_id: uuid.UUID, source_object_id: uuid.UUID, *, page: DetailPage | None = None
) -> ObservationPage | None:
    page = DetailPage.model_validate(page or DetailPage())
    media = await _file(session, file_id)
    if media is None:
        return None
    rows: Sequence[Mapping[Any, Any]] = (
        (
            await session.execute(
                _observation_query(media)
                .where(Observation.object_id == source_object_id)
                .order_by(Observation.retrieved_at.desc(), Observation.id.desc())
                .offset(page.offset)
                .limit(page.limit + 1)
            )
        )
        .mappings()
        .all()
    )
    # A source with no captured content is not an observation-history permission grant.
    retained = exists(select(Observation.id).where(Observation.object_id == Source.id, _authorized(media)))
    if not rows and not await session.scalar(select(Source.id).where(Source.id == source_object_id, or_(_live_source(media), retained))):
        return None
    rows = await _embedded_freshness(session, media, rows)
    authorities = await _authorities(session, media)
    return ObservationPage(
        file_id=file_id,
        source_object_id=source_object_id,
        observations=tuple(_summary(row, media, authorities) for row in rows[: page.limit]),
        next_offset=_next(page, rows),
    )


async def get_stored_observation(
    session: AsyncSession, file_id: uuid.UUID, observation_id: uuid.UUID, *, track_page: DetailPage | None = None, fact_page: DetailPage | None = None
) -> StoredObservationDetail | None:
    track_page = DetailPage.model_validate(track_page or DetailPage())
    fact_page = DetailPage.model_validate(fact_page or DetailPage())
    media = await _file(session, file_id)
    if media is None:
        return None
    row: Mapping[Any, Any] | None = (
        (await session.execute(_observation_query(media).where(Observation.id == observation_id))).mappings().one_or_none()
    )
    if row is None:
        return None
    row = (await _embedded_freshness(session, media, [row]))[0]
    authorities = await _authorities(session, media)
    tracks_path = f"$.tracks[{track_page.offset} to {track_page.offset + track_page.limit}]"
    facts_path = f"$.release_facts[{fact_page.offset} to {fact_page.offset + fact_page.limit}]"
    payload = (
        await session.execute(
            select(
                func.jsonb_path_query_array(Observation.payload, cast(tracks_path, JSONPATH)),
                func.jsonb_path_query_array(Observation.payload, cast(facts_path, JSONPATH)),
                Observation.payload["completeness"]["evidence"],
                Observation.payload["provenance"],
            )
            .join(Source, Source.id == Observation.object_id)
            .where(Observation.id == observation_id, _authorized(media))
        )
    ).one_or_none()
    if payload is None:
        return None
    tracks = tuple(ProviderTrack.model_validate(item) for item in payload[0])
    facts = tuple(ReleaseFact.model_validate(item) for item in payload[1])
    selected_tracks: tuple[ProviderTrack, ...] = ()
    selected_track_count = 0
    next_selected_track_offset = None
    selected_availability = None
    mapping = None
    for authority in authorities:
        if authority.kind == "tracklist" and authority.observation_id == observation_id:
            selected = await get_selected_recording_source(session, file_id, kind=authority.kind)
            if selected is not None:
                selected_tracks = selected.tracks[track_page.offset : track_page.offset + track_page.limit]
                selected_track_count = len(selected.tracks)
                end = track_page.offset + len(selected_tracks)
                next_selected_track_offset = end if end < selected_track_count else None
                selected_availability = selected.availability
                mapping = selected.target_mapping
    return StoredObservationDetail(
        file_id=file_id,
        summary=_summary(row, media, authorities),
        tracks=tracks[: track_page.limit],
        release_facts=facts[: fact_page.limit],
        evidence=tuple((payload[2] or [])[:64]) + tuple((payload[3] or [])[:64]),
        next_track_offset=_next(track_page, tracks),
        next_fact_offset=_next(fact_page, facts),
        selected_tracks=selected_tracks,
        selected_track_count=selected_track_count,
        next_selected_track_offset=next_selected_track_offset,
        selected_availability=selected_availability,
        target_mapping=mapping,
    )


async def get_stored_text(
    session: AsyncSession, file_id: uuid.UUID, observation_id: uuid.UUID, *, chunk: StoredTextRequest | None = None
) -> StoredTextChunk | None:
    chunk = StoredTextRequest.model_validate(chunk or StoredTextRequest())
    media = await _file(session, file_id)
    if media is None:
        return None
    row = (
        await session.execute(
            select(
                func.substring(Observation.decoded_text, chunk.offset + 1, chunk.length),
                Observation.encoding,
                Observation.truncated,
                func.coalesce(func.char_length(cast(Observation.decoded_text, Text)), 0),
            )
            .join(Source, Source.id == Observation.object_id)
            .where(Observation.id == observation_id, _authorized(media))
        )
    ).one_or_none()
    if row is None:
        return None
    text, encoding, truncated, length = row
    end = chunk.offset + len(text or "")
    return StoredTextChunk(
        file_id=file_id,
        observation_id=observation_id,
        text=text,
        encoding=encoding,
        source_truncated=truncated,
        offset=chunk.offset,
        total_characters=length,
        next_offset=end if end < length else None,
    )


async def get_companion_media(session: AsyncSession, companion_id: uuid.UUID, *, page: DetailPage | None = None) -> ReverseMediaPage | None:
    page = DetailPage.model_validate(page or DetailPage())
    companion = await _file(session, companion_id)
    if companion is None:
        return None
    rows = (
        await session.execute(
            select(FileCompanion.id, FileRecord.id, FileRecord.original_filename, FileRecord.file_type, FileRecord.current_path)
            .join(FileCompanion, FileCompanion.media_id == FileRecord.id)
            .where(FileCompanion.companion_id == companion_id, FileRecord.agent_id == _current_owner(companion_id))
            .order_by(FileRecord.id, FileCompanion.id)
            .offset(page.offset)
            .limit(page.limit + 1)
        )
    ).all()
    return ReverseMediaPage(
        companion_id=companion_id,
        media=tuple(
            ReverseMediaLink(link_id=link_id, media_id=media_id, filename=name, file_type=kind, location=path)
            for link_id, media_id, name, kind, path in rows[: page.limit]
        ),
        next_offset=_next(page, rows),
    )
