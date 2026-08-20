"""Proposal review UI router -- serves the approval workflow pages."""

from datetime import datetime
from pathlib import Path
from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.analysis import AnalysisWindow
from phaze.models.proposal import APPROVE_REJECT_FROM, UNDO_FROM, ProposalStatus, RenameProposal

# Changes Review's container id + list context, so a bulk action re-renders that surface through
# the SAME builder its GET uses. The edge is one-way -- routers/shell.py does not import this
# module -- so there is no cycle.
from phaze.routers.shell import build_changes_review_context
from phaze.services.analysis_timeline import build_analysis_timeline_context
from phaze.services.collision import get_review_collision_ids
from phaze.services.proposal_queries import (
    ProposalEditRefusedError,
    ProposalPendingConflictError,
    ProposalReviewToken,
    ProposalStaleWriteError,
    ProposalTransitionError,
    bulk_approve_selected_above_confidence,
    bulk_update_status,
    get_proposal_with_file,
    update_proposal_fields,
    update_proposal_status,
)


# The review-UI state machine (phaze-uu17) now lives on the model, beside the enum it constrains --
# phaze-a6hm.11 hoisted it there because the propose workspace render needs the SAME fact to decide
# which rows may be offered a checkbox. These aliases keep this module's existing spelling.
_APPROVE_REJECT_FROM = APPROVE_REJECT_FROM
_UNDO_FROM = UNDO_FROM

# phaze-3yop: the two literal bulk actions bulk_action() whitelists (line ~580), each spelled with
# its own real past tense. `f"{action}d"` happened to be correct for "approve" and wrong for
# "reject" ("rejectd") -- a suffix rule applied to a two-member set where it is only valid for one
# member. Keyed off the SAME literal pair `status_map` there already spells out, so a third action
# can never silently fall through a shared suffix rule again.
_PAST_TENSE = {"approve": "approved", "reject": "rejected"}
ProposalReviewTokenWire = Annotated[str, StringConstraints(max_length=256)]


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
    verb = _PAST_TENSE[action]
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
    expected_updated_at: datetime | None = None,
) -> RenameProposal | None:
    """Call update_proposal_status, translating state-machine errors into 409 responses."""
    try:
        return await update_proposal_status(session, proposal_id, new_status, allowed_from=allowed_from, expected_updated_at=expected_updated_at)
    except ProposalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProposalPendingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProposalStaleWriteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _parse_updated_at_token(token: str | None) -> datetime | None:
    """Parse the APPROVE button's optimistic-concurrency token (phaze-exivg).

    Empty/missing means "no token supplied" (``None``) -- every caller that predates this token
    (API clients, tests, a bare hand-built PATCH) keeps the pre-phaze-exivg status-only guard with
    no behavior change. A token that fails to parse is treated the SAME way rather than 400ing: an
    operator's browser always sends a well-formed ``isoformat()`` value (it round-trips the row's
    own ``updated_at``, minted server-side by :func:`_diff_row_context`), so a malformed one only
    originates from something outside this guard's job to police -- and refusing to approve
    outright over a malformed token would be a worse failure mode than folding the check out.
    """
    if not token:
        return None
    try:
        return datetime.fromisoformat(token)
    except ValueError:
        return None


# phaze-3a2j: the v7 diff-row surfaces render rows from the shared pipeline/partials/_diff_row.html
# partial and hx-target each row's own <div>. The mutation routes historically returned the LEGACY
# <tr>-based proposal_row.html, so a swap dropped broken table-row markup into the div list and the
# Alpine bindings threw ReferenceErrors. When a request originates from one of these surfaces
# (identified by its HX-Target = "{prefix}-{proposal_id}"), the route must instead return
# _diff_row.html with the matching prefix, facet, and lifecycle state.
#
# phaze-tzy6s.11 / ADR-0008: the surfaces collapsed into ONE. Changes Review is now the only UI
# workspace that authorizes filename, destination, and tag changes, so the rename / move / tagwrite
# workspaces and the record drawer's inline cluster were all deleted. Only "rename-row" survived
# that consolidation with a renderer -- pipeline/partials/_changes_list.html:69, the Filename +
# Destination section of Changes Review, which kept the id stem rather than churn every hx-target.
# (Tag rows use "tagwrite-row" and are routed by tags.py, not this map.)
#
# phaze-7tiqp: the map used to carry three more stems -- "changes-row", "record-row" and
# "move-row" -- kept past the consolidation that orphaned them. None had a renderer in any
# template, so no browser could ever send an HX-Target matching them; they resolved only for a
# hand-built request, and the "move-row" one resolved to the PATH facet, i.e. a response shape the
# product had no way to ask for. They are retired here along with the rest of the
# bulk-approve-high-confidence chain. A caller naming an unknown row still gets the default
# (rename / filename) shape below, so the removal narrows what is reachable by hand without
# changing what any live surface receives.
_V7_ROW_FACETS: dict[str, str] = {
    "rename-row": "filename",
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


def _row_target(request: Request, proposal_id: uuid.UUID) -> tuple[str, str]:
    """Return the (row_id_prefix, facet) pair to render this mutation's response row with.

    phaze-vvmh: this used to return ``None`` for a request that named no v7 row, and every mutation
    route had a second, LEGACY branch for that case rendering ``approve_response.html`` /
    ``undo_response.html`` / ``proposal_row.html``. Those responses were unreachable in the product
    and actively wrong if they ever HAD been reached: each carried an OOB fragment addressed to
    ``#stats-bar``, an id whose only host (the legacy proposals list page) the Phase-62 cutover
    deleted, so htmx would have discarded it with ``htmx:oobErrorNoTarget``; and the ``<tr>``-shaped
    ``proposal_row.html`` would have dropped table-row markup into a ``<div>`` list. Every live
    approve / reject / undo / edit control in the app is a ``pipeline/partials/_diff_row.html`` whose
    ``hx-target`` is exactly one of the three prefixes below, so the fallback existed only for
    hand-made requests -- and it was the last thing keeping the Execute Approved button's dead host
    chain reachable by the template guard.

    A caller that names no known row now gets the SAME shared ``_diff_row.html`` under the default
    (rename / filename) shape rather than a differently-shaped legacy response: one response shape
    for one route, which is the property the two deleted branches lacked.
    """
    hx_target = request.headers.get("HX-Target", "")
    for prefix, facet in _V7_ROW_FACETS.items():
        if hx_target == f"{prefix}-{proposal_id}":
            return prefix, facet
    return "rename-row", "filename"


def _diff_row_context(proposal: RenameProposal, row_id_prefix: str, facet: str, row_state: str) -> dict[str, object]:
    """Build the render context _diff_row.html expects for one proposal (phaze-3a2j).

    phaze-7tiqp: the builder used to take an ``oob`` keyword for a second consumer, the
    bulk-approve-high-confidence response (phaze-71hi), which needed a LIST of these rendered with
    ``oob=True`` because the rename/move workspaces ran no row poll (R-2) to pick a bulk transition
    up on their own. Those workspaces, that route and its template are all retired, so the keyword
    had one possible value left and ``_diff_row.html`` dropped the ``hx-swap-oob`` it fed. The only
    consumer now is :func:`_diff_row_response`, which swaps the row it was targeted at.
    """
    file_record = proposal.file
    if facet == "path":
        before = file_record.current_path
        after = proposal.proposed_path or ""
        edit_facet = "path"
        extra_context: dict[str, object] = {
            "diff_label": "Destination",
            "secondary_label": "Filename",
            "secondary_before": file_record.original_filename,
            "secondary_after": proposal.proposed_filename,
        }
    else:
        before = file_record.original_filename
        after = proposal.proposed_filename
        edit_facet = "filename"
        destination = (
            str(Path(proposal.proposed_path) / proposal.proposed_filename)
            if proposal.proposed_path
            else str(Path(file_record.current_path).parent / proposal.proposed_filename)
        )
        extra_context = {
            "diff_label": "Filename",
            "secondary_label": "Destination",
            "secondary_before": file_record.current_path,
            "secondary_after": destination,
        }
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
        "confidence": proposal.confidence,
        "warnings": ([proposal.reason] if proposal.reason else []),
        "consequences": (
            "Rename in place on the owning file server."
            if proposal.proposed_path is None
            else "Rename and move on the owning file server; execution remains separately gated."
        ),
        "eligibility_reason": (
            "Eligible for selected bulk approval."
            if ProposalStatus(proposal.status) == ProposalStatus.PENDING and proposal.confidence is not None and proposal.confidence >= 0.9
            else "Individual approval only, or this decision is already resolved."
        ),
        # phaze-exivg: the optimistic-concurrency token the APPROVE button round-trips back via
        # hx-vals. Always the row's LIVE updated_at at render time -- after an undo/edit/re-propose
        # the next render of this same partial carries the row's NEW value, so a stale button never
        # lingers on screen past its own re-render.
        "updated_at": proposal.updated_at,
        **extra_context,
    }


def _diff_row_response(request: Request, proposal: RenameProposal, row_id_prefix: str, facet: str, row_state: str) -> HTMLResponse:
    """Render the shared _diff_row.html for a v7 workspace row swap (phaze-3a2j)."""
    context = _diff_row_context(proposal, row_id_prefix, facet, row_state)
    context["request"] = request
    return templates.TemplateResponse(request=request, name="pipeline/partials/_diff_row.html", context=context)


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/proposals", tags=["proposals"])

SPARK_W = 80.0
SPARK_H = 24.0


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
    expected_updated_at: str | None = Form(default=None),
) -> HTMLResponse:
    """Approve a proposal and return the updated row (phaze-vvmh: one response shape, no OOB stats).

    phaze-exivg: ``expected_updated_at`` is the optimistic-concurrency token the row's own render
    carried (``_diff_row_context``'s ``updated_at``, round-tripped by the APPROVE button's
    hx-vals). Folded into the conditional UPDATE's WHERE clause
    (:func:`~phaze.services.proposal_queries.update_proposal_status`), so a row a concurrent
    ``store_proposals`` upsert rewrote after the page render -- same id, same PENDING status,
    different ``proposed_filename`` / ``proposed_path`` / ``confidence`` -- fails the guard and
    409s instead of being approved sight-unseen. Absent/blank (no token from the browser, e.g. a
    bare API PATCH) skips the check -- the pre-existing status-only guard applies on its own.
    """
    parsed_updated_at = _parse_updated_at_token(expected_updated_at)
    if parsed_updated_at is None:
        raise HTTPException(status_code=400, detail="A reviewed proposal version is required")
    if str(proposal_id) in await get_review_collision_ids(session):
        raise HTTPException(status_code=409, detail="Destination collides with another pending or approved proposal")
    proposal = await _guarded_status_update(
        session,
        proposal_id,
        ProposalStatus.APPROVED,
        _APPROVE_REJECT_FROM,
        expected_updated_at=parsed_updated_at,
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    row_id_prefix, facet = _row_target(request, proposal_id)
    return _diff_row_response(request, proposal, row_id_prefix, facet, row_state="approved")


@router.patch("/{proposal_id}/reject", response_class=HTMLResponse)
async def reject_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Reject a proposal and return the updated row (phaze-vvmh: one response shape, no OOB stats)."""
    proposal = await _guarded_status_update(session, proposal_id, ProposalStatus.REJECTED, _APPROVE_REJECT_FROM)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    row_id_prefix, facet = _row_target(request, proposal_id)
    return _diff_row_response(request, proposal, row_id_prefix, facet, row_state="skipped")


@router.patch("/{proposal_id}/undo", response_class=HTMLResponse)
async def undo_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Revert a proposal to pending status and return the updated row (phaze-vvmh: no OOB stats)."""
    proposal = await _guarded_status_update(session, proposal_id, ProposalStatus.PENDING, _UNDO_FROM)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    row_id_prefix, facet = _row_target(request, proposal_id)
    return _diff_row_response(request, proposal, row_id_prefix, facet, row_state="pending")


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

    # phaze-w55w1: the Phase 44 AnalysisResult fetch that fed the "Sampled" badge and the
    # "Deepen analysis" button is gone with them (ADR-0007 §7) -- the timeline renders from
    # AnalysisWindow rows alone, which is the full coverage now that nothing is sampled.

    return templates.TemplateResponse(
        request=request,
        name="proposals/partials/analysis_timeline.html",
        context={
            "request": request,
            "proposal": proposal,
            "file_id": file_id,
            **build_analysis_timeline_context(windows),
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
    # inside the UPDATE and raises ProposalEditRefusedError (phaze-3mru), which we translate to 409.
    try:
        if is_path:
            proposal = await update_proposal_fields(session, proposal_id, proposed_path=value, allowed_from=_APPROVE_REJECT_FROM)
        else:
            proposal = await update_proposal_fields(session, proposal_id, proposed_filename=value, allowed_from=_APPROVE_REJECT_FROM)
    except ProposalEditRefusedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    # phaze-3a2j: a v7 diff-row workspace expects the shared _diff_row.html back (the row stays
    # PENDING with the edited "after" value), not the legacy <tr> proposal_row.html.
    # phaze-3tj4: derive row_state from the real status rather than hardcoding "pending" so a row is
    # never re-rendered with pending affordances it no longer has.
    row_id_prefix, facet = _row_target(request, proposal_id)
    return _diff_row_response(
        request, proposal, row_id_prefix, facet, row_state=_ROW_STATE_FOR_STATUS.get(ProposalStatus(proposal.status), "pending")
    )


@router.patch("/bulk", response_class=HTMLResponse)
async def bulk_action(
    request: Request,
    action: str = Form(...),
    proposal_ids: list[str] = Form(default=[]),
    review_tokens: list[ProposalReviewTokenWire] = Form(default=[], max_length=100),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Bulk approve or reject the selection Changes Review submitted.

    phaze-3st0: ``proposal_ids`` is a browser-held id-set that may be arbitrarily stale (request_
    guards.py contract rule 2, ELEMENT case) -- a malformed/empty entry is SKIPPED rather than
    rejecting the whole request, and the returned count is the authority on what actually happened.

    ONE ROUTE, ONE RESPONSE SHAPE. This endpoint has twice carried a second, ``HX-Target``-forked
    branch that outlived the surface it answered, and both are now gone:

    * phaze-y4s6 removed the legacy ``#proposal-list-container`` fork (``bulk_actions.html`` and
      friends), which had no live caller after the v7 cutover.
    * phaze-7tiqp removed the fallthrough that rendered
      ``pipeline/partials/_propose_bulk_response.html``. Its caller was the Propose bulk bar,
      deleted by phaze-tzy6s.7 (bf45fe06); ADR-0008 then made Changes Review the only surface that
      authorizes anything, so Propose is preparation-only and has no decision controls to reach
      this route with. The one live caller is ``pipeline/partials/_changes_list.html:46``, whose
      ``hx-target`` is ``#{{ changes_list_id }}`` -- always ``CHANGES_LIST_CONTAINER_ID`` -- so the
      fallthrough was reachable only by a hand-built request and answered it with a re-render of a
      container the caller had not asked about.

    A caller that names no ``HX-Target`` at all therefore gets the Changes Review list body, which
    is the surface that owns this action. That is the same property ``_row_target`` settled on for
    the single-row mutations (phaze-vvmh): one shape per route, rather than a second shape kept
    alive by tests.
    """
    if action not in ("approve_eligible", "reject"):
        raise HTTPException(status_code=400, detail="Action must be 'approve_eligible' or 'reject'")
    # Parse submitted ids into UUIDs, skipping malformed/empty strings (never a 500); mirrors
    # tracklists.trigger_scan's identical id-list guard.
    reviewed: list[ProposalReviewToken] = []
    for token in review_tokens:
        try:
            raw_id, raw_updated_at, content_digest = token.split("|", 2)
            reviewed.append(
                ProposalReviewToken(
                    proposal_id=uuid.UUID(raw_id),
                    updated_at=datetime.fromisoformat(raw_updated_at),
                    content_digest=content_digest,
                )
            )
        except (TypeError, ValueError):
            continue
    uuids = [token.proposal_id for token in reviewed]
    for pid in proposal_ids:
        try:
            parsed = uuid.UUID(pid)
        except ValueError:
            continue
        if parsed not in uuids:
            uuids.append(parsed)
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
    if action == "approve_eligible":
        count = await bulk_approve_selected_above_confidence(session, reviewed)
        toast_action = "approve"
    else:
        count = await bulk_update_status(session, uuids, ProposalStatus.REJECTED, allowed_from=_APPROVE_REJECT_FROM)
        toast_action = action

    changes_context = await build_changes_review_context(request, session)
    changes_context |= {
        "request": request,
        # The toast quotes `count` -- the rows that ACTUALLY transitioned -- and names the
        # skipped remainder explicitly when the two differ (phaze-uu17 acceptance). An operator
        # who selects 50 rows of which 12 were still pending is told "12 approved · 38 skipped
        # (already actioned)", never "50 approved". Reporting the selection size would be a
        # confident lie about an irreplaceable archive, which is the failure that bead names.
        "changes_toast": _bulk_toast(toast_action, requested=len(uuids), applied=count),
    }
    return templates.TemplateResponse(request=request, name="pipeline/partials/_changes_list.html", context=changes_context)
