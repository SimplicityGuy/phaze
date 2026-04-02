"""Unified search service -- cross-entity FTS across files and tracklists."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import String, func, literal_column, select, union_all
from sqlalchemy.sql.expression import cast

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist
from phaze.services.proposal_queries import Pagination


if TYPE_CHECKING:
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
    date_from: str | None = None,
    date_to: str | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    file_state: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[SearchResult], Pagination]:
    """Search across files and tracklists using full-text search with facet filters."""
    ts_query = func.plainto_tsquery("simple", query)

    # File subquery
    file_tsvector = func.to_tsvector(
        "simple",
        func.concat_ws(" ", FileRecord.original_filename, FileMetadata.artist, FileMetadata.title, FileMetadata.genre),
    )
    file_q = (
        select(
            cast(FileRecord.id, String).label("id"),
            literal_column("'file'").label("result_type"),
            FileRecord.original_filename.label("title"),
            FileMetadata.artist.label("artist"),
            FileMetadata.genre.label("genre"),
            FileRecord.state.label("state"),
            cast(FileRecord.created_at, String).label("date"),
            func.ts_rank(file_tsvector, ts_query).label("rank"),
        )
        .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .outerjoin(AnalysisResult, AnalysisResult.file_id == FileRecord.id)
        .where(file_tsvector.op("@@")(ts_query))
    )

    if artist:
        file_q = file_q.where(FileMetadata.artist.ilike(f"%{artist}%"))
    if genre:
        file_q = file_q.where(FileMetadata.genre.ilike(f"%{genre}%"))
    if date_from:
        file_q = file_q.where(FileRecord.created_at >= date_from)
    if date_to:
        file_q = file_q.where(FileRecord.created_at <= date_to)
    if bpm_min is not None:
        file_q = file_q.where(AnalysisResult.bpm >= bpm_min)
    if bpm_max is not None:
        file_q = file_q.where(AnalysisResult.bpm <= bpm_max)
    if file_state:
        file_q = file_q.where(FileRecord.state == file_state)

    # Tracklist subquery (excluded when file_state filter is active)
    tracklist_tsvector = func.to_tsvector(
        "simple",
        func.concat_ws(" ", Tracklist.artist, Tracklist.event),
    )
    tracklist_q = select(
        cast(Tracklist.id, String).label("id"),
        literal_column("'tracklist'").label("result_type"),
        func.coalesce(Tracklist.event, Tracklist.artist).label("title"),
        Tracklist.artist.label("artist"),
        literal_column("NULL").label("genre"),
        Tracklist.status.label("state"),
        cast(Tracklist.date, String).label("date"),
        func.ts_rank(tracklist_tsvector, ts_query).label("rank"),
    ).where(tracklist_tsvector.op("@@")(ts_query))

    if artist:
        tracklist_q = tracklist_q.where(Tracklist.artist.ilike(f"%{artist}%"))
    if date_from:
        tracklist_q = tracklist_q.where(Tracklist.date >= date_from)
    if date_to:
        tracklist_q = tracklist_q.where(Tracklist.date <= date_to)

    # When file_state filter is active, exclude tracklists entirely
    combined = file_q.subquery() if file_state else union_all(file_q, tracklist_q).subquery()

    # Count total
    count_q = select(func.count()).select_from(combined)
    total_result = await session.execute(count_q)
    total = total_result.scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    results_q = select(combined).order_by(combined.c.rank.desc()).offset(offset).limit(page_size)
    rows = await session.execute(results_q)

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

    pagination = Pagination(page=page, page_size=page_size, total=total)
    return results, pagination


async def get_summary_counts(session: AsyncSession) -> dict[str, int]:
    """Return total file and tracklist counts for the search landing page."""
    file_count_result = await session.execute(select(func.count()).select_from(FileRecord))
    file_count = file_count_result.scalar() or 0

    tracklist_count_result = await session.execute(select(func.count()).select_from(Tracklist))
    tracklist_count = tracklist_count_result.scalar() or 0

    return {"file_count": file_count, "tracklist_count": tracklist_count}
