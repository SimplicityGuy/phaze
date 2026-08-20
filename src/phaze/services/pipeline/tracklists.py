"""The tracklist / match read model -- the match-pending enqueue set, the bounded per-set page,
and the un-tracklisted file set.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr).
:func:`get_untracked_files` moved here from beside the metadata readers: it answers "which files
still have no ``Tracklist`` row", which is the complement ``stage_status.done_clause(Stage.TRACKLIST)``
answers from the other side (``tests/shared/core/test_identify_workspaces.py`` pins the two together).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, func, select
import structlog

from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.pagination import DEFAULT_PAGE_SIZE, Page, clamp_page, clamp_page_size, paged_stmt, split_sentinel
from phaze.services.pipeline.common import MUSIC_VIDEO_TYPES


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

    from phaze.routers.column_sort import SortState


logger = structlog.get_logger(__name__)


async def get_match_pending_tracklists(session: AsyncSession) -> list[Tracklist]:
    """Return the Tracklist rows NOT reachable from ``discogs_links`` (the complement of match.done).

    The EXACT complement of :func:`get_stage_progress`'s ``match.done`` (DISTINCT tracklist_id walked
    ``discogs_links -> tracklist_tracks -> tracklist_versions``): a tracklist whose version→track→link
    chain exists is excluded. A tracklist with a scraped version but no discogs link is still
    match-pending (scrape and match are independent stages). Pure ORM ``.not_in(subquery)`` with NO
    interpolated operator input (T-41-01).
    """
    matched_subq = (
        select(TracklistVersion.tracklist_id)
        .select_from(DiscogsLink)
        .join(TracklistTrack, DiscogsLink.track_id == TracklistTrack.id)
        .join(TracklistVersion, TracklistTrack.version_id == TracklistVersion.id)
    )
    stmt = select(Tracklist).where(Tracklist.id.not_in(matched_subq))
    result = await session.execute(stmt)
    return list(result.scalars().all())


# --- Phase 59 (59-01, IDENT-01/IDENT-02) Identify-workspace read-only row assembly ----------
#
# The two genuinely-new pieces of Phase 59 (RESEARCH "Don't Hand-Roll" key insight): per-row
# presentation data for the Track-ID combined table and the Tracklist per-set table. Both are
# PURE READS over existing, already-populated tables (no enqueue, no commit, no schema change) and
# both degrade to ``[]`` inside a SAVEPOINT on any error, mirroring :func:`get_analyze_working_set`
# -- they ride the hot render/poll path and must NEVER 500 the page.

# phaze-1wvb: the Identify per-set Tracklist read below is BOUNDED at the source, on the paging
# contract (:mod:`phaze.services.pagination`). As authored in Phase 59 it was a whole-corpus read --
# one row per ``Tracklist``, no LIMIT, materialised with ``.all()`` and server-rendered inline into
# one HTML table by ``shell._render_stage``. That is the identical cliff phaze-5462 fixed on the
# Analyze tab (10,132 rows / 12.7 MB, and 92,335 rows / ~105 MB HTML at the seeded 200K scale).
# (phaze-0jpe: the sibling Track-ID reader bounded here alongside it is gone with the fingerprint
# feature -- its whole reason for existing was per-engine audio-match status.)
#
# RULE 7 DETERMINATION (paging contract rule 7 -- do this BEFORE bounding anything): the reader is
# RENDER-ONLY. Verified by call graph -- its ONLY caller was ``shell._render_stage``
# (``tracklist_sets``), flowing straight into ``_file_table.html``; it feeds no enqueue, no trigger
# and no bulk action. The Identify workspace's remaining bulk action reads a DIFFERENT, deliberately
# UNBOUNDED set: MATCH ALL -> :func:`get_match_pending_tracklists`. (phaze-2akf removed the SEARCH ALL
# and SCRAPE ALL triggers with the legacy scrape path; the drain replaces them and bounds itself in
# LOOKUPS rather than in rows.) That set is not touched here, so there is no shared reader to split
# and no way for this change to under-enqueue the backlog: bounding this one bounds ONLY pixels. Do
# NOT ever point a bulk trigger at a ``*_page`` reader.


def _tracklist_sets_page_stmt(*, page: int, page_size: int, sort: SortState | None = None) -> Select[Any]:
    """Build the BOUNDED per-set Tracklist page SELECT (phaze-1wvb).

    Extracted so the LIMIT is assertable in the EMITTED SQL, not merely inferred from the length of the returned list. Newest-first with the
    MANDATORY unique ``Tracklist.id`` tiebreaker (contract rule 4).
    """
    track_counts_subq = (
        select(
            TracklistTrack.version_id.label("version_id"),
            func.count(TracklistTrack.id).label("total"),
            func.count(TracklistTrack.confidence).label("confident"),
        )
        .group_by(TracklistTrack.version_id)
        .subquery()
    )
    return paged_stmt(
        select(
            Tracklist.external_id,
            Tracklist.artist,
            Tracklist.event,
            Tracklist.file_id,
            FileRecord.original_filename,
            FileRecord.original_path,
            track_counts_subq.c.total,
            track_counts_subq.c.confident,
        )
        .select_from(Tracklist)
        .outerjoin(FileRecord, FileRecord.id == Tracklist.file_id)
        .outerjoin(track_counts_subq, track_counts_subq.c.version_id == Tracklist.latest_version_id),
        page=page,
        page_size=page_size,
        # phaze-a6hm.1 sortable-column contract -- see _pending_page_stmt.
        order_by=sort.order_by() if sort is not None else (Tracklist.created_at.desc(),),
        tiebreaker=(Tracklist.id.desc(),),
    )


async def get_tracklist_sets_page(
    session: AsyncSession, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, sort: SortState | None = None
) -> Page[dict[str, Any]]:
    """Return ONE BOUNDED page of the per-set Tracklist rows (IDENT-02 / D-07/D-08), degrade-safe.

    One row per ``Tracklist`` (a "set"), carrying the set name + path, the match-state, the exact
    matched ``file_id`` (or ``None``), and the D-07 per-set track coverage: ``tracks_confident`` of
    ``tracks_total`` derived from ``TracklistTrack.confidence`` over the tracklist's versioned tracks
    (``COUNT(confidence)`` counts only non-NULL confidences -> the confident N; ``COUNT(id)`` -> the
    total M). Membership and row shape are UNCHANGED from Phase 59; phaze-1wvb only added the bound.

    The track counts stay scoped to the tracklist's ``latest_version_id`` only (the same convention
    the tracklists router uses) -- a re-scraped tracklist with multiple versions must NOT sum coverage
    across versions, which would inflate the D-07 N/M. A tracklist whose ``latest_version_id`` is NULL
    reports 0/0.

    Bounded per the paging contract: OFFSET pages with a ``page_size + 1`` sentinel for ``has_next``
    (never a COUNT -- rule 2), newest-first display order with the MANDATORY unique ``Tracklist.id``
    tiebreaker (rule 4 -- ``created_at`` ties for every row written in one transaction), and clamped
    inputs that yield an empty page rather than an error (rule 5).

    RENDER READ ONLY (rule 7): the MATCH ALL trigger above this table enqueues
    :func:`get_match_pending_tracklists`, which is UNBOUNDED BY DESIGN and untouched, and the drain
    trigger beside it bounds itself in LOOKUPS rather than in rows. Paging THIS read cannot
    under-enqueue anything; paging THOSE would.

    Degrade-safe via a SAVEPOINT returning an EMPTY :class:`Page` on any error (rule 6).
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    try:
        async with session.begin_nested():
            stmt = _tracklist_sets_page_stmt(page=page, page_size=page_size, sort=sort)
            raw = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("tracklist_sets_page_degraded", page=page, page_size=page_size, exc_info=True)
        return Page(rows=[], page=page, page_size=page_size, has_next=False)

    sentinel_rows, has_next = split_sentinel(raw, page_size)
    sets: list[dict[str, Any]] = []
    for external_id, artist, event, file_id, filename, path, total, confident in sentinel_rows:
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
    return Page(rows=sets, page=page, page_size=page_size, has_next=has_next)


async def get_untracked_files(session: AsyncSession) -> list[FileRecord]:
    """Return music/video FileRecords with NO ``Tracklist`` row -- the un-tracklisted set.

    phaze-2akf: this used to be the enqueue set for the "SEARCH ALL" bulk trigger, which is gone
    with the legacy scrape path. It survives as the canonical READ of "which files still have no
    tracklist" -- the same question ``stage_status.done_clause(Stage.TRACKLIST)`` answers from the
    other side, which is why ``tests/shared/core/test_identify_workspaces.py`` pins the two
    together. The drain builds its own, much narrower candidate funnel
    (``services/tracklist_candidate_queue.py``) and does NOT consume this. Pure ORM ``~exists(...)``
    with NO interpolated operator input (T-42-03).
    """
    stmt = select(FileRecord).where(
        FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
        ~exists(select(Tracklist.id).where(Tracklist.file_id == FileRecord.id)),
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
