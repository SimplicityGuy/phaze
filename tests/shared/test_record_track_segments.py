"""phaze-x1qr3.6: the tracklist's four measurement columns, through the REAL record endpoints.

``tests/shared/services/test_track_segments.py`` proves the join is arithmetically right. This
file proves the answer reaches the operator: that the record page and the three HTMX endpoints
that re-render the same fragment all put ``track_segments`` in its context, that a timestamped
track's own measurements appear in its own row, and that an untimed track's cells stay empty.

The two are not interchangeable. The columns could be numerically perfect and still render as
em dashes on every surface but one, because a Jinja context is a dict and a missing key is
silently falsy -- which is exactly the failure mode a pure-service test cannot see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisWindow
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.routers.record import build_file_record_context
from phaze.services.analysis_timeline import MOOD_HUES
from phaze.services.track_segments import TrackSegment


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord


LIVE_SET_FILENAME = "Nightwave-Live_At_Signal_Festival-2026-08-01-WEB-FLAC-GRVMSTR.mp3"
DURATION = 1200.0


async def _seed_set_with_tracklist_and_windows(make_file, session: AsyncSession) -> FileRecord:  # type: ignore[no-untyped-def]
    """A two-track set: track 1 timestamped at 0:00, track 2 with NO timestamp at all.

    Windows are placed so track 1's segment [0, 1200) contains all of them and the expected
    values are hand-computable: median BPM of (126, 130) is 128.0, modal Camelot is 8A ("A
    minor"), mean energy of (0.4, 0.6) is 0.5, and mood_party's mean (0.9) beats mood_sad's.
    """
    file_rec = await make_file(original_filename=LIVE_SET_FILENAME)
    session.add(FileMetadata(file_id=file_rec.id, duration=DURATION))
    tracklist = Tracklist(
        external_id="ext-seg", source_url="https://www.1001tracklists.com/tracklist/ext-seg", file_id=file_rec.id, match_confidence=95
    )
    session.add(tracklist)
    await session.flush()
    version = TracklistVersion(tracklist_id=tracklist.id, version_number=1)
    session.add(version)
    await session.flush()
    tracklist.latest_version_id = version.id
    session.add(TracklistTrack(version_id=version.id, position=1, artist="Coldwave", title="Opener", timestamp="00:00"))
    session.add(TracklistTrack(version_id=version.id, position=2, artist="Duskline", title="Untimed", timestamp=None))
    session.add_all(
        [
            AnalysisWindow(
                file_id=file_rec.id, tier="fine", window_index=0, start_sec=0.0, end_sec=30.0, bpm=126.0, musical_key="A minor", camelot="8A"
            ),
            AnalysisWindow(
                file_id=file_rec.id, tier="fine", window_index=1, start_sec=30.0, end_sec=60.0, bpm=130.0, musical_key="A minor", camelot="8A"
            ),
            AnalysisWindow(
                file_id=file_rec.id,
                tier="coarse",
                window_index=0,
                start_sec=0.0,
                end_sec=300.0,
                energy=0.4,
                mood_scores={"mood_party": 0.9, "mood_sad": 0.1},
            ),
            AnalysisWindow(
                file_id=file_rec.id,
                tier="coarse",
                window_index=1,
                start_sec=300.0,
                end_sec=600.0,
                energy=0.6,
                mood_scores={"mood_party": 0.9, "mood_sad": 0.2},
            ),
        ]
    )
    await session.commit()
    return file_rec


@pytest.mark.asyncio
async def test_the_record_context_exposes_the_segment_list_for_the_later_beads(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """``track_segments`` is a real context key on the ONE shared record context builder.

    Discharges "the segment list is exposed in the record context (track_segments) for the
    inspection bead and the poster bead to reuse". Asserted on the builder both presentations
    call, so the drawer and the canonical page cannot diverge on it.
    """
    file_rec = await _seed_set_with_tracklist_and_windows(make_file, session)

    context = await build_file_record_context(file_rec.id, session)

    assert context is not None
    segments = context["track_segments"]
    assert isinstance(segments, list)
    assert [type(segment) for segment in segments] == [TrackSegment]
    assert segments[0].position == 1
    assert segments[0].start_sec == 0.0
    assert segments[0].end_sec == DURATION  # the last track closes at the file's own duration
    assert segments[0].bpm == 128.0
    assert segments[0].camelot == "8A"
    assert segments[0].key == "A minor"
    assert segments[0].mood == "mood_party"
    assert segments[0].mood_hue == MOOD_HUES["mood_party"]
    assert segments[0].energy == 0.5
    # The untimed track produced no segment, so nothing can attach track 1's audio to it.
    assert not any(segment.position == 2 for segment in segments)
    # And the SAME segments drive the timeline's boundary ticks -- one join, two surfaces, so
    # a tick and a table row can never disagree about where a track starts.
    ticks = context["tracklist_ticks"]
    assert isinstance(ticks, list)
    assert [tick["position"] for tick in ticks] == [1]
    assert ticks[0]["sec"] == segments[0].start_sec


@pytest.mark.asyncio
async def test_the_four_columns_render_on_both_record_presentations(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """The drawer and the canonical page both show a timestamped track's own measurements."""
    file_rec = await _seed_set_with_tracklist_and_windows(make_file, session)

    drawer = (await client.get(f"/record/{file_rec.id}")).text
    page = (await client.get(f"/files/{file_rec.id}")).text

    for body in (drawer, page):
        assert "Median BPM of the fine windows inside this track" in body
        assert "128.0" in body
        assert "A minor · 8A" in body
        assert "Party" in body
        assert f"hsl({MOOD_HUES['mood_party']}," in body
        assert "0.5" in body
        # Every existing column survives.
        assert "Opener" in body
        assert "Untimed" in body
        assert "Scraped from 1001Tracklists" in body


@pytest.mark.asyncio
async def test_a_file_with_no_windows_renders_the_table_exactly_as_before(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """Nothing analyzed yet: four em-dash cells, the tracklist itself unchanged and no error."""
    file_rec = await make_file(original_filename=LIVE_SET_FILENAME)
    session.add(FileMetadata(file_id=file_rec.id, duration=DURATION))
    tracklist = Tracklist(external_id="ext-bare", source_url="https://www.1001tracklists.com/tracklist/ext-bare", file_id=file_rec.id)
    session.add(tracklist)
    await session.flush()
    version = TracklistVersion(tracklist_id=tracklist.id, version_number=1)
    session.add(version)
    await session.flush()
    tracklist.latest_version_id = version.id
    session.add(TracklistTrack(version_id=version.id, position=1, artist="Coldwave", title="Opener", timestamp="00:00"))
    await session.commit()

    response = await client.get(f"/record/{file_rec.id}")
    context = await build_file_record_context(file_rec.id, session)

    assert response.status_code == 200
    assert "Opener" in response.text
    assert "Scraped from 1001Tracklists" in response.text
    # The segment exists (the track IS timestamped) but measures nothing, so every one of the
    # four values is an explicit absence -- not a zero, which would read as a silent track.
    assert context is not None
    segments = context["track_segments"]
    assert isinstance(segments, list)
    assert (segments[0].bpm, segments[0].camelot, segments[0].mood, segments[0].energy) == (None, None, None, None)


@pytest.mark.asyncio
async def test_the_prioritize_and_unprioritize_endpoints_keep_the_columns_populated(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """The HTMX re-renders carry the same segments, so a button click never blanks the columns.

    The failure this catches is invisible to a service test and to the GET path: those three
    endpoints build their own context dict, and a missing ``track_segments`` key there would
    swap in a tracklist whose measurements had silently vanished the moment the operator
    touched it.
    """
    file_rec = await _seed_set_with_tracklist_and_windows(make_file, session)

    unprioritized = await client.post(f"/pipeline/tracklists/{file_rec.id}/unprioritize")
    refreshed = await client.post(f"/pipeline/tracklists/{file_rec.id}/refresh")

    for response in (unprioritized, refreshed):
        assert response.status_code == 200
        assert "A minor · 8A" in response.text
        assert "128.0" in response.text


@pytest.mark.asyncio
async def test_a_vanished_file_still_renders_the_not_found_fragment(client: AsyncClient) -> None:
    """The phaze-9xyjp vanished-file response also carries the key, so it renders rather than 500s."""
    response = await client.post(f"/pipeline/tracklists/{uuid.uuid4()}/unprioritize")

    assert "File not found." in response.text
