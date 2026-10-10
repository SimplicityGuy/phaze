"""Bounded HTML view context for persisted companion content and reviewed decisions."""

from typing import Any
import uuid

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError
from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import ProviderSourceObject, ProviderSourceObservation
from phaze.schemas.companion_details import CompanionDetails, StoredObservationDetail, StoredTextRequest
from phaze.schemas.local_source_import import ImportLocalSource, SourceDecision
from phaze.services.companion_details import _embedded_freshness, _file, _observation_query, _summary, get_companion_details, get_stored_text
from phaze.tracklist_providers.domain import ProviderTrack, ReleaseFact
from phaze.tracklist_providers.local_parser import PARSER_VERSION
from phaze.tracklist_providers.local_release import RELEASE_PARSER_VERSION


def html_requested(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true" or "text/html" in request.headers.get("accept", "")


def fragment(templates: Jinja2Templates, request: Request, name: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name=name, context={"request": request, **context})


async def command_body(request: Request, model: type[ImportLocalSource] | type[SourceDecision]) -> BaseModel:
    """JSON clients retain their contract; HTMX uses actual native form serialization."""
    if "application/json" in request.headers.get("content-type", ""):
        return model.model_validate(await request.json())
    values: dict[str, Any] = dict(await request.form())
    for key in ("raw_observation_id", "embedded_channel", "expected_selection_token", "expected_revision", "cue_file_ordinal"):
        if values.get(key) == "":
            values[key] = None
    return model.model_validate(values)


def validation_message(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(part) for part in item['loc']) or 'request'}: {item['msg']}" for item in error.errors())


async def embedded_channels(session: AsyncSession, file_id: uuid.UUID) -> tuple[str, ...]:
    """Fetch only recognized stored key names, never tag bodies, for native import controls."""
    names = select(func.jsonb_object_keys(FileMetadata.raw_tags).label("channel")).where(FileMetadata.file_id == file_id).subquery()
    return tuple(
        await session.scalars(
            select(names.c.channel)
            .where(func.lower(names.c.channel).in_(("comment", "comments", "description", "lyrics", "tracklist")))
            .order_by(names.c.channel)
            .limit(50)
        )
    )


async def observation_actions(session: AsyncSession, detail: StoredObservationDetail) -> dict[str, Any]:
    """Expected revision/hash/token values are shown, then independently checked on POST."""
    media = await session.get(FileRecord, detail.file_id)
    source = await session.get(ProviderSourceObject, detail.summary.source_object_id)
    file = await session.get(FileRecord, source.source_file_id) if source and source.source_file_id else None
    overview = await get_companion_details(session, detail.file_id)
    known = detail.summary.parser_version in {PARSER_VERSION, RELEASE_PARSER_VERSION}
    kind = "release_metadata" if detail.summary.parser_version == RELEASE_PARSER_VERSION else "tracklist"
    selected = next((item for item in overview.selected if item.kind == kind), None) if overview else None
    can_select = (
        known
        and detail.summary.status == "found"
        and detail.summary.completeness_state == "complete"
        and detail.summary.availability == "current"
        and detail.summary.candidate_status in {"pending", "accepted"}
        and (detail.summary.release_fact_count > 0 if kind == "release_metadata" else detail.summary.track_count > 0)
    )
    text_id = detail.summary.observation_id
    if detail.summary.parent_id and await get_stored_text(session, detail.file_id, detail.summary.parent_id, chunk=StoredTextRequest(length=1)):
        text_id = detail.summary.parent_id
    source_format = await session.scalar(
        select(ProviderSourceObservation.payload["source_format"].astext).where(ProviderSourceObservation.id == detail.summary.observation_id)
    )
    return {
        "is_cue": source_format == "cue",
        "text_id": text_id,
        "kind": kind,
        "decision_id": uuid.uuid4(),
        "selection_token": selected.selection_token if selected else None,
        "media_sha256": media.sha256_hash if media else None,
        "source_sha256": file.sha256_hash if file else None,
        "can_select": can_select,
        "can_reject": known and detail.summary.candidate_status == "pending",
        "raw_id": detail.summary.observation_id if detail.summary.parser_version == "source-read-v1" else detail.summary.parent_id,
        "source_file_id": source.source_file_id if source else None,
    }


async def visible_parsed_content(session: AsyncSession, details: CompanionDetails) -> tuple[StoredObservationDetail, ...]:
    """One narrow page query exposes both interpretation kinds independently, with tiny row slices.

    Uses the shared membership/freshness predicates rather than a second permission policy.
    Full tracks, release history and raw text remain explicitly lazy.
    """
    media = await _file(session, details.file_id)
    if media is None or not details.sources:
        return ()
    obs = ProviderSourceObservation
    query = (
        _observation_query(media)
        .add_columns(
            func.jsonb_path_query_array(obs.payload, cast("$.tracks[0 to 2]", JSONPATH)).label("visible_tracks"),
            func.jsonb_path_query_array(obs.payload, cast("$.release_facts[0 to 5]", JSONPATH)).label("visible_facts"),
        )
        .where(
            obs.object_id.in_([source.source_object_id for source in details.sources]),
            obs.parser_version.in_([PARSER_VERSION, RELEASE_PARSER_VERSION]),
        )
        .distinct(obs.object_id, obs.parser_version)
        .order_by(obs.object_id, obs.parser_version, obs.retrieved_at.desc(), obs.id.desc())
    )
    rows = (await session.execute(query)).mappings().all()
    fresh_rows = await _embedded_freshness(session, media, rows)
    return tuple(
        StoredObservationDetail(
            file_id=media.id,
            summary=_summary(row, media, details.selected),
            tracks=tuple(ProviderTrack.model_validate(item) for item in row["visible_tracks"]),
            release_facts=tuple(ReleaseFact.model_validate(item) for item in row["visible_facts"]),
            evidence=(),
            next_track_offset=3 if row["track_count"] > 3 else None,
            next_fact_offset=6 if row["release_fact_count"] > 6 else None,
        )
        for row in fresh_rows
    )


def request_schema(model: type[ImportLocalSource] | type[SourceDecision]) -> dict[str, Any]:
    """Expose the validated JSON contract alongside the native form request contract."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value: Any) -> Any:
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].split("/")[-1]])
            return {key: inline(item) for key, item in value.items()}
        if isinstance(value, list):
            return [inline(item) for item in value]
        return value

    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": inline(schema)}, "application/x-www-form-urlencoded": {"schema": inline(schema)}},
        }
    }
