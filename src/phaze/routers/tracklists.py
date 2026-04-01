"""Tracklists admin UI router -- 1001Tracklists management page."""

from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from phaze.database import get_session
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.proposal_queries import Pagination
from phaze.services.tracklist_matcher import compute_match_confidence
from phaze.services.tracklist_scraper import TracklistScraper


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/tracklists", tags=["tracklists"])


async def _get_tracklist_stats(session: AsyncSession) -> dict[str, int]:
    """Query aggregate tracklist counts: total, matched, unmatched."""
    total_result = await session.execute(select(func.count(Tracklist.id)))
    total = total_result.scalar() or 0

    matched_result = await session.execute(select(func.count(Tracklist.id)).where(Tracklist.file_id.is_not(None)))
    matched = matched_result.scalar() or 0

    return {"total": total, "matched": matched, "unmatched": total - matched}


async def _get_tracklist_count(session: AsyncSession, filter_value: str) -> int:
    """Count tracklists by filter type."""
    stmt = select(func.count(Tracklist.id))
    if filter_value == "matched":
        stmt = stmt.where(Tracklist.file_id.is_not(None))
    elif filter_value == "unmatched":
        stmt = stmt.where(Tracklist.file_id.is_(None))
    result = await session.execute(stmt)
    return result.scalar() or 0


@router.get("/", response_class=HTMLResponse)
async def list_tracklists(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=10, le=100),
    filter: str = Query("all"),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the tracklists list page or HTMX partial."""
    stmt = select(Tracklist)

    if filter == "matched":
        stmt = stmt.where(Tracklist.file_id.is_not(None))
    elif filter == "unmatched":
        stmt = stmt.where(Tracklist.file_id.is_(None))

    stmt = stmt.order_by(Tracklist.match_confidence.desc().nulls_last(), Tracklist.created_at.desc())

    # Count total for pagination
    total = await _get_tracklist_count(session, filter)
    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)

    result = await session.execute(stmt)
    tracklists = list(result.scalars().all())

    # Load track counts for each tracklist via latest version
    for tl in tracklists:
        if tl.latest_version_id:
            count_result = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == tl.latest_version_id))
            tl._track_count = count_result.scalar() or 0  # type: ignore[attr-defined]
        else:
            tl._track_count = 0  # type: ignore[attr-defined]

    stats = await _get_tracklist_stats(session)
    pagination = Pagination(page=page, page_size=page_size, total=total)

    context: dict[str, Any] = {
        "request": request,
        "tracklists": tracklists,
        "stats": stats,
        "pagination": pagination,
        "current_page": "tracklists",
        "active_filter": filter,
    }

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request=request, name="tracklists/partials/tracklist_list.html", context=context)

    return templates.TemplateResponse(request=request, name="tracklists/list.html", context=context)


@router.get("/{tracklist_id}/tracks", response_class=HTMLResponse)
async def get_tracks(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return track detail partial for a tracklist's latest version."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()

    tracks: list[TracklistTrack] = []
    if tracklist and tracklist.latest_version_id:
        version_result = await session.execute(
            select(TracklistVersion).options(selectinload(TracklistVersion.tracks)).where(TracklistVersion.id == tracklist.latest_version_id)
        )
        version = version_result.scalar_one_or_none()
        if version:
            tracks = sorted(version.tracks, key=lambda t: t.position)

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/track_detail.html",
        context={"request": request, "tracks": tracks},
    )


@router.post("/{tracklist_id}/link", response_class=HTMLResponse)
async def link_tracklist(
    request: Request,
    tracklist_id: uuid.UUID,
    file_id: uuid.UUID = Form(...),
    confidence: int = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Link a search result to a file."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if tracklist:
        tracklist.file_id = file_id
        tracklist.match_confidence = confidence
        tracklist.auto_linked = False
        await session.commit()

    return await _render_tracklist_list(request, session, "all")


@router.post("/{tracklist_id}/unlink", response_class=HTMLResponse)
async def unlink_tracklist(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Unlink a tracklist from its file."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if tracklist:
        tracklist.file_id = None
        tracklist.match_confidence = None
        tracklist.auto_linked = False
        await session.commit()

    return await _render_tracklist_list(request, session, "all")


@router.post("/{tracklist_id}/rescrape", response_class=HTMLResponse)
async def rescrape_tracklist(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Enqueue a re-scrape job for a tracklist."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if tracklist:
        arq_pool = request.app.state.arq_pool
        await arq_pool.enqueue_job("scrape_and_store_tracklist", str(tracklist_id))

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/tracklist_card.html",
        context={"request": request, "tracklist": tracklist, "rescrape_queued": True},
    )


@router.get("/{tracklist_id}/search", response_class=HTMLResponse)
async def search_better_match(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search for a better match for an existing tracklist."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()

    search_results: list[dict[str, Any]] = []
    query = ""

    if tracklist:
        parts = []
        if tracklist.artist:
            parts.append(tracklist.artist)
        if tracklist.event:
            parts.append(tracklist.event)
        query = " ".join(parts) if parts else ""

        if query:
            scraper = TracklistScraper()
            try:
                raw_results = await scraper.search(query)
                for r in raw_results:
                    conf = compute_match_confidence(
                        tracklist_artist=r.artist,
                        tracklist_event=None,
                        tracklist_date=None,
                        file_artist=tracklist.artist,
                        file_event=tracklist.event,
                        file_date=tracklist.date,
                    )
                    search_results.append(
                        {
                            "external_id": r.external_id,
                            "title": r.title,
                            "url": r.url,
                            "artist": r.artist,
                            "confidence": conf,
                            "tracklist_id": tracklist_id,
                        }
                    )
                search_results.sort(key=lambda x: x["confidence"], reverse=True)
            finally:
                await scraper.close()

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/search_results.html",
        context={"request": request, "results": search_results, "query": query, "file_id": tracklist.file_id if tracklist else None},
    )


@router.post("/search", response_class=HTMLResponse)
async def manual_search(
    request: Request,
    file_id: uuid.UUID = Query(...),
) -> HTMLResponse:
    """Manual search for an unmatched file."""
    arq_pool = request.app.state.arq_pool
    await arq_pool.enqueue_job("search_tracklist", str(file_id))

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/search_results.html",
        context={"request": request, "results": [], "query": "", "file_id": file_id, "loading": True},
    )


@router.post("/{tracklist_id}/undo-link", response_class=HTMLResponse)
async def undo_link(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Undo an auto-link (per D-14, D-23)."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if tracklist:
        tracklist.file_id = None
        tracklist.match_confidence = None
        tracklist.auto_linked = False
        await session.commit()

    return await _render_tracklist_list(request, session, "all")


async def _render_tracklist_list(request: Request, session: AsyncSession, filter_value: str, page: int = 1, page_size: int = 20) -> HTMLResponse:
    """Render the tracklist list partial with stats."""
    stmt = select(Tracklist)
    if filter_value == "matched":
        stmt = stmt.where(Tracklist.file_id.is_not(None))
    elif filter_value == "unmatched":
        stmt = stmt.where(Tracklist.file_id.is_(None))

    stmt = stmt.order_by(Tracklist.match_confidence.desc().nulls_last(), Tracklist.created_at.desc())

    total = await _get_tracklist_count(session, filter_value)
    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)

    result = await session.execute(stmt)
    tracklists = list(result.scalars().all())

    for tl in tracklists:
        if tl.latest_version_id:
            count_result = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == tl.latest_version_id))
            tl._track_count = count_result.scalar() or 0  # type: ignore[attr-defined]
        else:
            tl._track_count = 0  # type: ignore[attr-defined]

    stats = await _get_tracklist_stats(session)
    pagination = Pagination(page=page, page_size=page_size, total=total)

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/tracklist_list.html",
        context={
            "request": request,
            "tracklists": tracklists,
            "stats": stats,
            "pagination": pagination,
            "current_page": "tracklists",
            "active_filter": filter_value,
        },
    )
