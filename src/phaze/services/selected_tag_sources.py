"""Bounded reviewed tag facts, distinct from legacy Tracklist persistence."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import Text, and_, case, cast, func, or_, select
from sqlalchemy.orm import aliased

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import ProviderRecordingSelection, ProviderSourceObject, ProviderSourceObservation
from phaze.services.companion_content import MEDIA_FILE_TYPES
from phaze.services.companion_details import _observation_query, _summary
from phaze.services.local_source_import import embedded_revision, selection_token
from phaze.services.selected_source_consumers import tag_release_projection


if TYPE_CHECKING:
    from datetime import datetime
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.tracklist import Tracklist
    from phaze.schemas.local_source_import import SourceKind


@dataclass(frozen=True)
class CurrentMediaBinding:
    id: uuid.UUID
    agent_id: str
    sha256_hash: str
    file_type: str
    missing_at: datetime | None


@dataclass(frozen=True)
class SelectedTagSource:
    media_id: uuid.UUID
    tags: dict[str, str | int]
    evidence: tuple[dict[str, Any], ...]
    track_observation_id: uuid.UUID | None
    legacy: Tracklist | None = None
    eligible: bool = True

    @property
    def latest_version_id(self) -> uuid.UUID | None:
        return self.legacy.latest_version_id if self.legacy is not None else None


async def validate_selected_tag_review(
    session: AsyncSession, media_id: uuid.UUID, expected_evidence: object, *, kind: SourceKind | None = None
) -> None:
    """Refresh selected binding at the durable audit-row boundary, including bulk candidates."""
    source_ids = (
        select(ProviderSourceObject.source_file_id)
        .join(ProviderSourceObservation, ProviderSourceObservation.object_id == ProviderSourceObject.id)
        .join(ProviderRecordingSelection, ProviderRecordingSelection.observation_id == ProviderSourceObservation.id)
        .where(ProviderRecordingSelection.media_id == media_id, ProviderRecordingSelection.kind.in_(("tracklist", "release_metadata")))
    )
    await session.execute(
        select(FileRecord.id)
        .where(or_(FileRecord.id == media_id, FileRecord.id.in_(source_ids)))
        .order_by(FileRecord.original_path, FileRecord.id)
        .with_for_update()
    )
    await session.execute(
        select(ProviderRecordingSelection.media_id)
        .where(ProviderRecordingSelection.media_id == media_id, ProviderRecordingSelection.kind.in_(("tracklist", "release_metadata")))
        .with_for_update(read=True)
    )
    await session.execute(select(FileCompanion.id).where(FileCompanion.media_id == media_id).order_by(FileCompanion.id).with_for_update(read=True))
    source = (await overlay_selected_tag_sources(session, [media_id], {})).get(media_id)
    current = list(source.evidence) if isinstance(source, SelectedTagSource) else None
    if current is not None and kind is not None:
        current = [item for item in current if item["kind"] == kind]
    if current != expected_evidence or (current is not None and any(item["availability"] != "current" for item in current)):
        raise ValueError("Selected source binding changed after review; refresh before approving tags")


async def overlay_selected_tag_sources(
    session: AsyncSession, file_ids: list[uuid.UUID], legacy: dict[uuid.UUID, Tracklist]
) -> dict[uuid.UUID, Tracklist | SelectedTagSource]:
    """Two known kinds per file, bounded chunks; never load decoded text or entire Snapshot."""
    result: dict[uuid.UUID, Tracklist | SelectedTagSource] = dict(legacy)
    for start in range(0, len(file_ids), 100):
        metadata = aliased(FileMetadata)
        channel = func.split_part(ProviderSourceObject.native_id, ":", 3)
        embedded_value = metadata.raw_tags.op("->")(channel)
        rows = (
            (
                await session.execute(
                    _observation_query(FileRecord)
                    .add_columns(
                        ProviderRecordingSelection,
                        FileRecord.id.label("media_id"),
                        FileRecord.agent_id.label("media_agent_id"),
                        FileRecord.sha256_hash.label("media_sha256"),
                        FileRecord.file_type.label("media_file_type"),
                        FileRecord.missing_at.label("media_missing_at"),
                        func.jsonb_build_object(
                            "certainty",
                            ProviderSourceObservation.payload["artist"]["certainty"],
                            "value",
                            ProviderSourceObservation.payload["artist"]["value"],
                        ).label("artist_fact"),
                        func.jsonb_build_object(
                            "certainty",
                            ProviderSourceObservation.payload["event"]["certainty"],
                            "value",
                            ProviderSourceObservation.payload["event"]["value"],
                        ).label("event_fact"),
                        func.jsonb_build_object(
                            "certainty",
                            ProviderSourceObservation.payload["date"]["certainty"],
                            "value",
                            ProviderSourceObservation.payload["date"]["value"],
                        ).label("date_fact"),
                        tag_release_projection().label("release_facts"),
                        metadata.failed_at.label("metadata_failed_at"),
                        case((func.octet_length(cast(embedded_value, Text)) <= 2097152, embedded_value), else_=None).label("embedded_value"),
                    )
                    .join(
                        ProviderRecordingSelection,
                        and_(
                            ProviderRecordingSelection.observation_id == ProviderSourceObservation.id,
                            ProviderRecordingSelection.kind.in_(("tracklist", "release_metadata")),
                        ),
                    )
                    .join(FileRecord, FileRecord.id == ProviderRecordingSelection.media_id)
                    .outerjoin(metadata, and_(metadata.file_id == FileRecord.id, ProviderSourceObject.channel == "embedded"))
                    .where(FileRecord.id.in_(file_ids[start : start + 100]))
                    .order_by(FileRecord.id, ProviderRecordingSelection.kind.desc())
                    .execution_options(populate_existing=True)
                )
            )
            .mappings()
            .all()
        )
        grouped: dict[uuid.UUID, list[Any]] = {}
        for row in rows:
            grouped.setdefault(row["ProviderRecordingSelection"].media_id, []).append(row)
        for media_id, selections in grouped.items():
            tags: dict[str, str | int] = {}
            evidence = []
            track_observation_id = None
            for row in selections:
                selection = row["ProviderRecordingSelection"]
                media = CurrentMediaBinding(
                    row["media_id"], row["media_agent_id"], row["media_sha256"], row["media_file_type"], row["media_missing_at"]
                )
                freshness_row = dict(row)
                if row["channel"] == "embedded":
                    channel_key = row["native_id"].split(":", 2)[-1]
                    try:
                        freshness_row["embedded_current"] = (
                            row["metadata_failed_at"] is None
                            and embedded_revision({channel_key: row["embedded_value"]}, channel_key) == row["revision"]
                        )
                    except ValueError:
                        freshness_row["embedded_current"] = False
                availability = _summary(freshness_row, media, ()).availability
                mapping = selection.target_mapping
                if media.missing_at is not None:
                    availability = "missing"
                elif media.file_type not in MEDIA_FILE_TYPES:
                    availability = "unavailable"
                elif mapping is None or "media_sha256" not in mapping:
                    availability = "unknown_binding" if availability == "current" else availability
                elif media.sha256_hash != mapping["media_sha256"] or (
                    row["source_file_id"] is not None and row["sha256_hash"] != mapping.get("source_sha256")
                ):
                    availability = "stale"
                revision, parser = row["revision"], row["parser_version"]
                artist, event, recording_date, facts = row["artist_fact"], row["event_fact"], row["date_fact"], row["release_facts"]
                evidence.append(
                    {
                        "kind": selection.kind,
                        "observation_id": str(selection.observation_id),
                        "selection_token": str(selection_token(selection)),
                        "revision": revision,
                        "parser_version": parser,
                        "target_mapping": selection.target_mapping,
                        "availability": availability,
                        "media_sha256": media.sha256_hash,
                        "media_agent_id": media.agent_id,
                        "source_sha256": row["sha256_hash"],
                        "source_agent_id": row["source_agent"],
                        "embedded_revision": freshness_row.get("embedded_current"),
                    }
                )
                if selection.kind == "tracklist":
                    track_observation_id = selection.observation_id
                if availability != "current":
                    continue
                if selection.kind == "tracklist":
                    for target, fact in (("artist", artist), ("album", event)):
                        if isinstance(fact, dict) and fact.get("certainty") == "known" and fact.get("value"):
                            tags[target] = fact["value"]
                    if isinstance(recording_date, dict) and recording_date.get("certainty") == "known" and recording_date.get("value"):
                        tags["year"] = date.fromisoformat(recording_date["value"]).year
                for fact in facts or []:
                    if fact.get("certainty") != "known" or not fact.get("value"):
                        continue
                    field = fact["field"]
                    value = fact.get("normalized_value") or fact["value"]
                    if field in {"artist", "title", "album", "genre"}:
                        tags[field] = value
                    elif field == "event":
                        tags["album"] = value
                    elif field == "date":
                        with suppress(ValueError):
                            tags["year"] = date.fromisoformat(value).year
                    elif field == "year" and len(value) == 4 and value.isascii() and value.isdecimal() and 1 <= int(value) <= 9999:
                        tags["year"] = int(value)
            result[media_id] = SelectedTagSource(
                media_id,
                tags,
                tuple(evidence),
                track_observation_id,
                legacy.get(media_id) if track_observation_id is None else None,
                all(item["availability"] == "current" for item in evidence),
            )
    return result
