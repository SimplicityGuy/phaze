"""Proposal review UI router -- serves the approval workflow pages."""

from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.proposal import ProposalStatus
from phaze.services.collision import get_collision_ids
from phaze.services.proposal_queries import (
    bulk_update_status,
    get_proposal_stats,
    get_proposal_with_file,
    get_proposals_page,
    update_proposal_status,
)


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
    collision_ids = await get_collision_ids(session)

    context = {
        "request": request,
        "proposals": proposals,
        "pagination": pagination,
        "stats": stats,
        "collision_ids": collision_ids,
        "current_status": effective_status,
        "search_query": q or "",
        "current_sort": sort,
        "current_order": order,
        "current_page": "proposals",
    }

    # HTMX requests get tabs + table fragment (so tab active state updates)
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request=request, name="proposals/partials/proposal_content.html", context=context)

    return templates.TemplateResponse(request=request, name="proposals/list.html", context=context)


@router.patch("/{proposal_id}/approve", response_class=HTMLResponse)
async def approve_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Approve a proposal and return the updated row with OOB stats and toast."""
    proposal = await update_proposal_status(session, proposal_id, ProposalStatus.APPROVED)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    stats = await get_proposal_stats(session)
    return templates.TemplateResponse(
        request=request,
        name="proposals/partials/approve_response.html",
        context={
            "request": request,
            "proposal": proposal,
            "stats": stats,
            "action_label": "approved",
            "toast_message": "Proposal approved.",
            "is_bulk": False,
        },
    )


@router.patch("/{proposal_id}/reject", response_class=HTMLResponse)
async def reject_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Reject a proposal and return the updated row with OOB stats and toast."""
    proposal = await update_proposal_status(session, proposal_id, ProposalStatus.REJECTED)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    stats = await get_proposal_stats(session)
    return templates.TemplateResponse(
        request=request,
        name="proposals/partials/approve_response.html",
        context={
            "request": request,
            "proposal": proposal,
            "stats": stats,
            "action_label": "rejected",
            "toast_message": "Proposal rejected.",
            "is_bulk": False,
        },
    )


@router.patch("/{proposal_id}/undo", response_class=HTMLResponse)
async def undo_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Revert a proposal to pending status and return updated row with OOB stats."""
    proposal = await update_proposal_status(session, proposal_id, ProposalStatus.PENDING)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    stats = await get_proposal_stats(session)
    return templates.TemplateResponse(
        request=request,
        name="proposals/partials/undo_response.html",
        context={
            "request": request,
            "proposal": proposal,
            "stats": stats,
        },
    )


@router.get("/{proposal_id}/detail", response_class=HTMLResponse)
async def row_detail(
    request: Request,
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the expanded detail panel for a proposal row."""
    proposal = await get_proposal_with_file(session, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return templates.TemplateResponse(
        request=request,
        name="proposals/partials/row_detail.html",
        context={"request": request, "proposal": proposal},
    )


@router.patch("/bulk", response_class=HTMLResponse)
async def bulk_action(
    request: Request,
    action: str = Form(...),
    proposal_ids: list[str] = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Bulk approve or reject multiple proposals."""
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")
    status_map = {"approve": ProposalStatus.APPROVED, "reject": ProposalStatus.REJECTED}
    uuids = [uuid.UUID(pid) for pid in proposal_ids]
    count = await bulk_update_status(session, uuids, status_map[action])
    stats = await get_proposal_stats(session)
    return templates.TemplateResponse(
        request=request,
        name="proposals/partials/approve_response.html",
        context={
            "request": request,
            "proposal": None,
            "stats": stats,
            "action_label": action + "d",
            "toast_message": f"{count} proposals {action}d.",
            "is_bulk": True,
            "bulk_ids": [str(uid) for uid in uuids],
        },
    )
