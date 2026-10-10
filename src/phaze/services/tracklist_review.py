"""Read stored tracklists for the record page, without external acquisition."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, select
from sqlalchemy.orm import aliased

from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.local_source_import import get_selected_recording_source


if TYPE_CHECKING:
    from collections.abc import Mapping
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.schemas.local_source_import import SelectedRecordingSource
    from phaze.tracklist_providers.domain import ProviderTrack


_TIMESTAMP_LINE = re.compile(r"(?:^|\n)[^\n]{0,40}?\b\d{1,2}:\d{2}(?::\d{2})?\b")
_TEXT_KEYS = frozenset(
    {"comment", "comments", "description", "desc", "lyrics", "unsyncedlyrics", "tracklist", "chapters", "chapter", "podcastdesc", "purl"}
)
_TAG_HEAD_CHARS = 12000


def detect_embedded_tracklist(raw_tags: Mapping[str, Any] | None) -> bool:
    """Recognize three timestamped lines in a descriptive tag's bounded head; never parse rows."""
    for key, value in (raw_tags or {}).items():
        if key.lower().replace(" ", "").replace("_", "") not in _TEXT_KEYS:
            continue
        values = [value] if isinstance(value, str) else value if isinstance(value, (list, tuple)) else []
        for text in values:
            if isinstance(text, str) and len(_TIMESTAMP_LINE.findall(text[:_TAG_HEAD_CHARS])) >= 3:
                return True
    return False


async def _local_sources(session: AsyncSession, file_id: uuid.UUID) -> tuple[str, ...]:
    """Recognize linked local sources from existing metadata; perform no file or network reads."""
    metadata = (await session.execute(select(FileMetadata.raw_tags).where(FileMetadata.file_id == file_id))).scalar_one_or_none()
    sources = ["embedded tracklist"] if detect_embedded_tracklist(metadata) else []
    companion = aliased(FileRecord)
    linked = select(FileCompanion.id).join(companion, companion.id == FileCompanion.companion_id).where(FileCompanion.media_id == file_id)
    if (await session.execute(select(exists(linked.where(companion.file_type == "cue"))))).scalar_one():
        sources.append("CUE companion")
    text_tracklist = linked.join(
        CompanionContentFeatures,
        (CompanionContentFeatures.file_id == companion.id) & (CompanionContentFeatures.fingerprint == companion.sha256_hash),
    ).where(companion.file_type.in_(("txt", "nfo")), CompanionContentFeatures.is_tracklist.is_(True))
    if (await session.execute(select(exists(text_tracklist)))).scalar_one():
        sources.append("text companion tracklist")
    return tuple(sources)


@dataclass(frozen=True, slots=True)
class FileTracklistReview:
    file_id: uuid.UUID
    tracklist: Tracklist | None
    latest_version: TracklistVersion | None
    tracks: tuple[TracklistTrack | ProviderTrack, ...]
    is_propagated: bool
    local_sources: tuple[str, ...] = ()
    selected_source: SelectedRecordingSource | None = None


async def get_file_tracklist_review(session: AsyncSession, file_id: uuid.UUID) -> FileTracklistReview | None:
    """Return the stored latest version, or None for a missing file."""
    if await session.get(FileRecord, file_id) is None:
        return None
    local_sources = await _local_sources(session, file_id)
    selected = await get_selected_recording_source(session, file_id)
    if selected is not None:
        return FileTracklistReview(
            file_id=file_id,
            tracklist=None,
            latest_version=None,
            tracks=selected.tracks,
            is_propagated=False,
            local_sources=local_sources,
            selected_source=selected,
        )
    tracklist_result = await session.execute(select(Tracklist).where(Tracklist.file_id == file_id).order_by(Tracklist.updated_at.desc()).limit(1))
    tracklist = tracklist_result.scalar_one_or_none()

    if tracklist is not None:
        latest_version: TracklistVersion | None = None
        tracks: tuple[TracklistTrack, ...] = ()
        if tracklist.latest_version_id is not None:
            latest_version = await session.get(TracklistVersion, tracklist.latest_version_id)
            track_result = await session.execute(
                select(TracklistTrack).where(TracklistTrack.version_id == tracklist.latest_version_id).order_by(TracklistTrack.position)
            )
            tracks = tuple(track_result.scalars().all())
        return FileTracklistReview(
            file_id=file_id,
            tracklist=tracklist,
            latest_version=latest_version,
            tracks=tracks,
            is_propagated=tracklist.propagated_from_set_key is not None,
            local_sources=local_sources,
        )

    return FileTracklistReview(file_id=file_id, tracklist=None, latest_version=None, tracks=(), is_propagated=False, local_sources=local_sources)
