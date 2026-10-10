"""phaze-8yvb0: every real consumer of stored tracks behaves correctly with an UNRESOLVED row.

An unresolved ("ID - ID") row is stored with artist and title both NULL (see
``TracklistTrackPayload.is_unresolved``). The consumers found by reading the code, and the test for each:

* CUE writer      -- ``cue_review.build_cue_tracks_for_versions`` + ``cue_generator`` (``test_cue_*``)
* Tag writer      -- ``tag_proposal.compute_proposed_tags`` reads only Tracklist-level artist/event, never
                     a track row; the one path from a track row into tags is an accepted Discogs link,
                     which needs the Discogs matcher below (``test_tag_*``)
* Review UI       -- ``record/partials/_tracklist_review_body.html`` via ``GET /record/{id}`` (``test_review_ui_*``)
* Discogs matcher -- ``discogs_matcher.match_track_to_discogs`` (``test_discogs_*``)
* Segment join    -- ``track_segments.build_track_segments`` (``test_segment_*``)

Rows are synthetic stored tracklists; no provider pages or parser are needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.cue_generator import generate_cue_content
from phaze.services.cue_review import build_cue_tracks_for_versions
from phaze.services.discogs_matcher import match_track_to_discogs
from phaze.services.tag_proposal import compute_proposed_tags
from phaze.services.track_segments import build_track_segments
from tests._timing import qualified_timing


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


UNRESOLVED_POSITION = 5


async def _store(session: AsyncSession, file_id: uuid.UUID | None = None, *, timestamped: bool = False) -> tuple[Tracklist, TracklistVersion]:
    """Persist a synthetic tracklist containing an unresolved row."""
    tracklist = Tracklist(
        external_id=uuid.uuid4().hex[:8], source_url="https://example.invalid/t", file_id=file_id, artist="Some Artist", event="Some Event"
    )
    session.add(tracklist)
    await session.flush()
    version = TracklistVersion(tracklist_id=tracklist.id, version_number=1)
    session.add(version)
    await session.flush()
    tracklist.latest_version_id = version.id
    for position in range(1, 53):
        session.add(
            TracklistTrack(
                version_id=version.id,
                position=position,
                artist=None if position == UNRESOLVED_POSITION else "Artist",
                title=None if position == UNRESOLVED_POSITION else "Title",
                label=None,
                timestamp=f"{position}:00" if timestamped else None,
                timestamp_evidence=qualified_timing(f"{position}:00", file_id) if timestamped and file_id else None,
                is_mashup=False,
                remix_info=None,
            )
        )
    await session.flush()
    return tracklist, version


async def _unresolved_row(session: AsyncSession, version: TracklistVersion) -> TracklistTrack:
    from sqlalchemy import select

    rows = (
        await session.execute(select(TracklistTrack).where(TracklistTrack.version_id == version.id, TracklistTrack.position == UNRESOLVED_POSITION))
    ).scalars()
    return rows.one()


async def test_cue_writer_emits_a_bare_timed_track_never_performer_id(session: AsyncSession, make_file) -> None:
    file = await make_file()
    _, version = await _store(session, file.id, timestamped=True)

    cue_tracks = (await build_cue_tracks_for_versions(session, [version.id]))[version.id]
    sheet = generate_cue_content("set.mp3", "mp3", cue_tracks)

    assert len(cue_tracks) == 52
    assert 'TITLE "ID"' not in sheet
    assert 'PERFORMER "ID"' not in sheet
    # The unresolved track still holds its place: its TRACK/INDEX pair is emitted with no TITLE/PERFORMER.
    assert sheet.count("TRACK ") == 52
    block = sheet.split(f"  TRACK {UNRESOLVED_POSITION:02d} AUDIO\n")[1].split("  TRACK ")[0]
    assert block.startswith("    INDEX 01 ")


async def test_review_ui_renders_a_dash_for_an_unresolved_row_never_id(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    import re

    file = await make_file(original_filename="Some_Artist-Some_Event-WEB-FLAC-GRP.mp3")
    await _store(session, file.id)
    await session.commit()

    body = (await client.get(f"/record/{file.id}")).text

    row = re.search(rf'<tr data-track-row data-track-position="{UNRESOLVED_POSITION}".*?</tr>', body, re.S)
    assert row is not None
    cells = [re.sub(r"\s+", " ", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row.group(0), re.S)]
    assert cells[2] == "—", cells  # artist
    assert cells[3] == "—", cells  # title
    assert not re.search(r">\s*ID\s*<", row.group(0))


async def test_discogs_matcher_skips_an_unresolved_row_without_querying(session: AsyncSession) -> None:
    _, version = await _store(session)
    row = await _unresolved_row(session, version)
    client = AsyncMock()

    assert await match_track_to_discogs(client, row) == []
    client.search_releases.assert_not_called()
    # The task's own eligibility filter (tasks/discogs.py: `t.artist and t.title`) excludes it as well.
    assert not (row.artist and row.title)


async def test_tag_proposal_never_sees_a_track_row(session: AsyncSession) -> None:
    """Tags come from Tracklist-level artist/event and an accepted Discogs link -- an unresolved track leaks into neither."""
    tracklist, _ = await _store(session)

    proposed = compute_proposed_tags(None, tracklist, "Some_Artist-Some_Event-WEB-FLAC-GRP.mp3")

    assert proposed["artist"] == "Some Artist"
    assert proposed["album"] == "Some Event"
    assert "ID" not in proposed.values()


async def test_segment_join_gives_an_unresolved_row_no_title(session: AsyncSession, make_file) -> None:
    from sqlalchemy import select

    file = await make_file()
    _, version = await _store(session, file.id, timestamped=True)
    tracks = list(
        (await session.execute(select(TracklistTrack).where(TracklistTrack.version_id == version.id).order_by(TracklistTrack.position))).scalars()
    )

    segments = {s.position: s for s in build_track_segments(tracks, [], 7200.0, media_id=file.id)}

    assert segments[UNRESOLVED_POSITION].title is None
    assert segments[1].title == "Title"
    assert "ID" not in {s.title for s in segments.values()}
