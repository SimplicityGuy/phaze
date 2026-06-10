"""Proposal review UI router -- serves the approval workflow pages."""

from collections.abc import Sequence
from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.analysis import AnalysisWindow
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

# Numeric coordinate spaces for server-rendered SVG. CSS scales these to fit;
# coordinate attributes stay numeric-only so no essentia-derived string ever
# reaches an SVG geometry attribute (XSS hardening, RESEARCH V5 / T-31-06-01).
TIMELINE_W = 1000.0
TIMELINE_H = 120.0
SPARK_W = 80.0
SPARK_H = 24.0


def _bpm_polyline_points(windows: Sequence[AnalysisWindow], total_sec: float, width: float, height: float) -> str:
    """Map fine-window BPM values onto a numeric ``x,y`` polyline path string.

    Windows lacking a ``bpm`` are skipped. Coordinates are rounded floats only,
    so the rendered ``points`` attribute can never carry injected markup.
    """
    pairs = [(w.start_sec, w.end_sec, b) for w in windows if (b := w.bpm) is not None]
    if not pairs or total_sec <= 0:
        return ""
    bpms = [b for _, _, b in pairs]
    lo, hi = min(bpms), max(bpms)
    span = hi - lo
    coords: list[str] = []
    for start_sec, end_sec, bpm in pairs:
        midpoint = (start_sec + end_sec) / 2.0
        x = midpoint / total_sec * width
        # Higher BPM sits higher on the chart (smaller y in SVG's top-left origin).
        y = height / 2.0 if span <= 0 else height - ((bpm - lo) / span) * height
        coords.append(f"{x:.2f},{y:.2f}")
    return " ".join(coords)


def _hue_for(label: str) -> int:
    """Deterministically derive an integer HSL hue (0-359) from a label string.

    Integer-only output keeps the ribbon colour a safe numeric CSS value while
    giving each distinct key/mood/style a stable, distinguishable colour.
    """
    return sum(ord(c) for c in label) % 360


def _ribbons(windows: Sequence[AnalysisWindow], attr: str, total_sec: float) -> list[dict[str, object]]:
    """Build width-proportional ribbon descriptors for one lane (key/mood/style).

    Each ribbon carries the raw ``label`` (rendered through Jinja2 autoescaping
    by the template -- never ``| safe``), a numeric ``width_pct`` proportional to
    the window's duration, and a numeric ``hue``.
    """
    if total_sec <= 0:
        return []
    ribbons: list[dict[str, object]] = []
    for w in windows:
        label = getattr(w, attr)
        if label is None:
            continue
        width_pct = round((w.end_sec - w.start_sec) / total_sec * 100.0, 4)
        ribbons.append({"label": label, "width_pct": width_pct, "hue": _hue_for(str(label))})
    return ribbons


async def _build_sparklines(session: AsyncSession, file_ids: list[uuid.UUID]) -> dict[str, str]:
    """Fetch fine-tier BPM windows for a page of files and pre-render sparkline paths.

    Returns a mapping of ``str(file_id) -> polyline points`` so the row template
    can render a compact inline BPM sparkline without an N+1 query per row.
    """
    if not file_ids:
        return {}
    stmt = (
        select(AnalysisWindow)
        .where(AnalysisWindow.file_id.in_(file_ids))
        .where(AnalysisWindow.tier == "fine")
        .order_by(AnalysisWindow.file_id, AnalysisWindow.window_index)
    )
    result = await session.execute(stmt)
    by_file: dict[uuid.UUID, list[AnalysisWindow]] = {}
    for window in result.scalars().all():
        by_file.setdefault(window.file_id, []).append(window)
    sparklines: dict[str, str] = {}
    for fid, windows in by_file.items():
        total_sec = max((w.end_sec for w in windows), default=0.0)
        sparklines[str(fid)] = _bpm_polyline_points(windows, total_sec, SPARK_W, SPARK_H)
    return sparklines


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
    sparklines = await _build_sparklines(session, [p.file_id for p in proposals])

    context = {
        "request": request,
        "proposals": proposals,
        "pagination": pagination,
        "stats": stats,
        "collision_ids": collision_ids,
        "sparklines": sparklines,
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


@router.get("/{proposal_id}/timeline", response_class=HTMLResponse)
async def proposal_timeline(
    request: Request,
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the multi-lane analysis-window timeline fragment for a proposal's file.

    Resolves the proposal to its ``file_id`` and renders the windows scoped
    strictly by that ``file_id`` (broken-access-control mitigation, T-31-06-02),
    behind the same review-UI surface as the rest of this router.
    """
    proposal = await get_proposal_with_file(session, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")

    file_id = proposal.file_id
    stmt = select(AnalysisWindow).where(AnalysisWindow.file_id == file_id).order_by(AnalysisWindow.tier, AnalysisWindow.window_index)
    result = await session.execute(stmt)
    windows = list(result.scalars().all())

    fine = [w for w in windows if w.tier == "fine"]
    coarse = [w for w in windows if w.tier == "coarse"]
    total_sec = max((w.end_sec for w in windows), default=0.0)

    return templates.TemplateResponse(
        request=request,
        name="proposals/partials/analysis_timeline.html",
        context={
            "request": request,
            "proposal": proposal,
            "has_windows": bool(windows),
            "timeline_w": TIMELINE_W,
            "timeline_h": TIMELINE_H,
            "bpm_points": _bpm_polyline_points(fine, total_sec, TIMELINE_W, TIMELINE_H),
            "key_ribbons": _ribbons(fine, "musical_key", total_sec),
            "mood_ribbons": _ribbons(coarse, "mood", total_sec),
            "style_ribbons": _ribbons(coarse, "style", total_sec),
        },
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
