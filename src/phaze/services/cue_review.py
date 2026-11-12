"""Shared cue review queries and selected-source cue-track assembly.

Review consumes the approved/applied legacy-tracklist predicate from this service module, avoiding a
service-to-router dependency. Legacy tracklists are display-only: only a reviewed selected source
supplies qualified CUE offsets (docs/design/0027-retire-legacy-timestamp-qualification.md).

``build_cue_tracks_for_file`` / ``cue_tracks_from_selected`` project a recording's CUE tracks from
its reviewed selected source alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Select, select

from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist
from phaze.services.cue_generator import CueTrackData
from phaze.services.local_source_import import get_selected_recording_source
from phaze.services.selected_discogs import accepted_recording_discogs_links
from phaze.services.selected_source_consumers import explicit_tracklist_clause
from phaze.services.stage_status import applied_clause


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.discogs_link import DiscogsLink
    from phaze.schemas.local_source_import import SelectedRecordingSource


def gated_tracklist_stmt() -> Select[Tracklist, FileRecord]:
    """Build the base (UNORDERED, UNBOUNDED) SELECT for legacy cue review cards: approved + applied.

    Every legacy tracklist is GATED -- display-only, never a CUE source. Only a reviewed selected
    source can supply a qualified recording offset, so an approved, applied legacy tracklist is
    listed for review context but can never be generated from
    (docs/design/0027-retire-legacy-timestamp-qualification.md). Media with an explicit selected
    source are excluded: that source owns the recording's cue workflow.

    Deliberately carries no ``ORDER BY`` and no ``LIMIT`` so each caller composes exactly the ones
    it needs: the card reader adds a display sort plus the review row cap, the counter adds neither.
    """
    return (
        select(Tracklist, FileRecord)
        .join(FileRecord, Tracklist.file_id == FileRecord.id)
        .where(Tracklist.status == "approved", Tracklist.file_id.is_not(None), applied_clause(), ~explicit_tracklist_clause(Tracklist.file_id))
    )


async def build_cue_tracks_for_file(session: AsyncSession, media_id: uuid.UUID) -> list[CueTrackData]:
    """The recording's CUE tracks: only its explicit reviewed authority, rational offsets kept until rendering.

    A recording with no selected tracklist source has no CUE tracks. A legacy tracklist is never a
    CUE source: its timing is display-only (docs/design/0027-retire-legacy-timestamp-qualification.md).
    """
    selected = await get_selected_recording_source(session, media_id)
    if selected is None:
        return []
    links = await accepted_recording_discogs_links(session, selected)
    return cue_tracks_from_selected(selected, links)


def cue_tracks_from_selected(selected: SelectedRecordingSource, links: dict[int, DiscogsLink] | None = None) -> list[CueTrackData]:
    """Project only current, qualified rational offsets from one immutable reviewed authority."""
    result = []
    for track in selected.tracks:
        timestamp = track.timestamp
        offset = (
            timestamp.offset.as_fraction()
            if selected.availability == "current"
            and timestamp is not None
            and timestamp.kind == "offset"
            and timestamp.offset_usability == "qualified"
            and timestamp.offset is not None
            else None
        )
        link = (links or {}).get(track.position)
        result.append(
            CueTrackData(
                position=track.position,
                title=track.title,
                artist=track.artist,
                timestamp_seconds=offset,
                label=link.discogs_label if link is not None else track.label,
                year=link.discogs_year if link is not None else None,
            )
        )
    return result
