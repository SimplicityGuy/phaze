"""Read stored tracklists for the record page, without external acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class FileTracklistReview:
    file_id: uuid.UUID
    tracklist: Tracklist | None
    latest_version: TracklistVersion | None
    tracks: tuple[TracklistTrack, ...]
    is_propagated: bool


async def get_file_tracklist_review(session: AsyncSession, file_id: uuid.UUID) -> FileTracklistReview | None:
    """Return the stored latest version, or None for a missing file."""
    if await session.get(FileRecord, file_id) is None:
        return None
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
        )

    return FileTracklistReview(file_id=file_id, tracklist=None, latest_version=None, tracks=(), is_propagated=False)
