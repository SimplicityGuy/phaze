"""Tests for `services/pipeline/tracklists.py` (split from test_pipeline.py, phaze-7l8jh).

get_match_pending_tracklists, get_untracked_files -- `services/pipeline/tracklists.py`.
"""

from __future__ import annotations

from tests.shared.services.pipeline._shared import *


@pytest.mark.asyncio
async def test_get_match_pending_tracklists_excludes_discogs_reachable(session: AsyncSession) -> None:
    """Match pending = tracklists NOT reachable from discogs_links; a linked tracklist is excluded.

    The match-reachable chain is version → TracklistTrack → DiscogsLink (the SAME join-walk
    get_stage_progress.match.done uses). A tracklist with no discogs chain stays pending even if it
    HAS a scraped version (scrape and match are independent stages).
    """
    from phaze.models.discogs_link import DiscogsLink
    from phaze.models.tracklist import TracklistTrack, TracklistVersion

    pending = _make_tracklist(1)
    linked = _make_tracklist(2)
    session.add_all([pending, linked])
    await session.flush()
    # `pending` gets a version (scrape-done) but NO discogs link → still match-pending.
    session.add(TracklistVersion(id=uuid.uuid4(), tracklist_id=pending.id, version_number=1))
    linked_version = TracklistVersion(id=uuid.uuid4(), tracklist_id=linked.id, version_number=1)
    session.add(linked_version)
    await session.flush()
    track = TracklistTrack(id=uuid.uuid4(), version_id=linked_version.id, position=1)
    session.add(track)
    await session.flush()
    session.add(DiscogsLink(id=uuid.uuid4(), track_id=track.id, discogs_release_id="r1", confidence=0.9))
    await session.flush()

    result = await get_match_pending_tracklists(session)
    ids = {tl.id for tl in result}
    assert pending.id in ids
    assert linked.id not in ids


@pytest.mark.asyncio
async def test_get_untracked_files_excludes_files_with_tracklist(session: AsyncSession) -> None:
    """Untracked = music/video files with NO Tracklist row; a tracked file and a non-music file are out."""
    untracked = _make_pipeline_file(file_type="mp3")
    tracked = _make_pipeline_file(file_type="mp3")
    non_music = _make_pipeline_file(file_type="txt")
    session.add_all([untracked, tracked, non_music])
    await session.flush()
    session.add(Tracklist(id=uuid.uuid4(), file_id=tracked.id, external_id="tl-1", source_url="http://x/1"))
    await session.flush()

    result = await get_untracked_files(session)
    ids = {f.id for f in result}
    assert untracked.id in ids
    assert tracked.id not in ids
    assert non_music.id not in ids
