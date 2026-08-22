"""Shared cue-eligibility query + cue-track-building helpers (phaze-b4u3p).

``eligible_tracklist_stmt``/``get_eligible_tracklist_query`` lived as private helpers on
``routers/cue.py`` and were reached into directly by ``services/review.py``
(``get_cue_review_cards``, ``count_cue_review_candidates``) -- a SERVICE importing a ROUTER's
underscore-prefixed surface, the same layering inversion ``services/tag_comparison.py`` documents
for the tag-review seam. Moved here so both ``routers/cue.py`` and ``services/review.py`` depend
on ONE shared module. ``routers/cue.py`` re-imports the eligible-side names it still uses under
the same identifiers, so its own routes and the existing white-box tests that import them via
``phaze.routers.cue`` are unaffected.

``gated_tracklist_stmt`` (the sibling GATED predicate -- approved + applied, no timestamped
track) previously lived on ``services/review.py`` itself as ``_gated_tracklist_stmt``, built from
an inline COPY of the same join + first-three-predicates ``eligible_tracklist_stmt`` uses, which
was the 14-line clone repowise flagged between the two files. ``_approved_applied_tracklist_base``
factors that shared core out so the eligible and gated statements can never drift on the parts
they're supposed to share (in particular the ``latest_version_id`` scoping phaze-dboy fixed once
already -- see its note below).

``build_cue_tracks_for_versions`` is the batched form of ``routers.cue._build_cue_tracks``: TWO
queries for however many tracklist VERSIONS are asked for, instead of two PER version. Before this
(phaze-b4u3p), ``get_cue_review_cards`` called the single-version form once per eligible tracklist
inside its render loop -- a cross-function N+1 (up to ``2 * _MAX_REVIEW_ROWS`` round-trips for a
full page). ``routers.cue._build_cue_tracks`` is now defined IN TERMS OF this function (a
single-version call with the same query shape, just not batched), so there is exactly one
implementation of "which tracks + accepted Discogs links belong to this version" between the
router's three single-tracklist call sites and the review workspace's batched one.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, or_, select

from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack
from phaze.services.cue_generator import CueTrackData, parse_timestamp_string
from phaze.services.stage_status import applied_clause


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


# phaze-hdho: the cue workspace's display order -- alphabetically by artist/event. Both columns
# are nullable ``Text`` (models/tracklist.py), so callers that page or slice on this MUST append a
# ``Tracklist.id`` tiebreaker themselves (paging contract rule 4) -- this tuple alone is not a
# unique sort key.
ELIGIBLE_DISPLAY_ORDER: tuple[Any, ...] = (Tracklist.artist, Tracklist.event)


def _approved_applied_tracklist_base() -> tuple[Select[Any], Select[Any]]:
    """Shared join+filter core for approved, applied tracklists (cue eligibility + review gating).

    Returns ``(base_stmt, has_timestamp_subq)`` -- ``base_stmt`` already carries every predicate
    common to the "eligible" (>=1 timestamped track) and "gated" (no timestamped track) cue review
    sets; callers append the ONE predicate that distinguishes them. ``has_timestamp_subq`` is
    handed back rather than re-built by each caller so both statements reference the identical
    subquery object.

    phaze-dboy: the timestamp-existence check the two callers each compose is scoped to
    ``Tracklist.latest_version_id`` -- NOT "any version ever" -- because that is the ONLY version
    actual generation ever reads (``build_cue_tracks_for_versions`` / the single-version
    ``routers.cue._build_cue_tracks``, both keyed on ``latest_version_id``). A re-scrape can create
    a newer ``latest_version_id`` whose tracks carry no timestamps while an OLDER version still has
    them; scoping to "any version" previously listed that tracklist as eligible with a Generate
    button that could never succeed, permanently inflating ``eligible`` past ``generated`` with no
    way to converge.
    """
    has_timestamp_subq = select(TracklistTrack.version_id).where(TracklistTrack.timestamp.is_not(None)).distinct()
    base_stmt = (
        select(Tracklist, FileRecord)
        .join(FileRecord, Tracklist.file_id == FileRecord.id)
        .where(Tracklist.status == "approved", Tracklist.file_id.is_not(None), applied_clause())
    )
    return base_stmt, has_timestamp_subq


def eligible_tracklist_stmt() -> Select[Any]:
    """Build the base (UNORDERED) SELECT for approved tracklists with EXECUTED files that have >=1 timestamped track.

    Shared by every eligible-tracklist reader so the join/filter logic lives in exactly one place.
    Deliberately carries NO ``ORDER BY`` -- callers compose :data:`ELIGIBLE_DISPLAY_ORDER` (+ the
    mandatory ``Tracklist.id`` tiebreaker) themselves, so it is applied exactly once per statement
    instead of risking a double-appended sort key.
    """
    base_stmt, has_timestamp_subq = _approved_applied_tracklist_base()
    return base_stmt.where(Tracklist.latest_version_id.in_(has_timestamp_subq))


def gated_tracklist_stmt() -> Select[Any]:
    """Build the base (UNORDERED, UNBOUNDED) SELECT for GATED cue sets -- approved + applied, no timestamped track.

    The sibling of :func:`eligible_tracklist_stmt` and, like it, deliberately carries no
    ``ORDER BY`` and no ``LIMIT`` so each caller composes exactly the ones it needs: the card
    reader adds a display sort plus the review row cap, the counter adds neither.
    """
    base_stmt, has_timestamp_subq = _approved_applied_tracklist_base()
    return base_stmt.where(or_(Tracklist.latest_version_id.is_(None), Tracklist.latest_version_id.not_in(has_timestamp_subq)))


async def get_eligible_tracklist_query(session: AsyncSession, *, limit: int | None = None) -> list[tuple[Tracklist, FileRecord]]:
    """Query approved tracklists with EXECUTED files that have at least one timestamped track.

    Ordered by :data:`ELIGIBLE_DISPLAY_ORDER` with ``Tracklist.id`` appended as a tiebreaker so a
    caller-supplied ``limit`` (a bare SQL ``LIMIT``, no ``OFFSET``) is deterministic even when many
    rows tie on source/artist/event -- WITHOUT it, WHICH rows fall inside the cap could vary between
    executions (phaze-hdho).

    Pass ``limit`` to bound the result set at the SQL level. WR-03: the review-card consumer
    (``services.review.get_cue_review_cards``) passes the review row cap so the DB never returns
    more than the render cap -- the eligible half is then genuinely memory-bounded, not just
    loop-capped after materializing every eligible pair.
    """
    stmt = eligible_tracklist_stmt().order_by(*ELIGIBLE_DISPLAY_ORDER, Tracklist.id)
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    return list(result.tuples().all())


# asyncpg refuses a statement with more than 32,767 bind parameters, and SQLAlchemy renders
# ``in_(list)`` as one parameter per element. 1000 keeps every generated IN list two orders of
# magnitude clear of that ceiling while still collapsing the N+1 this module exists to fix.
_ID_CHUNK = 1000


def _id_chunks(ids: Sequence[uuid.UUID]) -> Iterator[list[uuid.UUID]]:
    """Yield ``ids`` in ``_ID_CHUNK``-sized slices; yields nothing for an empty list."""
    for start in range(0, len(ids), _ID_CHUNK):
        yield list(ids[start : start + _ID_CHUNK])


async def build_cue_tracks_for_versions(session: AsyncSession, version_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[CueTrackData]]:
    """Build ``CueTrackData`` lists for MANY tracklist versions in two queries total (phaze-b4u3p).

    Batch form of the single-version track+Discogs-link build: one ``TracklistTrack`` query keyed
    by ``version_id IN (...)`` and one ``DiscogsLink`` query keyed by the resulting ``track_id``s,
    regardless of how many versions are asked for. Fixes the cross-function N+1
    ``get_cue_review_cards -> routers/cue.py::_build_cue_tracks`` used to have: that call sat
    inside the per-card render loop, so a page of N eligible tracklists issued 2N round-trips to
    build previews that are pure in-memory ``.cue`` text (T-60-CUE) -- no disk write, but the DB
    cost still scaled with the render, not with the page.

    A ``version_id`` with no matching tracks (deleted version, or simply none yet) resolves to an
    empty list, identically to what the single-version form returns for a version it cannot find --
    it never queried ``TracklistVersion`` for existence either, only ``TracklistTrack`` rows keyed
    off the version id, so the two forms agree on every input including the "version vanished"
    case.
    """
    # A plain literal per key, NOT dict.fromkeys(version_ids, []) -- fromkeys would alias every
    # key to the SAME list object, which is harmless only as long as every write below replaces
    # a key's value outright (as it does) rather than mutating it in place.
    result: dict[uuid.UUID, list[CueTrackData]] = {version_id: [] for version_id in version_ids}
    if not version_ids:
        return result

    # phaze-b4u3p review finding: batching turns TWO queries per version into two per CHUNK, but a
    # bare ``in_(...)`` over the whole batch is not safe at this call site's scale. The caller passes
    # up to ``_MAX_REVIEW_ROWS`` (2000) version ids, and ``track_ids`` below is the union of EVERY
    # track across all of them -- 2000 concert sets averaging ~17 timestamped tracks is already
    # 34,000 ids. SQLAlchemy renders ``in_(list)`` as one bind parameter per element and asyncpg
    # refuses a statement carrying more than 32,767, so the unchunked form raises InterfaceError.
    # That would be caught by ``get_cue_review_cards``'s outer ``except Exception`` and degrade the
    # WHOLE cue workspace to an empty list with only a log line -- an operator sees "no cue work to
    # review", which is exactly the failure phaze-hcsb's per-card isolation exists to prevent.
    # Chunking also bounds peak memory: without it every version's rows are resident at once.
    #
    # phaze-bk9el.26: both chunk loops below are flagged by the health index as io_in_loop +
    # serial_await_in_loop (lines 178 and 184 as measured at a3fd169a), and the flag carries
    # ``dataflow_verified: true`` -- correctly, no iteration reads what a previous one wrote. They
    # are LEFT SEQUENTIAL ANYWAY. Absence of a data dependence is necessary for an
    # ``asyncio.gather`` and nowhere near sufficient; what actually decides it here is the shared
    # session, which a dataflow check cannot see.
    #
    #   1. THE GATHER WINS NOTHING, MEASURED. ``session`` is one AsyncSession holding one DBAPI
    #      connection, which cannot multiplex. Note it does NOT raise on concurrent use -- the
    #      widely repeated claim that it does is false for the pinned SQLAlchemy 2.0.52; it
    #      silently serializes instead, which is worse, because the "fix" then looks like it
    #      worked. Measured against the test harness, 6 x ``pg_sleep(0.1)`` on ONE session: serial
    #      loop 0.745 s, ``asyncio.gather`` 0.659 s, against an ideal-parallel 0.10 s and an
    #      ideal-serial 0.60 s. The 1.13x is per-call Python overhead, not overlap -- the queries
    #      still went down the wire one at a time.
    #   2. REAL CONCURRENCY WOULD COST THE SNAPSHOT. Overlapping these reads needs one connection
    #      per chunk, i.e. a session and transaction each. That drops the single read transaction
    #      the two queries share -- ``track_ids`` is derived from the first query's rows and fed to
    #      the second -- and multiplies this render's connection-pool draw by the chunk count, from
    #      inside a page render bounded at ``_MAX_REVIEW_ROWS`` (2000) versions.
    #   3. IT WOULD CHANGE THE DEGRADE PATH. This runs inside ``get_cue_review_cards``'s outer
    #      ``except Exception``, whose contract is to degrade the cue workspace rather than 500. A
    #      gather changes which exception surfaces when more than one chunk fails, so the degraded
    #      behavior is observably different -- and every finding on this file sits inside this one
    #      function, which has no dedicated test file of its own (covered incidentally, chiefly by
    #      tests/review/routers/test_cue.py and tests/review/services/test_review_degrade.py).
    tracks: list[TracklistTrack] = []
    for chunk in _id_chunks(version_ids):
        tracks_stmt = select(TracklistTrack).where(TracklistTrack.version_id.in_(chunk))
        tracks.extend((await session.execute(tracks_stmt)).scalars().all())

    track_ids = [t.id for t in tracks]
    discogs_by_track: dict[uuid.UUID, DiscogsLink] = {}
    for chunk in _id_chunks(track_ids):
        discogs_stmt = select(DiscogsLink).where(DiscogsLink.track_id.in_(chunk), DiscogsLink.status == "accepted")
        for link in (await session.execute(discogs_stmt)).scalars().all():
            discogs_by_track[link.track_id] = link

    tracks_by_version: dict[uuid.UUID, list[TracklistTrack]] = defaultdict(list)
    for track in tracks:
        tracks_by_version[track.version_id].append(track)

    for version_id, version_tracks in tracks_by_version.items():
        cue_tracks: list[CueTrackData] = []
        for track in sorted(version_tracks, key=lambda t: t.position):
            discogs_link = discogs_by_track.get(track.id)
            cue_tracks.append(
                CueTrackData(
                    position=track.position,
                    title=track.title,
                    artist=track.artist,
                    timestamp_seconds=parse_timestamp_string(track.timestamp),
                    genre=None,  # DiscogsLink has no genre field (D-09)
                    label=discogs_link.discogs_label if discogs_link else None,
                    year=discogs_link.discogs_year if discogs_link else None,
                )
            )
        result[version_id] = cue_tracks

    return result
