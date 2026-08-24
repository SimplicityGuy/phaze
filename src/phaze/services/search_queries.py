"""Unified search service -- cross-entity FTS across files and tracklists."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, Select, String, func, literal_column, select, union_all
from sqlalchemy.sql.expression import cast

from phaze.models.analysis import AnalysisResult
from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist
from phaze.services.like_escape import LIKE_ESCAPE_CHAR, like_wildcard
from phaze.services.pagination import DEFAULT_PAGE_SIZE, paged_stmt, split_sentinel


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class SearchResult:
    """A single search result from files, tracklists, or Discogs releases."""

    id: str
    result_type: str  # "file", "tracklist", or "discogs_release"
    title: str
    artist: str | None
    genre: str | None
    state: str
    date: str | None  # ISO format YYYY-MM-DD
    rank: float


@dataclass
class SearchPagination:
    """Pagination metadata for a page of ``search()`` results.

    phaze-ezic: deliberately has NO ``total`` / ``total_pages`` field, mirroring
    ``phaze.services.pagination.Page`` (contract rule 2) -- ``search()`` is the hot ⌘K
    per-keystroke path, and the pagination contract forbids a whole-corpus COUNT on every render.
    ``has_next`` comes from the ``page_size + 1`` sentinel (``split_sentinel``), never a COUNT.
    """

    page: int
    page_size: int
    has_next: bool


@dataclass(frozen=True)
class SearchFacets:
    """The facet filters ``search()`` applies to its three union branches.

    Grouped into one object because the three ``_*_branch`` builders below each consume a DIFFERENT
    subset of them and every branch must be able to say which facets it honours -- ``genre`` and the
    BPM range exist only on files, ``date_from``/``date_to`` only on files and tracklists, and
    ``artist`` on all three. Passing the individual values through would make each builder's
    signature a positional-argument shape where a caller swapping two ``str | None`` arguments type
    checks cleanly; a named field cannot be transposed by accident.

    A facet left ``None`` (or, for the two text facets, empty) is NOT applied -- the branch is
    unfiltered on that dimension rather than filtered to NULL.
    """

    artist: str | None = None
    genre: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    bpm_min: float | None = None
    bpm_max: float | None = None


# phaze-bk9el.25 -- DUPLICATION RULING for the three branch builders below, recorded so the two
# clone pairs a similarity scanner reports here are not "resolved" by a later pass.
#
# The bead that produced this refactor cited "21.6% duplication" on this file. That figure does NOT
# reproduce at the tip it was re-measured against: there is no dry_violation finding on this module
# at all. What exists are two INTRA-FILE clone pairs, both surfaced only as refactoring suggestions,
# and both are RULED STRUCTURAL -- deliberately left in place:
#
# 1. The facet-application blocks (`if facets.artist: ... .ilike(...)` and the date bounds) look
#    near-identical across the file and tracklist branches, and are not. They apply to DIFFERENT
#    columns on DIFFERENT models with DIFFERENT column types, and the date bound is where that
#    difference bites: `FileRecord.created_at` is a DateTime, so `date_to` needs the exclusive
#    next-day bound (phaze-ql6c) clamped against `date.max` (phaze-u2c4), while `Tracklist.date` is
#    a true Date and `<= date_to` is already inclusive. A shared helper would have to re-introduce
#    that distinction as a parameter, and -- the real cost -- would separate those two bead comments
#    from the single expression they exist to explain, in a module with 8 bug fixes in 6 months.
#    Both fixes ARE that expression; a reader who reaches it must reach them.
#
# 2. The union branches' SELECT column lists repeat the same eight labels. That repetition is what
#    makes the UNION ALL legal: every branch must project the same eight columns in the same order
#    and the NULL literals in the `genre`/`state` slots are load-bearing column-parity placeholders,
#    not copied code. Factoring them into a generated list would hide the one property this union's
#    correctness rests on.
#
# What the split below DOES buy is the thing the duplication figure was standing in for: `search()`
# was one 98-line function at CCN 15 taking 10 parameters. Each branch is now separately readable
# and separately documented about which facets it honours and why it ignores the rest.


def _file_branch(ts_query: ColumnElement[Any], facets: SearchFacets) -> Select[Any]:
    """Build the ``file`` branch of the search union, with the facets files support applied.

    Honours every facet: ``artist``/``genre`` against FileMetadata, the date range against
    ``FileRecord.created_at``, and the BPM range against the outer-joined AnalysisResult. The
    outer joins keep files with no tags and no analysis in the result set -- they can still match on
    filename -- while a facet on either table implicitly narrows to rows that have one.

    phaze-x4ux: match/display the mojibake-repaired filename when one is on record (COALESCE falls
    back to the raw, byte-faithful ``original_filename`` for files ingested before this repair
    shipped and not yet backfilled -- see services/text_repair_backfill.py).
    ``FileMetadata.artist``/``title``/``genre`` need no COALESCE here: they are repaired in place at
    tag-extraction ingest (services/metadata.py::_first_str), so the stored column is already the
    clean text.
    """
    file_display_filename = func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename)
    file_tsvector = func.to_tsvector(
        "simple",
        func.concat_ws(" ", file_display_filename, FileMetadata.artist, FileMetadata.title, FileMetadata.genre),
    )
    file_q: Select[Any] = (
        select(
            cast(FileRecord.id, String).label("id"),
            literal_column("'file'").label("result_type"),
            file_display_filename.label("title"),
            FileMetadata.artist.label("artist"),
            FileMetadata.genre.label("genre"),
            # Phase 90 (PR-A, D-11): the file branch no longer exposes the file pipeline-status column --
            # the search status facet is removed with no derived replacement. The union ``state`` SLOT is
            # kept (a neutral NULL literal here) purely for column-parity so the tracklist/discogs branches
            # can still surface their own ``status`` in that slot.
            literal_column("NULL").label("state"),
            cast(FileRecord.created_at, String).label("date"),
            func.ts_rank(file_tsvector, ts_query).label("rank"),
        )
        .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .outerjoin(AnalysisResult, AnalysisResult.file_id == FileRecord.id)
        .where(file_tsvector.op("@@")(ts_query))
    )

    # LIKE-metacharacter escaping (phaze-ba79): `like_wildcard` escapes `%`/`_`/`\` in the raw
    # facet text before wrapping it in `%...%`, so a `_`-bearing value (e.g. `Coachella_2024`)
    # matches literally instead of `_` acting as a single-char wildcard, and an artist tag
    # containing a literal `\` (e.g. `AC\DC`) round-trips through the ⌘K autocomplete instead of
    # the backslash being silently consumed as the LIKE escape character.
    if facets.artist:
        file_q = file_q.where(FileMetadata.artist.ilike(like_wildcard(facets.artist), escape=LIKE_ESCAPE_CHAR))
    if facets.genre:
        file_q = file_q.where(FileMetadata.genre.ilike(like_wildcard(facets.genre), escape=LIKE_ESCAPE_CHAR))
    if facets.date_from:
        file_q = file_q.where(FileRecord.created_at >= facets.date_from)
    if facets.date_to:
        # phaze-ql6c: FileRecord.created_at is a DateTime, while date_to is a plain date -- comparing
        # created_at <= date_to promotes date_to to midnight, silently excluding every file created
        # later that same day. Use the exclusive next-day bound so date_to is fully inclusive, matching
        # the tracklist branch below (Tracklist.date is a true Date column, so <= is already inclusive).
        #
        # phaze-u2c4: `date_to + timedelta(days=1)` is a PARTIAL function -- it raises OverflowError
        # at `date_to == date.max` (9999-12-31), which a plain `date | None` router param happily
        # accepts and forwards. Clamp the addend's input so the expression stays total for every
        # value `date` can represent: `date.max` and `date.max - timedelta(days=1)` (9999-12-30)
        # both correctly resolve to the exclusive bound `date.max` (there is no later day to exclude).
        file_q = file_q.where(FileRecord.created_at < min(facets.date_to, date.max - timedelta(days=1)) + timedelta(days=1))
    if facets.bpm_min is not None:
        file_q = file_q.where(AnalysisResult.bpm >= facets.bpm_min)
    if facets.bpm_max is not None:
        file_q = file_q.where(AnalysisResult.bpm <= facets.bpm_max)
    return file_q


def _tracklist_branch(ts_query: ColumnElement[Any], facets: SearchFacets) -> Select[Any]:
    """Build the ``tracklist`` branch of the search union, with the facets tracklists support.

    Honours ``artist`` and the date range only. ``genre`` and the BPM range have no tracklist
    equivalent, so a search carrying either still returns tracklists -- deliberately: the facets are
    per-branch narrowings, not a whole-union filter, and the ``genre`` slot below is the NULL literal
    that keeps this branch's column list union-compatible with the file branch's.

    Unlike the file branch, ``Tracklist.date`` is a true Date column, so ``<= date_to`` is already
    inclusive and needs none of the file branch's exclusive next-day bound.
    """
    tracklist_tsvector = func.to_tsvector(
        "simple",
        func.concat_ws(" ", Tracklist.artist, Tracklist.event),
    )
    tracklist_q: Select[Any] = select(
        cast(Tracklist.id, String).label("id"),
        literal_column("'tracklist'").label("result_type"),
        func.coalesce(Tracklist.event, Tracklist.artist).label("title"),
        Tracklist.artist.label("artist"),
        literal_column("NULL").label("genre"),
        Tracklist.status.label("state"),
        cast(Tracklist.date, String).label("date"),
        func.ts_rank(tracklist_tsvector, ts_query).label("rank"),
    ).where(tracklist_tsvector.op("@@")(ts_query))

    if facets.artist:
        tracklist_q = tracklist_q.where(Tracklist.artist.ilike(like_wildcard(facets.artist), escape=LIKE_ESCAPE_CHAR))
    if facets.date_from:
        tracklist_q = tracklist_q.where(Tracklist.date >= facets.date_from)
    if facets.date_to:
        tracklist_q = tracklist_q.where(Tracklist.date <= facets.date_to)
    return tracklist_q


def _discogs_branch(ts_query: ColumnElement[Any], facets: SearchFacets) -> Select[Any]:
    """Build the ``discogs_release`` branch of the search union (accepted links only, per D-09).

    Honours ``artist`` only -- a Discogs link carries no genre, no BPM, and no date comparable to the
    other two branches (``discogs_year`` is a year, cast to a String for the shared ``date`` slot, so
    it cannot participate in a date-range comparison). The ``status == "accepted"`` predicate is NOT
    a facet: it is the D-09 rule that unaccepted links never surface in search at all.
    """
    discogs_tsvector = func.to_tsvector(
        "simple",
        func.concat_ws(" ", DiscogsLink.discogs_artist, DiscogsLink.discogs_title),
    )
    discogs_q: Select[Any] = (
        select(
            cast(DiscogsLink.id, String).label("id"),
            literal_column("'discogs_release'").label("result_type"),
            DiscogsLink.discogs_title.label("title"),
            DiscogsLink.discogs_artist.label("artist"),
            literal_column("NULL").label("genre"),
            DiscogsLink.status.label("state"),
            cast(DiscogsLink.discogs_year, String).label("date"),
            func.ts_rank(discogs_tsvector, ts_query).label("rank"),
        )
        .where(discogs_tsvector.op("@@")(ts_query))
        .where(DiscogsLink.status == "accepted")
    )

    if facets.artist:
        discogs_q = discogs_q.where(DiscogsLink.discogs_artist.ilike(like_wildcard(facets.artist), escape=LIKE_ESCAPE_CHAR))
    return discogs_q


async def search(
    session: AsyncSession,
    query: str,
    *,
    artist: str | None = None,
    genre: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[SearchResult], SearchPagination]:
    """Search across files and tracklists using full-text search with facet filters.

    The three union branches are built by :func:`_file_branch`, :func:`_tracklist_branch` and
    :func:`_discogs_branch`; each documents which facets it honours and why it ignores the rest.
    """
    ts_query = func.plainto_tsquery("simple", query)
    facets = SearchFacets(artist=artist, genre=genre, date_from=date_from, date_to=date_to, bpm_min=bpm_min, bpm_max=bpm_max)

    # Phase 90 (PR-A, D-11): the pipeline-status facet is gone, so files / tracklists / discogs ALWAYS union.
    combined = union_all(_file_branch(ts_query, facets), _tracklist_branch(ts_query, facets), _discogs_branch(ts_query, facets)).subquery()

    # phaze-ezic: a whole-corpus COUNT used to run here on every call (`select(func.count())
    # .select_from(combined)`), i.e. on every ⌘K keystroke -- exactly the per-render full scan
    # the T-87-11 DoS mitigation (pagination contract rule 2) forbids. Its only consumer was
    # `Pagination.total`, which the sole caller (routers/search.py) discards entirely. Deleted;
    # `has_next` below already comes from the page_size+1 sentinel, never a COUNT.

    # Fetch page -- ts_rank ties are common (the discogs/tracklist branches score only two
    # concat_ws fields and single-term matches all land on the same default weight), so `rank`
    # alone leaves the ORDER BY only partially defined. Route through the paging contract's
    # paged_stmt (phaze.services.pagination, rule 4) so a MANDATORY unique tiebreaker is appended
    # after `rank DESC`, giving every row a total order and keeping LIMIT/OFFSET from skipping or
    # duplicating rows across pages (phaze-39ss). `combined.c.id` alone is NOT unique across the
    # union -- it is a String-cast of three independent primary-key spaces (FileRecord / Tracklist
    # / DiscogsLink), so a file id and a tracklist id can collide as the same string. Pairing it
    # with `result_type` makes the tiebreaker tuple genuinely unique across the whole result set.
    results_q = paged_stmt(
        select(combined),
        page=page,
        page_size=page_size,
        order_by=(combined.c.rank.desc(),),
        tiebreaker=(combined.c.result_type.asc(), combined.c.id.asc()),
    )
    raw_rows = (await session.execute(results_q)).all()
    rows, has_next = split_sentinel(raw_rows, page_size)

    results = [
        SearchResult(
            id=str(row.id),
            result_type=row.result_type,
            title=row.title or "",
            artist=row.artist,
            genre=row.genre,
            state=row.state or "",
            date=row.date[:10] if row.date and len(str(row.date)) >= 10 else row.date,
            rank=float(row.rank),
        )
        for row in rows
    ]

    pagination = SearchPagination(page=page, page_size=page_size, has_next=has_next)
    return results, pagination


async def distinct_artists(session: AsyncSession, query: str, *, limit: int = 20) -> list[str]:
    """Return DISTINCT non-NULL artist names matching ``query`` across files and tracklists.

    The one sanctioned additive read for the ⌘K palette Artists group (RECORD-02 / D-05): a
    read-only ``SELECT DISTINCT`` over ``FileMetadata.artist`` + ``Tracklist.artist``, each
    filtered by ``IS NOT NULL`` and a parameterized (bound) ILIKE. The ``%{query}%`` pattern is
    bound by SQLAlchemy, never string-interpolated into the SQL text (T-61-06) — mirrors the
    existing ``search()`` ``artist=`` filter, including its LIKE-metacharacter escaping
    (phaze-ba79): without it, an artist containing ``%``, ``_``, or a literal backslash returned here could never
    match itself when round-tripped into ``search(artist=...)``.

    Pitfall 4: both artist columns are UNINDEXED ``Text``. The caller owns the debounce
    (>=150-250ms), a ``len(query) >= 2`` gate, and relies on the ``LIMIT`` here to bound the
    per-keystroke scan. A real trigram index is deferred (a schema change, out of the
    presentation scope of this phase).

    phaze-p0ytz: ``DISTINCT`` + ``LIMIT`` with no ``ORDER BY`` lets Postgres return ANY
    ``limit`` of the matching distinct artists, and the chosen subset is plan-dependent —
    two identical calls can return different results, so a matching artist can silently
    (and unstably) drop out of the truncated set. Ordering alphabetically before the
    ``LIMIT`` makes the truncation deterministic, mirroring the mandatory tiebreaker
    ``search()`` appends via ``paged_stmt`` above.
    """
    like = like_wildcard(query)
    fm = select(FileMetadata.artist).where(FileMetadata.artist.is_not(None), FileMetadata.artist.ilike(like, escape=LIKE_ESCAPE_CHAR))
    tl = select(Tracklist.artist).where(Tracklist.artist.is_not(None), Tracklist.artist.ilike(like, escape=LIKE_ESCAPE_CHAR))
    combined = union_all(fm, tl).subquery()
    rows = await session.execute(select(combined.c.artist).distinct().order_by(combined.c.artist).limit(limit))
    return [artist for (artist,) in rows if artist]


async def get_summary_counts(session: AsyncSession) -> dict[str, int]:
    """Return total file, tracklist, and Discogs link counts for the search landing page."""
    file_count_result = await session.execute(select(func.count()).select_from(FileRecord))
    file_count = file_count_result.scalar() or 0

    tracklist_count_result = await session.execute(select(func.count()).select_from(Tracklist))
    tracklist_count = tracklist_count_result.scalar() or 0

    discogs_count_result = await session.execute(select(func.count()).select_from(DiscogsLink).where(DiscogsLink.status == "accepted"))
    discogs_count = discogs_count_result.scalar() or 0

    return {"file_count": file_count, "tracklist_count": tracklist_count, "discogs_count": discogs_count}
