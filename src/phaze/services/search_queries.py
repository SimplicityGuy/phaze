"""Unified search query service -- cross-entity FTS with pagination and facet filters.

Provides ranked, paginated search results from files and tracklists via UNION ALL.
Uses expression-based tsvector (not stored column references) so queries work
identically with or without the GENERATED columns and GIN indexes from migration 009.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Date, String, Text, cast, func, literal_column, select, union_all

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist
from phaze.services.proposal_queries import Pagination


if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class SearchResult:
    """A single search result from either files or tracklists."""

    id: str
    result_type: str  # "file" or "tracklist"
    title: str
    artist: str | None
    genre: str | None
    state: str
    date: str | None  # ISO format YYYY-MM-DD
    rank: float


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
    file_state: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[SearchResult], Pagination]:
    """Search across files and tracklists with FTS, facet filters, and pagination.

    Uses expression-based to_tsvector with plainto_tsquery for safe user input.
    Returns ranked results with result_type discriminator.
    """
    if not query or not query.strip():
        return [], Pagination(page=page, page_size=page_size, total=0)

    ts_query = func.plainto_tsquery("simple", query.strip())

    # -- File subquery --
    file_fts_expr = func.to_tsvector(
        "simple",
        func.concat_ws(
            " ",
            func.coalesce(FileRecord.original_filename, ""),
            func.coalesce(FileMetadata.artist, ""),
            func.coalesce(FileMetadata.title, ""),
            func.coalesce(FileMetadata.album, ""),
            func.coalesce(FileMetadata.genre, ""),
        ),
    )

    file_rank = func.ts_rank(file_fts_expr, ts_query).label("rank")

    file_q = (
        select(
            cast(FileRecord.id, Text).label("id"),
            literal_column("'file'").label("result_type"),
            FileRecord.original_filename.label("title"),
            FileMetadata.artist.label("artist"),
            FileMetadata.genre.label("genre"),
            FileRecord.state.label("state"),
            cast(FileRecord.created_at, Text).label("date"),
            file_rank,
        )
        .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .outerjoin(AnalysisResult, AnalysisResult.file_id == FileRecord.id)
        .where(file_fts_expr.bool_op("@@")(ts_query))
    )

    # Apply file-specific facet filters
    if artist:
        file_q = file_q.where(FileMetadata.artist.ilike(f"%{artist}%"))
    if genre:
        file_q = file_q.where(FileMetadata.genre.ilike(f"%{genre}%"))
    if file_state:
        file_q = file_q.where(FileRecord.state == file_state)
    if bpm_min is not None:
        file_q = file_q.where(AnalysisResult.bpm >= bpm_min)
    if bpm_max is not None:
        file_q = file_q.where(AnalysisResult.bpm <= bpm_max)
    if date_from is not None:
        file_q = file_q.where(cast(FileRecord.created_at, Date) >= date_from)
    if date_to is not None:
        file_q = file_q.where(cast(FileRecord.created_at, Date) <= date_to)

    # -- Tracklist subquery --
    tracklist_fts_expr = func.to_tsvector(
        "simple",
        func.concat_ws(
            " ",
            func.coalesce(Tracklist.artist, ""),
            func.coalesce(Tracklist.event, ""),
        ),
    )

    tracklist_rank = func.ts_rank(tracklist_fts_expr, ts_query).label("rank")

    tracklist_q = select(
        cast(Tracklist.id, Text).label("id"),
        literal_column("'tracklist'").label("result_type"),
        Tracklist.event.label("title"),
        Tracklist.artist.label("artist"),
        literal_column("NULL").cast(String).label("genre"),
        Tracklist.status.label("state"),
        cast(Tracklist.date, Text).label("date"),
        tracklist_rank,
    ).where(tracklist_fts_expr.bool_op("@@")(ts_query))

    # Apply tracklist-specific facet filters
    if artist:
        tracklist_q = tracklist_q.where(Tracklist.artist.ilike(f"%{artist}%"))
    # genre filter does not apply to tracklists
    # bpm filter does not apply to tracklists
    # file_state filter does not apply to tracklists
    if file_state:
        # If filtering by file_state, exclude tracklists entirely
        tracklist_q = tracklist_q.where(literal_column("1") == literal_column("0"))
    if date_from is not None:
        tracklist_q = tracklist_q.where(Tracklist.date >= date_from)
    if date_to is not None:
        tracklist_q = tracklist_q.where(Tracklist.date <= date_to)

    # -- Combine via UNION ALL --
    combined = union_all(file_q, tracklist_q).subquery("combined")

    # Count total
    count_stmt = select(func.count()).select_from(combined)
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    # Paginate with ORDER BY rank DESC
    results_stmt = (
        select(combined)
        .order_by(combined.c.rank.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await session.execute(results_stmt)
    rows = result.all()

    search_results = [
        SearchResult(
            id=str(row.id),
            result_type=row.result_type,
            title=row.title or "",
            artist=row.artist,
            genre=row.genre,
            state=row.state or "",
            date=row.date,
            rank=float(row.rank),
        )
        for row in rows
    ]

    pagination = Pagination(page=page, page_size=page_size, total=total)
    return search_results, pagination


async def get_summary_counts(session: AsyncSession) -> dict[str, int]:
    """Return summary counts of files and tracklists."""
    file_count_stmt = select(func.count()).select_from(FileRecord)
    tracklist_count_stmt = select(func.count()).select_from(Tracklist)

    file_result = await session.execute(file_count_stmt)
    tracklist_result = await session.execute(tracklist_count_stmt)

    return {
        "file_count": file_result.scalar_one(),
        "tracklist_count": tracklist_result.scalar_one(),
    }
