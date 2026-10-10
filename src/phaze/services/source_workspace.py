"""Paged stored local-source summaries for the three operator workspaces."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import Select, Text, and_, case, cast as sql_cast, func, select
from sqlalchemy.orm import aliased
import structlog

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import ProviderRecordingCandidate, ProviderRecordingSelection, ProviderSourceObject, ProviderSourceObservation
from phaze.services.companion_content import MEDIA_FILE_TYPES
from phaze.services.companion_details import _array, _observation_query, _summary
from phaze.services.local_source_import import embedded_revision
from phaze.services.pagination import DEFAULT_PAGE_SIZE, Page, clamp_page, clamp_page_size, paged_stmt, split_sentinel
from phaze.services.selected_tag_sources import CurrentMediaBinding


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


async def get_source_workspace_page(
    session: AsyncSession, kind: Literal["tracklist", "release_metadata", "cue"], *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
) -> Page[dict[str, Any]]:
    """One narrow SQL page: candidates/history are evidence, selection alone is authority."""
    page, page_size = clamp_page(page), clamp_page_size(page_size)
    selected = aliased(ProviderRecordingSelection)
    channel_key = func.split_part(ProviderSourceObject.native_id, ":", 3)
    embedded_value = FileMetadata.raw_tags.op("->")(channel_key)
    authority_kind = "tracklist" if kind == "cue" else kind
    stmt = (
        _observation_query(FileRecord)
        .add_columns(
            FileRecord.id.label("media_id"),
            FileRecord.original_filename.label("media_filename"),
            FileRecord.agent_id.label("media_agent_id"),
            FileRecord.sha256_hash.label("media_sha256"),
            FileRecord.file_type.label("media_file_type"),
            FileRecord.missing_at.label("media_missing_at"),
            ProviderRecordingCandidate.id.label("candidate_id"),
            selected.observation_id.label("selected_observation_id"),
            selected.actor.label("selected_actor"),
            selected.selection_token.label("selection_token"),
            selected.selected_at.label("selected_at"),
            selected.target_mapping.label("target_mapping"),
            FileMetadata.failed_at.label("metadata_failed_at"),
            case((func.octet_length(sql_cast(embedded_value, Text)) <= 2097152, embedded_value), else_=None).label("embedded_value"),
        )
        .join(ProviderRecordingCandidate, ProviderRecordingCandidate.observation_id == ProviderSourceObservation.id)
        .join(FileRecord, FileRecord.id == ProviderRecordingCandidate.media_id)
        .outerjoin(selected, and_(selected.media_id == FileRecord.id, selected.kind == authority_kind))
        .outerjoin(FileMetadata, and_(FileMetadata.file_id == FileRecord.id, ProviderSourceObject.channel == "embedded"))
    )
    if kind == "release_metadata":
        stmt = stmt.where(func.jsonb_array_length(_array(ProviderSourceObservation.payload["release_facts"])) > 0)
    elif kind == "cue":
        stmt = stmt.where(ProviderSourceObservation.payload["source_format"].as_string() == "cue")
    else:
        stmt = stmt.where(func.jsonb_array_length(_array(ProviderSourceObservation.payload["tracks"])) > 0)
    stmt = paged_stmt(
        stmt,
        page=page,
        page_size=page_size,
        order_by=(ProviderRecordingCandidate.created_at.desc(),),
        tiebreaker=(ProviderRecordingCandidate.id,),
    )
    try:
        async with session.begin_nested():
            rows, has_next = split_sentinel((await session.execute(stmt)).mappings().all(), page_size)
    except Exception:
        logger.warning("local_source_workspace_degraded", kind=kind, page=page, exc_info=True)
        return Page(rows=[], page=page, page_size=page_size, has_next=False)
    result = []
    for row in rows:
        values = dict(row)
        if row["channel"] == "embedded":
            key = row["native_id"].split(":", 2)[-1]
            try:
                values["embedded_current"] = (
                    row["metadata_failed_at"] is None and embedded_revision({key: row["embedded_value"]}, key) == row["revision"]
                )
            except ValueError:
                values["embedded_current"] = False
        media = CurrentMediaBinding(row["media_id"], row["media_agent_id"], row["media_sha256"], row["media_file_type"], row["media_missing_at"])
        availability = _summary(values, media, ()).availability
        is_selected = row["selected_observation_id"] == row["observation_id"]
        mapping = row["target_mapping"]
        if media.missing_at is not None:
            availability = "missing"
        elif media.file_type not in MEDIA_FILE_TYPES:
            availability = "unavailable"
        elif is_selected and availability == "current":
            if mapping is None or "media_sha256" not in mapping:
                availability = "unknown_binding"
            elif mapping["media_sha256"] != media.sha256_hash or (
                row["source_file_id"] is not None and mapping.get("source_sha256") != row["sha256_hash"]
            ):
                availability = "stale"
        values.pop("embedded_value", None)
        result.append(dict(values, selected=is_selected, availability=availability))
    return Page(
        rows=result,
        page=page,
        page_size=page_size,
        has_next=has_next,
    )


async def get_existing_cue_page(session: AsyncSession, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> Page[dict[str, Any]]:
    """Current inventoried CUE companions, including files with no stored interpretation."""
    page, page_size = clamp_page(page), clamp_page_size(page_size)
    companion = aliased(FileRecord)
    stmt = paged_stmt(
        cast(
            "Select[Any]",
            select(
                FileRecord.id.label("media_id"),
                FileRecord.original_filename.label("media_filename"),
                companion.id.label("source_file_id"),
                companion.original_filename.label("source_filename"),
                companion.missing_at,
                companion.companion_ambiguous_at,
            )
            .join(FileCompanion, FileCompanion.media_id == FileRecord.id)
            .join(companion, companion.id == FileCompanion.companion_id)
            .where(companion.file_type == "cue", companion.agent_id == FileRecord.agent_id),
        ),
        page=page,
        page_size=page_size,
        order_by=(FileRecord.original_filename,),
        tiebreaker=(FileCompanion.id,),
    )
    try:
        async with session.begin_nested():
            rows, has_next = split_sentinel((await session.execute(stmt)).mappings().all(), page_size)
    except Exception:
        logger.warning("companion_cue_workspace_degraded", page=page, exc_info=True)
        return Page(rows=[], page=page, page_size=page_size, has_next=False)
    return Page(rows=[dict(row) for row in rows], page=page, page_size=page_size, has_next=has_next)
