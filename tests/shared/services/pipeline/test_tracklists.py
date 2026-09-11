"""Tests for `services/pipeline/tracklists.py` (split from test_pipeline.py, phaze-7l8jh).

Tracklist row projection, get_match_pending_tracklists, get_untracked_files --
`services/pipeline/tracklists.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from phaze.services.pipeline import get_tracklist_sets_page
from phaze.services.pipeline.tracklists import _project_tracklist_set_rows
from tests.shared.services.pipeline._shared import (
    Tracklist,
    _make_pipeline_file,
    _make_tracklist,
    _NullSavepoint,
    get_match_pending_tracklists,
    get_untracked_files,
    pytest,
    uuid,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _pre_extraction_projection(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """The inline projection used by ``get_tracklist_sets_page`` before extraction."""
    sets: list[dict[str, Any]] = []
    for external_id, artist, event, file_id, filename, path, total, confident in rows:
        matched = file_id is not None
        set_name = filename if matched else (artist or event or external_id)
        sets.append(
            {
                "set_name": set_name,
                "path": path,
                "tracklist_state": "matched" if matched else "candidate",
                "file_id": file_id,
                "tracks_confident": int(confident or 0),
                "tracks_total": int(total or 0),
                "matched_to_file": matched,
            }
        )
    return sets


class _RowsResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class _RowsSession:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def begin_nested(self) -> _NullSavepoint:
        return _NullSavepoint()

    async def execute(self, *_args: object, **_kwargs: object) -> _RowsResult:
        return _RowsResult(self._rows)


def test_tracklist_set_projection_matches_pre_extraction_values_and_order() -> None:
    matched_id = uuid.uuid4()
    rows = [
        ("matched", "Artist", "Event", matched_id, "set.mp3", "/music/set.mp3", 2, 1),
        ("artist-fallback", "Artist", "Event", None, None, None, None, None),
        ("event-fallback", None, "Event", None, None, None, 0, 0),
        ("external-fallback", None, None, None, None, None, None, None),
        ("missing-file-data", None, None, matched_id, None, None, None, None),
    ]

    projected = _project_tracklist_set_rows(rows)
    assert projected == _pre_extraction_projection(rows)
    assert [row["set_name"] for row in projected] == ["set.mp3", "Artist", "Event", "external-fallback", None]
    assert (projected[0]["tracks_confident"], projected[0]["tracks_total"]) == (1, 2)


@pytest.mark.asyncio
async def test_tracklist_set_projection_preserves_sentinel_order_and_empty_pages() -> None:
    rows = [(f"set-{index:02d}", None, None, None, None, None, None, None) for index in range(11)]

    first_page = await get_tracklist_sets_page(_RowsSession(rows), page_size=10)  # type: ignore[arg-type]
    assert first_page.rows == _pre_extraction_projection(rows[:10])
    assert first_page.has_next is True

    empty_page = await get_tracklist_sets_page(_RowsSession([]), page=2, page_size=10)  # type: ignore[arg-type]
    assert empty_page.rows == _pre_extraction_projection([])
    assert empty_page.has_next is False


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
