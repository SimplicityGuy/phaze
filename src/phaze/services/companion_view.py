"""Bounded HTML view context for persisted companion content and reviewed decisions."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError
from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from phaze.models.companion_import import ProviderAcquisitionAttempt
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import ProviderSourceObject, ProviderSourceObservation
from phaze.schemas.companion_details import CompanionDetails, DetailPage, StoredObservationDetail, StoredTextRequest
from phaze.schemas.local_source_import import ImportLocalSource, SourceDecision
from phaze.services.companion_acquisition import get_latest_source_attempts
from phaze.services.companion_details import (
    _embedded_freshness,
    _file,
    _observation_query,
    _summary,
    get_companion_details,
    get_source_observations,
    get_stored_observation,
    get_stored_text,
)
from phaze.tracklist_providers.domain import ProviderTrack, ReleaseFact
from phaze.tracklist_providers.local_parser import PARSER_VERSIONS
from phaze.tracklist_providers.local_release import RELEASE_PARSER_VERSIONS


def html_requested(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true" or "text/html" in request.headers.get("accept", "")


def fragment(templates: Jinja2Templates, request: Request, name: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name=name, context={"request": request, **context})


async def command_body(request: Request, model: type[ImportLocalSource] | type[SourceDecision]) -> BaseModel:
    """JSON clients retain their contract; HTMX uses actual native form serialization."""
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/json":
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
    overview = await get_companion_details(session, detail.file_id, attempt_reader=get_latest_source_attempts)
    known = detail.summary.parser_version in PARSER_VERSIONS | RELEASE_PARSER_VERSIONS
    kind = "release_metadata" if detail.summary.parser_version in RELEASE_PARSER_VERSIONS else "tracklist"
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
            obs.parser_version.in_(sorted(PARSER_VERSIONS | RELEASE_PARSER_VERSIONS)),
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


async def companion_overview_context(session: AsyncSession, details: CompanionDetails, review_source_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Small, pinned selection summaries and unified cards; no source-body reads until review."""
    selected = {}
    for authority in details.selected:
        detail = await get_stored_observation(session, details.file_id, authority.observation_id, track_page=DetailPage(limit=3))
        source = await session.get(ProviderSourceObject, authority.source_object_id)
        source_file = await session.get(FileRecord, source.source_file_id) if source and source.source_file_id else None
        selected[authority.kind] = {
            "authority": authority,
            "detail": detail,
            "name": source_file.original_filename if source_file else (source.channel if source else "Retained source"),
        }
    # Independent pagination must not turn a retained source into a duplicate linked-file card.
    sources: dict[uuid.UUID, Any] = {source.source_file_id: source for source in details.sources if source.source_file_id}
    # A link page and a retained-source page can differ. Resolve linked cards outside the
    # source page with two bounded metadata queries, so they never claim existing text is unread.
    missing = {link.file_id: link for link in details.links if link.file_id not in sources}
    if missing:
        objects = tuple(
            await session.scalars(
                select(ProviderSourceObject).where(
                    ProviderSourceObject.source_file_id.in_(missing),
                    ProviderSourceObject.provider_id == "local",
                    ProviderSourceObject.channel == "companion",
                    ProviderSourceObject.native_id == func.concat("companion:", ProviderSourceObject.source_file_id),
                )
            )
        )
        media = await _file(session, details.file_id)
        if media is not None and objects:
            obs = ProviderSourceObservation
            rows = (
                (
                    await session.execute(
                        _observation_query(media)
                        .where(obs.object_id.in_([source.id for source in objects]))
                        .distinct(obs.object_id)
                        .order_by(obs.object_id, obs.retrieved_at.desc(), obs.id.desc())
                    )
                )
                .mappings()
                .all()
            )
            summaries = {row["source_object_id"]: _summary(row, media, details.selected) for row in rows}
            for source in objects:
                if source.source_file_id is not None:
                    sources[source.source_file_id] = {
                        "source_object_id": source.id,
                        "latest_stored_content": summaries.get(source.id),
                        "latest_received_attempt": None,
                    }

    cards = [{"link": link, "source": sources.get(link.file_id)} for link in details.links]
    linked_ids = {link.file_id for link in details.links}
    cards.extend({"link": None, "source": source} for source in details.sources if source.source_file_id not in linked_ids)
    review = await source_review_context(session, details.file_id, review_source_id) if review_source_id else None
    return {"companion_cards": cards, "companion_selected": selected, "companion_review": review, "review_source_id": review_source_id}


async def source_review_context(session: AsyncSession, file_id: uuid.UUID, source_id: uuid.UUID) -> dict[str, Any] | None:
    """Review the newest raw capture and only its interpretations, never a different version's rows."""
    history = await get_source_observations(session, file_id, source_id)
    if history is None:
        return None
    media = await _file(session, file_id)
    obs = ProviderSourceObservation
    if media is None:
        return None
    query = _observation_query(media).where(obs.object_id == source_id)
    raw_id = await session.scalar(
        query.with_only_columns(obs.id).where(obs.parser_version == "source-read-v1").order_by(obs.retrieved_at.desc(), obs.id.desc()).limit(1)
    )
    # Embedded-tag interpretations have no separate acquisition observation.
    text_id = raw_id or (history.observations[0].observation_id if history.observations else None)
    chunk = await get_stored_text(session, file_id, text_id) if text_id else None
    parsed_query = query.where(obs.parser_version.in_(sorted(PARSER_VERSIONS | RELEASE_PARSER_VERSIONS)))
    if raw_id:
        parsed_query = parsed_query.where(obs.parent_id == raw_id)
    parsed_ids = tuple(
        await session.scalars(
            parsed_query.with_only_columns(obs.id).distinct(obs.parser_version).order_by(obs.parser_version, obs.retrieved_at.desc(), obs.id.desc())
        )
    )
    interpretations = []
    for identifier in parsed_ids:
        detail = await get_stored_observation(session, file_id, identifier)
        if detail:
            interpretations.append({"detail": detail, "actions": await observation_actions(session, detail)})
    return {"source_id": source_id, "history": history, "raw_id": raw_id, "chunk": chunk, "interpretations": interpretations}


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


@dataclass(frozen=True)
class CaptureRefreshBaseline:
    since: datetime
    ordinal: int
    revision: str


def _capture_pair(media_id: uuid.UUID, companion_id: uuid.UUID) -> Any:
    """Use current inventory owners and exact native identity, including collision-safe equality."""
    companion = aliased(FileRecord)
    source = ProviderSourceObject
    return (
        select(companion.sha256_hash.label("revision"), source.id.label("source_id"), FileRecord.agent_id.label("owner"))
        .select_from(FileRecord)
        .join(FileCompanion, FileCompanion.media_id == FileRecord.id)
        .join(companion, companion.id == FileCompanion.companion_id)
        .outerjoin(
            source, (source.source_file_id == companion.id) & (source.provider_id == "local") & (source.native_id == f"companion:{companion_id}")
        )
        .where(FileRecord.id == media_id, companion.id == companion_id, companion.agent_id == FileRecord.agent_id)
    ).subquery()


async def capture_refresh_baseline(session: AsyncSession, media_id: uuid.UUID, companion_id: uuid.UUID) -> CaptureRefreshBaseline:
    pair = _capture_pair(media_id, companion_id)
    latest = (
        select(func.coalesce(func.max(ProviderAcquisitionAttempt.ordinal), 0))
        .where(ProviderAcquisitionAttempt.source_object_id == pair.c.source_id)
        .scalar_subquery()
    )
    row = (await session.execute(select(pair.c.revision, latest.label("ordinal"), func.clock_timestamp().label("since")))).mappings().first()
    if row is None:
        raise ValueError("Companion link or owning agent changed")
    return CaptureRefreshBaseline(row["since"], row["ordinal"], row["revision"])


async def capture_refresh_received(
    session: AsyncSession, media_id: uuid.UUID, companion_id: uuid.UUID, baseline: CaptureRefreshBaseline
) -> tuple[datetime, dict[str, Any] | None, bool]:
    """Narrow authenticated receipt evidence; HTTP replay never advances the attempt ordinal.

    A receipt can come from another concurrent capture of this same revision. It is deliberately
    reported as observed after the request rather than asserted to complete one exact queue job.
    """
    pair = _capture_pair(media_id, companion_id)
    attempt = ProviderAcquisitionAttempt
    observation = ProviderSourceObservation
    valid_pair = await session.scalar(select(pair.c.revision).where(pair.c.revision == baseline.revision))
    now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    row = (
        (
            await session.execute(
                select(attempt.ordinal, attempt.status, attempt.code, attempt.freshness, attempt.received_at)
                .join(pair, pair.c.source_id == attempt.source_object_id)
                .join(observation, (observation.id == attempt.observation_id) & (observation.object_id == attempt.source_object_id))
                .where(
                    pair.c.revision == baseline.revision,
                    attempt.agent_id == pair.c.owner,
                    attempt.ordinal > baseline.ordinal,
                    attempt.received_at >= baseline.since,
                    attempt.envelope["target"]["expected_sha256"].astext == baseline.revision,
                    observation.parser_version == "source-read-v1",
                )
                .order_by(attempt.ordinal.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    return now, dict(row) if row is not None else None, valid_pair is not None
