"""Unified search UI router -- serves the cross-entity search page."""

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MIN_PAGE_SIZE
from phaze.services.search_queries import SearchResult, distinct_artists, search


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/search", tags=["search"])


@router.get("/", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str | None = Query(None),
    artist: str | None = Query(None),
    genre: str | None = Query(None),
    # phaze-z3tx (wire_bounds rule 6): declared as `date`, not `str`, so FastAPI parses/rejects at
    # the boundary. FileRecord.created_at is DateTime and Tracklist.date is Date -- a raw str bind
    # renders `$1::VARCHAR` against both and Postgres has no `timestamp >= varchar` operator, so
    # EVERY value 500s. The type is the bound; there is no width or range to additionally cap.
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    bpm_min: float | None = Query(None),
    bpm_max: float | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the search page, or an HTMX results fragment."""
    # SHELL-05 (D-03/D-04): /search is renamed to the v7.0 ⌘K command palette. A plain
    # (non-HX) GET / bookmark redirects to the shell root with ?palette=1, which the shell
    # Alpine reads to auto-open the palette. The in-page HX results fragment branch below
    # is left intact so live search-as-you-type still works (D-01).
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/?palette=1", status_code=302)

    # v7.0 (RECORD-02, D-03/D-05): the /search HX branch IS the ⌘K grouped command palette.
    # The unified search() results are split into Files / Tracklists / Discogs groups; the
    # Artists group is the one sanctioned additive read (distinct_artists(), gated on
    # len(q) >= 2 — Pitfall 4). The four static Commands (D-03) live in palette_results.html.
    # A non-HX GET already 302-redirected to /?palette=1 above, so this is always the palette.
    results: list[SearchResult] = []
    if q:
        results, _pagination = await search(
            session,
            q,
            artist=artist,
            genre=genre,
            date_from=date_from,
            date_to=date_to,
            bpm_min=bpm_min,
            bpm_max=bpm_max,
            page=page,
            page_size=page_size,
        )

    artists: list[str] = []
    if q and len(q) >= 2:
        artists = await distinct_artists(session, q)

    context = {
        "request": request,
        "query": q,
        "file_results": [r for r in results if r.result_type == "file"],
        "tracklist_results": [r for r in results if r.result_type == "tracklist"],
        "discogs_results": [r for r in results if r.result_type == "discogs_release"],
        "artists": artists,
        "artist": artist,
    }

    return templates.TemplateResponse(request=request, name="search/partials/palette_results.html", context=context)
