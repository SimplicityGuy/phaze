"""Proposal review UI router -- serves the approval workflow pages."""

from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.proposal import APPROVE_REJECT_FROM, UNDO_FROM, ProposalStatus, RenameProposal

# phaze-a6hm.11: the propose workspace's container id + list context, so a bulk action issued
# from that surface re-renders it through the SAME builder its GET uses. The edge is one-way --
# routers/shell.py does not import this module -- so there is no cycle.
from phaze.routers.shell import build_propose_list_context
from phaze.services.proposal_queries import (
    ProposalPendingConflictError,
    ProposalTransitionError,
    approve_pending_above_confidence,
    bulk_update_status,
    get_proposal_stats,
    get_proposal_with_file,
    update_proposal_fields,
    update_proposal_status,
)


# The review-UI state machine (phaze-uu17) now lives on the model, beside the enum it constrains --
# phaze-a6hm.11 hoisted it there because the propose workspace render needs the SAME fact to decide
# which rows may be offered a checkbox. These aliases keep this module's existing spelling.
_APPROVE_REJECT_FROM = APPROVE_REJECT_FROM
_UNDO_FROM = UNDO_FROM


def _bulk_toast(action: str, *, requested: int, applied: int) -> str:
    """Phrase the bulk result so it reports REAL transitions, never selection size (phaze-uu17).

    ``requested`` is how many well-formed ids the browser sent; ``applied`` is the UPDATE's rowcount
    after the ``allowed_from`` guard. They differ whenever the selection contained rows that are no
    longer PENDING -- terminal EXECUTED/FAILED rows reachable from the "All" tab, or rows another
    tab/session actioned since this page was rendered. The gap is exactly the information the
    operator needs and the one a naive ``f"{len(ids)} approved"`` destroys, so it is stated rather
    than smoothed over: silence about 38 skipped rows reads as success on all 50.

    The zero case gets its own sentence because "0 approved" alone invites the operator to conclude
    the button is broken and click it harder, when in fact the answer is complete and stable.
    """
    verb = f"{action}d"
    if applied == requested:
        return f"{applied} proposal{'' if applied == 1 else 's'} {verb}."
    skipped = requested - applied
    if applied == 0:
        return f"Nothing {verb} — all {skipped} selected proposal{'' if skipped == 1 else 's'} had already been actioned."
    return f"{applied} proposal{'' if applied == 1 else 's'} {verb} · {skipped} skipped (already actioned)."


async def _guarded_status_update(
    session: AsyncSession,
    proposal_id: uuid.UUID,
    new_status: ProposalStatus,
    allowed_from: frozenset[ProposalStatus],
) -> RenameProposal | None:
    """Call update_proposal_status, translating state-machine errors into 409 responses."""
    try:
        return await update_proposal_status(session, proposal_id, new_status, allowed_from=allowed_from)
    except ProposalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProposalPendingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# phaze-3a2j: the v7 diff-row workspaces (Rename / Move / Record slide-in) render rows from the
# shared pipeline/partials/_diff_row.html partial and hx-target each row's own <div>. The mutation
# routes historically returned the LEGACY <tr>-based proposal_row.html, so a swap dropped broken
# table-row markup into the div list and the Alpine bindings threw ReferenceErrors. When a request
# originates from one of these workspaces (identified by its HX-Target = "{prefix}-{proposal_id}"),
# the route must instead return _diff_row.html with the matching prefix, facet, and lifecycle state.
_V7_ROW_FACETS: dict[str, str] = {"rename-row": "filename", "record-row": "filename", "move-row": "path"}

# phaze-71hi: bulk_approve_high_confidence has no proposal_id in its URL, so it can't reuse
# _v7_row_target's "{prefix}-{proposal_id}" match. Instead its two callers (rename_workspace.html /
# move_workspace.html) each hx-target their own small status div by a FIXED id -- map that id
# straight to the (row_id_prefix, facet) pair _diff_row_context needs for the OOB row fragments.
_BULK_HIGH_CONFIDENCE_TARGETS: dict[str, tuple[str, str]] = {
    "rename-trigger-response": ("rename-row", "filename"),
    "move-trigger-response": ("move-row", "path"),
}

# phaze-3tj4: map a proposal's real status to the v7 diff-row lifecycle string so a mutation route
# renders the row's actual affordances instead of hardcoding "pending". The reject route names its
# state "skipped" (see reject_proposal above), so REJECTED maps there.
_ROW_STATE_FOR_STATUS: dict[ProposalStatus, str] = {
    ProposalStatus.PENDING: "pending",
    ProposalStatus.APPROVED: "approved",
    ProposalStatus.REJECTED: "skipped",
    ProposalStatus.EXECUTED: "executed",
    ProposalStatus.FAILED: "failed",
}


def _v7_row_target(request: Request, proposal_id: uuid.UUID) -> tuple[str, str] | None:
    """Return (row_id_prefix, facet) when the request came from a v7 diff-row workspace, else None."""
    hx_target = request.headers.get("HX-Target", "")
    for prefix, facet in _V7_ROW_FACETS.items():
        if hx_target == f"{prefix}-{proposal_id}":
            return prefix, facet
    return None


def _diff_row_context(proposal: RenameProposal, row_id_prefix: str, facet: str, row_state: str, *, oob: bool = False) -> dict[str, object]:
    """Build the render context _diff_row.html expects for one proposal (phaze-3a2j).

    Shared by the single-row mutation responses (:func:`_diff_row_response`) and the v7
    bulk-approve-high-confidence response (phaze-71hi), which needs a LIST of these -- rendered
    with ``oob=True`` -- to hx-swap-oob every row a bulk apply just transitioned, since the
    rename/move workspaces have no row poll (R-2) to pick the change up on their own.
    """
    file_record = proposal.file
    if facet == "path":
        before = file_record.current_path
        after = proposal.proposed_path or ""
        edit_facet = "path"
    else:
        before = file_record.original_filename
        after = proposal.proposed_filename
        edit_facet = "filename"
    pid = proposal.id
    return {
        "row_id_prefix": row_id_prefix,
        "pid": pid,
        "file": file_record.original_filename,
        "original_path": file_record.current_path,
        "before": before,
        "after": after,
        "approve_url": f"/proposals/{pid}/approve",
        "skip_url": f"/proposals/{pid}/reject",
        "undo_url": f"/proposals/{pid}/undo",
        "edit_url": f"/proposals/{pid}/edit",
        "edit_facet": edit_facet,
        "row_state": row_state,
        "oob": oob,
    }


def _diff_row_response(request: Request, proposal: RenameProposal, row_id_prefix: str, facet: str, row_state: str) -> HTMLResponse:
    """Render the shared _diff_row.html for a v7 workspace row swap (phaze-3a2j)."""
    context = _diff_row_context(proposal, row_id_prefix, facet, row_state)
    context["request"] = request
    return templates.TemplateResponse(request=request, name="pipeline/partials/_diff_row.html", context=context)


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


class BpmSpark(NamedTuple):
    """A rendered BPM spark: the numeric SVG ``points`` plus the min/max it spans.

    ``lo``/``hi`` are numeric ``float``s taken straight from ``w.bpm`` (never
    essentia strings) and are used ONLY as HTML label text -- they are deliberately
    NOT written into any SVG geometry attribute, preserving the coordinate-numeric-only
    XSS hardening invariant above. ``window_count`` is the number of bpm-bearing windows.
    """

    points: str
    lo: float | None
    hi: float | None
    window_count: int


def _bpm_spark(windows: Sequence[AnalysisWindow], total_sec: float, width: float, height: float) -> BpmSpark:
    """Map fine-window BPM values onto a numeric ``x,y`` polyline path string.

    Windows lacking a ``bpm`` are skipped. Coordinates are rounded floats only,
    so the rendered ``points`` attribute can never carry injected markup. The
    surfaced ``lo``/``hi`` (the min/max BPM) feed HTML gutter labels only.
    """
    pairs = [(w.start_sec, w.end_sec, b) for w in windows if (b := w.bpm) is not None]
    if not pairs or total_sec <= 0:
        return BpmSpark("", None, None, 0)
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
    return BpmSpark(" ".join(coords), lo, hi, len(pairs))


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


@router.get("/", response_class=RedirectResponse)
async def list_proposals() -> RedirectResponse:
    """SHELL-05 (D-03): resolve a legacy ``/proposals/`` bookmark into the v7.0 shell.

    phaze-y4s6: this used to also serve an in-page HX-filtered/paginated/sorted table (rendering
    ``proposals/partials/proposal_list.html``, composed of ``proposal_table.html`` +
    ``bulk_actions.html`` + ``pagination.html``). The live v7.0 propose workspace
    (``pipeline/partials/propose_workspace.html`` / ``_propose_list.html``) renders its own list
    through ``routers/shell.py``'s ``_render_stage`` instead -- ``proposals/partials/filter_tabs.html``
    and ``search_box.html`` (still live, included directly by ``propose_workspace.html``) target
    ``/s/propose``, never this bare path. There was no live caller left to preserve an HX-filter
    branch for; the dead list/pagination templates were deleted outright.
    """
    return RedirectResponse(url="/s/propose", status_code=302)


@router.patch("/{proposal_id}/approve", response_class=HTMLResponse)
async def approve_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Approve a proposal and return the updated row with OOB stats and toast."""
    proposal = await _guarded_status_update(session, proposal_id, ProposalStatus.APPROVED, _APPROVE_REJECT_FROM)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    v7 = _v7_row_target(request, proposal_id)
    if v7 is not None:
        return _diff_row_response(request, proposal, v7[0], v7[1], row_state="approved")
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
    proposal = await _guarded_status_update(session, proposal_id, ProposalStatus.REJECTED, _APPROVE_REJECT_FROM)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    v7 = _v7_row_target(request, proposal_id)
    if v7 is not None:
        return _diff_row_response(request, proposal, v7[0], v7[1], row_state="skipped")
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
    proposal = await _guarded_status_update(session, proposal_id, ProposalStatus.PENDING, _UNDO_FROM)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    v7 = _v7_row_target(request, proposal_id)
    if v7 is not None:
        return _diff_row_response(request, proposal, v7[0], v7[1], row_state="pending")
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

    # Phase 44 (44-04): also fetch the 1:1 AnalysisResult so the timeline can render the
    # "Sampled — more data available" badge (+ coverage tooltip) and the "Deepen analysis"
    # button. scalar_one_or_none() -> None for a file with no analysis row yet; the badge
    # template gates on `analysis is not none and analysis.sampled`, so a missing or NULL/false
    # sampled value renders NOTHING (D-03 / T-44-12), never an error.
    analysis_result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    analysis = analysis_result.scalar_one_or_none()

    spark = _bpm_spark(fine, total_sec, TIMELINE_W, TIMELINE_H)
    return templates.TemplateResponse(
        request=request,
        name="proposals/partials/analysis_timeline.html",
        context={
            "request": request,
            "proposal": proposal,
            "analysis": analysis,
            "file_id": file_id,
            "has_windows": bool(windows),
            "timeline_w": TIMELINE_W,
            "timeline_h": TIMELINE_H,
            "bpm_points": spark.points,
            "bpm_lo": spark.lo,
            "bpm_hi": spark.hi,
            "key_ribbons": _ribbons(fine, "musical_key", total_sec),
            "mood_ribbons": _ribbons(coarse, "mood", total_sec),
            "style_ribbons": _ribbons(coarse, "style", total_sec),
        },
    )


def _validate_proposed_value(proposed: str, *, is_path: bool) -> str:
    """Validate + normalize an operator-edited ``proposed`` value (D-05, T-60-02).

    Rejects empty/whitespace-only values, any ``..`` (path-traversal), and NUL/control chars;
    the filename facet additionally rejects any ``/``. The path facet mirrors ``store_proposals``
    normalization (``strip('/')`` + collapse ``//``). Raises ``HTTPException(400)`` on any
    violation so a hostile edit can never reach the persisted row a later physical move consumes.
    """
    value = proposed.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Proposed value must not be empty")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise HTTPException(status_code=400, detail="Proposed value must not contain control characters")
    if ".." in value:
        raise HTTPException(status_code=400, detail="Proposed value must not contain '..'")
    if not is_path:
        if "/" in value:
            raise HTTPException(status_code=400, detail="Proposed filename must not contain '/'")
        return value
    # Path facet: mirror services/proposal.py store_proposals sanitize (strip('/') + collapse '//').
    value = value.strip("/")
    while "//" in value:
        value = value.replace("//", "/")
    if not value:
        raise HTTPException(status_code=400, detail="Proposed path must not be empty")
    return value


@router.patch("/bulk-approve-high-confidence", response_class=HTMLResponse)
async def bulk_approve_high_confidence(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """REVIEW-02 (D-02): approve every PENDING proposal with confidence >= 0.9.

    Server-evaluated predicate -- the fixed 0.9 threshold is re-queried at submit and drives the
    result. This route reads NO client-supplied ``proposal_ids`` id-list (unlike ``bulk_action``),
    so a stale or forged selection under the counts-only poll can never mass-approve (the REVIEW-02
    correctness core). Mirrors ``tracklists.reject_low_confidence``. NULL-confidence rows are
    excluded by the SQL predicate (Pitfall 2). The threshold is fixed server-side (REVIEW-06 defers
    configurability). Same route serves the Rename/Path AND Move queues (both ``RenameProposal``).

    phaze-71hi: rename_workspace.html / move_workspace.html hx-target this at their small
    ``#rename-trigger-response`` / ``#move-trigger-response`` status div, NOT a container that
    re-renders the row list, and the workspaces deliberately run no row poll (R-2) that could pick
    the change up on its own. The legacy ``approve_response.html`` fork below (proposal=None, an
    OOB ``#stats-bar`` that does not exist in the v7 shell) therefore left every just-approved row
    rendered PENDING with live APPROVE/EDIT/SKIP controls, so a later click 409'd silently
    (``APPROVE_REJECT_FROM = frozenset({PENDING})``). When the request comes from one of those two
    v7 targets, snapshot the PENDING rows the predicate is ABOUT to approve before the guarded
    update runs, then answer with the toast plus one ``hx-swap-oob`` ``_diff_row.html`` fragment
    (``row_state="approved"``) per row this request actually transitioned -- rows a concurrent
    request already claimed are simply not re-fetched as APPROVED and are left alone.
    """
    threshold = 0.9
    v7_target = _BULK_HIGH_CONFIDENCE_TARGETS.get(request.headers.get("HX-Target", ""))
    candidate_ids: list[uuid.UUID] = []
    if v7_target is not None:
        candidate_stmt = select(RenameProposal.id).where(
            RenameProposal.status == ProposalStatus.PENDING,
            RenameProposal.confidence >= threshold,
        )
        candidate_ids = list((await session.execute(candidate_stmt)).scalars().all())

    count = await approve_pending_above_confidence(session, threshold=threshold)
    toast_message = f"{count} proposals approved." if count else "Nothing matched -- no pending rows meet the >=90% confidence predicate right now."

    if v7_target is not None:
        row_id_prefix, facet = v7_target
        approved_rows: list[dict[str, object]] = []
        if candidate_ids:
            rows_stmt = select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id.in_(candidate_ids))
            proposals = (await session.execute(rows_stmt)).scalars().all()
            approved_rows = [
                _diff_row_context(p, row_id_prefix, facet, "approved", oob=True) for p in proposals if p.status == ProposalStatus.APPROVED
            ]
        return templates.TemplateResponse(
            request=request,
            name="proposals/partials/_bulk_approve_high_confidence_response.html",
            context={
                "request": request,
                "toast_message": toast_message,
                "is_bulk": True,
                "approved_rows": approved_rows,
            },
        )

    stats = await get_proposal_stats(session)
    return templates.TemplateResponse(
        request=request,
        name="proposals/partials/approve_response.html",
        context={
            "request": request,
            "proposal": None,
            "stats": stats,
            "action_label": "approved",
            "toast_message": toast_message,
            "is_bulk": True,
            "bulk_ids": [],
        },
    )


@router.patch("/{proposal_id}/edit", response_class=HTMLResponse)
async def edit_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    proposed: str = Form(...),
    facet: str = Form("filename"),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """REVIEW-01 (D-05): persist an operator edit to a proposal BEFORE approve.

    Thin write over the persisted row -- validates the edited value (T-60-02) then updates
    ``proposed_filename`` (``facet="filename"``) or ``proposed_path`` (``facet="path"``). The row
    stays PENDING and the LLM is NOT re-run (generation logic untouched). Returns only the row
    markup so ``hx-swap="outerHTML"`` replaces just that row (R-6). Plan 60-02 re-points this at the
    shared ``pipeline/partials/_diff_row.html`` partial; until then the existing proposals row
    partial keeps this endpoint's own test green.
    """
    is_path = facet == "path"
    value = _validate_proposed_value(proposed, is_path=is_path)
    # phaze-3tj4: edits are only legal on PENDING rows. Without this guard an edit that lands after
    # a concurrent approval rewrote the proposed_path an APPROVED row feeds into execution_dispatch,
    # redirecting a reviewed move to an unreviewed destination (and edits to terminal EXECUTED/FAILED
    # rows corrupted the historical record). update_proposal_fields now evaluates the from-state
    # inside the UPDATE and raises ProposalTransitionError, which we translate to 409.
    try:
        if is_path:
            proposal = await update_proposal_fields(session, proposal_id, proposed_path=value, allowed_from=_APPROVE_REJECT_FROM)
        else:
            proposal = await update_proposal_fields(session, proposal_id, proposed_filename=value, allowed_from=_APPROVE_REJECT_FROM)
    except ProposalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    # phaze-3a2j: a v7 diff-row workspace expects the shared _diff_row.html back (the row stays
    # PENDING with the edited "after" value), not the legacy <tr> proposal_row.html.
    # phaze-3tj4: derive row_state from the real status rather than hardcoding "pending" so a row is
    # never re-rendered with pending affordances it no longer has.
    v7 = _v7_row_target(request, proposal_id)
    if v7 is not None:
        return _diff_row_response(request, proposal, v7[0], v7[1], row_state=_ROW_STATE_FOR_STATUS.get(ProposalStatus(proposal.status), "pending"))
    return templates.TemplateResponse(  # nosemgrep: python.fastapi.web.tainted-direct-response-fastapi.tainted-direct-response-fastapi -- Jinja2 TemplateResponse is autoescaped; the validated proposed value renders escaped (no raw/`| safe`), so this is not a direct tainted response.
        request=request,
        name="proposals/partials/proposal_row.html",
        context={"request": request, "proposal": proposal},
    )


@router.patch("/bulk", response_class=HTMLResponse)
async def bulk_action(
    request: Request,
    action: str = Form(...),
    proposal_ids: list[str] = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Bulk approve or reject multiple proposals, for the v7.0 propose workspace's bulk bar.

    phaze-3st0: ``proposal_ids`` is a browser-held id-set that may be arbitrarily stale (request_
    guards.py contract rule 2, ELEMENT case) -- a malformed/empty entry is SKIPPED rather than
    rejecting the whole request, and the returned count is the authority on what actually happened.

    phaze-y4s6: this used to also serve a legacy ``#proposal-list-container`` swap target
    (``bulk_actions.html``, forked on ``HX-Target``), whose view state rode along as
    status/q/page/page_size/sort/order hidden form inputs. That surface had no live caller left
    post-v7-cutover and was deleted outright (along with ``proposal_list.html``/
    ``proposal_table.html``/``pagination.html``/``bulk_actions.html``/``bulk_response.html``), so
    this endpoint now unconditionally serves its one remaining caller,
    ``pipeline/partials/_propose_bulk_bar.html`` -- whose view state rides in the query string
    (``propose_view.query()``), read via ``build_propose_list_context``'s
    ``ListViewState.from_request``, not through these now-removed Form fields.
    """
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")
    status_map = {"approve": ProposalStatus.APPROVED, "reject": ProposalStatus.REJECTED}
    # Parse submitted ids into UUIDs, skipping malformed/empty strings (never a 500); mirrors
    # tracklists.trigger_scan's identical id-list guard.
    uuids: list[uuid.UUID] = []
    for pid in proposal_ids:
        try:
            uuids.append(uuid.UUID(pid))
        except ValueError:
            continue
    # phaze-uu17: only PENDING rows may be bulk approved/rejected; terminal EXECUTED/FAILED
    # rows selected via the "All" tab are skipped, and count reflects only real transitions.
    #
    # phaze-a6hm.11: this single guarded UPDATE is also what makes the endpoint safe to
    # double-submit. `allowed_from` is evaluated INSIDE the UPDATE's WHERE clause, in one statement,
    # so there is no read-then-write window for a concurrent submission to slip through (the
    # phaze-u28m TOCTOU shape) -- and a replay of the same ids after the first submit matches zero
    # rows, because those rows are no longer PENDING. The action is therefore idempotent by
    # construction rather than by locking or by a client-side guard, and `count` on the second
    # submission is honestly 0 rather than a repeat of the first answer.
    count = await bulk_update_status(session, uuids, status_map[action], allowed_from=_APPROVE_REJECT_FROM)

    propose_context = await build_propose_list_context(request, session)
    propose_context |= {
        "request": request,
        "proposal": None,
        # The toast quotes `count` -- the rows that ACTUALLY transitioned -- and names the
        # skipped remainder explicitly when the two differ (phaze-uu17 acceptance). An operator
        # who selects 50 rows of which 12 were still pending is told "12 approved · 38 skipped
        # (already actioned)", never "50 approved". Reporting the selection size would be a
        # confident lie about an irreplaceable archive, which is the failure this bead names.
        "toast_message": _bulk_toast(action, requested=len(uuids), applied=count),
        "is_bulk": True,
    }
    return templates.TemplateResponse(request=request, name="pipeline/partials/_propose_bulk_response.html", context=propose_context)
