"""Unified search UI router -- serves the cross-entity search page."""

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.services.search_queries import SearchResult, get_summary_counts, search


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/search", tags=["search"])


@router.get("/", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str | None = Query(None),
    artist: str | None = Query(None),
    genre: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    bpm_min: float | None = Query(None),
    bpm_max: float | None = Query(None),
    file_state: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=100),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the search page, or an HTMX results fragment."""
    results: list[SearchResult] = []
    pagination = None
    counts: dict[str, int] | None = None

    if q:
        results, pagination = await search(
            session,
            q,
            artist=artist,
            genre=genre,
            date_from=date_from,
            date_to=date_to,
            bpm_min=bpm_min,
            bpm_max=bpm_max,
            file_state=file_state,
            page=page,
            page_size=page_size,
        )
    else:
        counts = await get_summary_counts(session)

    context = {
        "request": request,
        "query": q,
        "results": results,
        "pagination": pagination,
        "counts": counts,
        "current_page": "search",
        "artist": artist,
        "genre": genre,
        "date_from": date_from,
        "date_to": date_to,
        "bpm_min": bpm_min,
        "bpm_max": bpm_max,
        "file_state": file_state,
        "page_size": page_size,
    }

    # HTMX requests get partial content only (results swap target)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request=request, name="search/partials/results_content.html", context=context)

    return templates.TemplateResponse(request=request, name="search/page.html", context=context)
