"""Proposal review UI router -- serves the approval workflow pages."""

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.services.proposal_queries import get_proposal_stats, get_proposals_page


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/proposals", tags=["proposals"])


@router.get("/", response_class=HTMLResponse)
async def list_proposals(
    request: Request,
    status: str | None = Query(None),
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=100),
    sort: str = Query("confidence"),
    order: str = Query("asc"),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the proposal list page, or an HTMX table fragment."""
    # Default to pending filter when no status param provided (D-09)
    effective_status = status if status is not None else "pending"

    proposals, pagination = await get_proposals_page(
        session,
        status=effective_status,
        search=q,
        page=page,
        page_size=page_size,
        sort_by=sort,
        sort_order=order,
    )
    stats = await get_proposal_stats(session)

    context = {
        "request": request,
        "proposals": proposals,
        "pagination": pagination,
        "stats": stats,
        "current_status": effective_status,
        "search_query": q or "",
        "current_sort": sort,
        "current_order": order,
    }

    # HTMX requests get fragment only
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request=request, name="proposals/partials/proposal_table.html", context=context)

    return templates.TemplateResponse(request=request, name="proposals/list.html", context=context)
