"""Explicit reviewed source predicates shared by bounded downstream consumers."""

from typing import Any

from sqlalchemy import ColumnElement, Text, and_, case, cast, exists, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH

from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.provider_source import ProviderRecordingSelection, ProviderSourceObservation
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


def explicit_tracklist_clause(file_id: Any = FileRecord.id) -> ColumnElement[bool]:
    return exists(
        select(ProviderRecordingSelection.media_id)
        .where(ProviderRecordingSelection.media_id == file_id, ProviderRecordingSelection.kind == "tracklist")
        .correlate_except(ProviderRecordingSelection)
    )


def selected_track_membership_clause(position: Any = DiscogsLink.source_track_position) -> ColumnElement[bool]:
    """Narrow SQL membership mirrors intrinsic position and explicit CUE FILE projection."""
    payload = ProviderSourceObservation.payload
    tracks = (
        func.jsonb_array_elements(case((func.jsonb_typeof(payload["tracks"]) == "array", payload["tracks"]), else_=cast("[]", JSONB)))
        .table_valued("value")
        .alias("selected_intrinsic_track")
    )
    track = cast(tracks.c.value, JSONB)
    cue_member = func.jsonb_path_exists(
        case((func.jsonb_typeof(track["evidence"]) == "array", track["evidence"]), else_=cast("[]", JSONB)),
        cast("$[*] ? (@ starts with $prefix)", JSONPATH),
        func.jsonb_build_object("prefix", func.concat("FILE:", ProviderRecordingSelection.target_mapping["cue_file_ordinal"].as_string(), ":")),
    )
    return exists(
        select(1)
        .select_from(tracks)
        .where(
            track["position"].as_string() == cast(position, Text),
            or_(
                payload["source_format"].as_string().is_distinct_from("cue"),
                and_(ProviderRecordingSelection.target_mapping["cue_file_ordinal"].is_not(None), cue_member),
            ),
        )
        .correlate(ProviderSourceObservation, ProviderRecordingSelection, DiscogsLink)
    )


def selected_discogs_clause(file_id: Any = FileRecord.id) -> ColumnElement[bool]:
    """Matching completion is bound to actual tracks of the selected recording projection."""
    return exists(
        select(DiscogsLink.id)
        .join(ProviderSourceObservation, ProviderSourceObservation.id == DiscogsLink.source_observation_id)
        .join(ProviderRecordingSelection, ProviderRecordingSelection.observation_id == ProviderSourceObservation.id)
        .where(ProviderRecordingSelection.media_id == file_id, ProviderRecordingSelection.kind == "tracklist", selected_track_membership_clause())
        .correlate_except(ProviderRecordingSelection, ProviderSourceObservation, DiscogsLink)
    )


def discogs_authority_clause() -> ColumnElement[bool]:
    """Search uses actual selected membership; legacy links survive only without explicit replacement."""
    selected = exists(
        select(ProviderRecordingSelection.media_id)
        .join(ProviderSourceObservation, ProviderSourceObservation.id == ProviderRecordingSelection.observation_id)
        .where(
            ProviderRecordingSelection.kind == "tracklist",
            ProviderSourceObservation.id == DiscogsLink.source_observation_id,
            selected_track_membership_clause(),
        )
    )
    legacy = exists(
        select(TracklistTrack.id)
        .join(TracklistVersion, TracklistVersion.id == TracklistTrack.version_id)
        .join(Tracklist, Tracklist.id == TracklistVersion.tracklist_id)
        .where(TracklistTrack.id == DiscogsLink.track_id, ~explicit_tracklist_clause(Tracklist.file_id))
    )
    return or_(selected, legacy)


def authoritative_tracklist_clause(file_id: Any = FileRecord.id) -> ColumnElement[bool]:
    selected = exists(
        select(ProviderRecordingSelection.media_id)
        .join(ProviderSourceObservation, ProviderSourceObservation.id == ProviderRecordingSelection.observation_id)
        .where(
            ProviderRecordingSelection.media_id == file_id,
            ProviderRecordingSelection.kind == "tracklist",
            func.jsonb_array_length(
                case(
                    (func.jsonb_typeof(ProviderSourceObservation.payload["tracks"]) == "array", ProviderSourceObservation.payload["tracks"]),
                    else_=cast("[]", JSONB),
                )
            )
            > 0,
        )
        .correlate_except(ProviderRecordingSelection, ProviderSourceObservation)
    )
    legacy = exists(select(Tracklist.id).where(Tracklist.file_id == file_id).correlate_except(Tracklist))
    return or_(selected, and_(~explicit_tracklist_clause(file_id), legacy))


def selected_fact_value(field: str, file_id: Any = FileRecord.id) -> Any:
    """Certain scalar values of pinned observations, never newer pending candidates."""
    if field not in {"artist", "event", "date", "genre", "album", "title"}:
        raise ValueError("Unsupported selected fact")
    payload = ProviderSourceObservation.payload
    structured = case((payload[field]["certainty"].as_string() == "known", payload[field]["value"].as_string()), else_=None)
    value = func.coalesce(last_certain_release_value(field), structured)
    return (
        select(value)
        .join(ProviderRecordingSelection, ProviderRecordingSelection.observation_id == ProviderSourceObservation.id)
        .where(
            ProviderRecordingSelection.media_id == file_id, ProviderRecordingSelection.kind.in_(("tracklist", "release_metadata")), value.is_not(None)
        )
        .order_by(case((ProviderRecordingSelection.kind == "release_metadata", 0), else_=1))
        .limit(1)
        .correlate(FileRecord)
        .scalar_subquery()
    )


def last_certain_release_value(field: str) -> Any:
    """Last known supported scalar within one source; discard evidence/text before transport."""
    if field not in {"artist", "event", "date", "genre", "album", "title", "year"}:
        raise ValueError("Unsupported selected fact")
    last = _last_certain_release_fact((field,))
    return func.coalesce(func.nullif(last["normalized_value"].as_string(), ""), last["value"].as_string())


def _last_certain_release_fact(fields: tuple[str, ...]) -> Any:
    predicate = " || ".join(f'@.field == "{field}"' for field in fields)
    matches = cast(
        func.jsonb_path_query_array(
            ProviderSourceObservation.payload, cast(f'$.release_facts[*] ? (({predicate}) && @.certainty == "known")', JSONPATH)
        ),
        JSONB,
    )
    return matches.op("->")(-1)


def tag_release_projection() -> Any:
    # Preserve source order across aliases which feed the same tag (album/event, date/year).
    # At most five scalar values cross the DB boundary, with no original text or evidence arrays.
    last_facts = [_last_certain_release_fact(fields) for fields in (("artist",), ("title",), ("album", "event"), ("genre",), ("date", "year"))]
    return func.jsonb_build_array(
        *(
            func.jsonb_build_object(
                "field",
                last["field"].as_string(),
                "certainty",
                "known",
                "value",
                func.coalesce(func.nullif(last["normalized_value"].as_string(), ""), last["value"].as_string()),
            )
            for last in last_facts
        )
    )
