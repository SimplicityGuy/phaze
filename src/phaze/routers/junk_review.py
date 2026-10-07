"""Junk review router -- approve, reject, undo and run quarantine for one content group, or a page of them (phaze-l1j35).

The page itself is the ``/s/junk`` shell workspace (``pipeline/partials/junk_workspace.html``,
context from :func:`phaze.routers.shell.stage_context._junk_stage_context`); this router owns its
mutations and its lazy excerpt fragment.

THE BOUNDARY. Every action is a POST carrying the review token the card rendered -- the tags
review's optimistic-concurrency pattern (``routers/tags.py``). The route re-reads the group's live
rows, and a token that no longer matches them is refused with 409 and the refreshed card, so the
operator acts on what is in the database, never on what a stale tab showed. The group comes from
the path and is re-checked against the token; no status is ever taken from the client -- the route
fixes the target, and :func:`phaze.services.companion_junk_review.decide_content_group` moves only
the rows the state machine allows. A malformed token is 422, not the 400 the tags review answers:
an unintelligible envelope is 422 everywhere under ``routers/request_guards.py`` rule 1.

APPROVE, THEN RUN. Operator decision 2026-10-07 (dispatch session, AskUserQuestion), recorded as a
comment on phaze-l1j35 that amends its criterion "Approving a group queues quarantine for each
pending member". Q as put: "...approving a group queues the quarantine moves immediately. So 'undo'
can only reverse a rejection... How should approve and undo work?" A (selected label): "Approve,
then a separate Run step (Recommended)". So approving only marks a group approved, and Undo takes it
back to pending; the separate ``quarantine`` action (per group and bulk) is the ONLY caller of
:func:`phaze.services.junk_quarantine.enqueue_quarantine`, and it dispatches only rows already
approved. Nothing is quarantined without both. Once a row is executing it can no longer be undone.

Decisions record a time only (operator decision 7 of 2026-10-07, "Timestamp only", epic
phaze-4x319): nothing here reads or stores who clicked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Path as PathParam, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import structlog

from phaze.database import get_session
from phaze.enums.junk_review import JunkReviewStatus
from phaze.services import junk_quarantine
from phaze.services.companion_junk_review import decide_content_group
from phaze.services.junk_review_page import (
    GROUP_PAGE_SIZE,
    SHA256_PATTERN,
    JunkGroup,
    JunkReviewTokenInvalid,
    JunkReviewTokenStale,
    approved_review_ids,
    decode_group_token,
    load_groups,
    read_group_excerpt,
    validate_group_tokens,
)
from phaze.web.template_globals import register_page_name_globals, register_set_glyph_globals


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_set_glyph_globals(templates.env)
register_page_name_globals(templates.env)
router = APIRouter(prefix="/junk-review", tags=["junk-review"])

GroupHash = Annotated[str, PathParam(pattern=SHA256_PATTERN, max_length=64)]
# A token is ~170 characters; the cap only refuses a payload no card could have rendered.
_MAX_TOKEN_CHARS = 512

Action = Literal["approve", "reject", "undo", "quarantine"]

_TARGETS: dict[str, JunkReviewStatus] = {
    "approve": JunkReviewStatus.APPROVED,
    "reject": JunkReviewStatus.REJECTED,
    "undo": JunkReviewStatus.PENDING,
}


def _copies(count: int) -> str:
    return f"{count:,} cop{'y' if count == 1 else 'ies'}"


def _decode(token: str, sha256_hash: str | None = None) -> tuple[str, str]:
    if len(token) > _MAX_TOKEN_CHARS:
        raise HTTPException(status_code=422, detail="invalid junk review token")
    try:
        return decode_group_token(token, sha256_hash)
    except JunkReviewTokenInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@dataclass
class _Outcome:
    """What one action did across one or more groups."""

    moved: int = 0
    requested: int = 0
    queued: int = 0
    errors: list[str] = field(default_factory=list)


async def _run_quarantine(request: Request, session: AsyncSession, sha256_hash: str, outcome: _Outcome) -> None:
    """The run step: dispatch the group's APPROVED rows to their agents.

    ``enqueue_quarantine`` commits its own moves (approved -> executing, before dispatch) and marks a
    row it provably could not dispatch as failed, so it raising is the unexpected case: then nothing
    moved and the rows stay approved.
    """
    review_ids = await approved_review_ids(session, sha256_hash)
    if not review_ids:
        return
    outcome.requested += len(review_ids)
    try:
        outcome.queued += await junk_quarantine.enqueue_quarantine(session, review_ids, task_router=request.app.state.task_router)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.warning("junk_review_quarantine_enqueue_failed", sha256_hash=sha256_hash, exc_info=True)
        outcome.errors.append(str(exc) or type(exc).__name__)


async def _apply(request: Request, session: AsyncSession, action: Action, hashes: list[str]) -> _Outcome:
    """Apply ``action`` to every group in ``hashes`` (their tokens already validated in this transaction)."""
    outcome = _Outcome()
    if action == "quarantine":
        for sha256_hash in hashes:
            await _run_quarantine(request, session, sha256_hash, outcome)
        return outcome
    for sha256_hash in hashes:
        outcome.moved += await decide_content_group(session, sha256_hash, _TARGETS[action])
    await session.commit()
    return outcome


def _run_message(outcome: _Outcome) -> str:
    if outcome.requested == 0:
        return "Nothing was quarantined: no copy is approved. Approve the group first, then run the quarantine."
    message = f"Quarantine queued for {_copies(outcome.queued)} of {_copies(outcome.requested)} approved."
    if outcome.errors:
        return message + f" It could not be queued ({'; '.join(outcome.errors)}): those copies stay approved and can be run again or undone."
    if outcome.queued < outcome.requested:
        # enqueue_quarantine returns a row it provably never sent to ``approved`` (so it can be run again)
        # and leaves an ambiguous one ``executing``; either way it is not counted.
        not_sent = _copies(outcome.requested - outcome.queued)
        return message + f" {not_sent} could not be handed to an agent: unsent copies stay approved and can be run again; the card below shows each."
    return message + " The agent moves each file into its scan root's quarantine folder; this can no longer be undone."


def _message(action: Action, outcome: _Outcome, groups: list[JunkGroup]) -> str:
    if action == "quarantine":
        return _run_message(outcome)
    across = f" across {len(groups):,} groups" if len(groups) > 1 else ""
    if action == "approve":
        if outcome.moved == 0:
            return "Nothing was pending, so nothing was approved."
        return f"Approved {_copies(outcome.moved)}{across}. Nothing moves until you run the quarantine; until then this can be undone."
    if action == "reject":
        if outcome.moved == 0:
            return "Nothing could be rejected."
        return f"Rejected {_copies(outcome.moved)}{across}: this content stays where it is, and identical copies are not proposed again."
    if outcome.moved == 0:
        if any(group.executing for group in groups):
            return "Nothing to undo: this group was already handed to quarantine, which cannot be undone."
        return "Nothing to undo in this group."
    return f"Undone: {_copies(outcome.moved)} back to pending review."


@router.get("/{sha256_hash}/excerpt", response_class=HTMLResponse)
async def group_excerpt(request: Request, sha256_hash: GroupHash, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """A bounded, escaped excerpt of one copy of the group, read on its agent. Read-only."""
    excerpt = await read_group_excerpt(session, request.app.state.task_router, sha256_hash)
    return templates.TemplateResponse(request=request, name="junk_review/partials/excerpt.html", context={"request": request, "excerpt": excerpt})


@router.post("/{sha256_hash}/{action}", response_class=HTMLResponse)
async def act_on_group(
    request: Request,
    sha256_hash: GroupHash,
    action: Action,
    review_token: str = Form(..., max_length=_MAX_TOKEN_CHARS),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Approve, reject, undo or run quarantine for one content group, if it is still exactly what the operator reviewed."""
    token = _decode(review_token, sha256_hash)
    context: dict[str, Any] = {"request": request, "focus_card": True}
    try:
        await validate_group_tokens(session, [token])
    except JunkReviewTokenStale:
        context["group"] = (await load_groups(session, [sha256_hash]))[sha256_hash]
        context["message"] = "This group changed after you reviewed it, so nothing was applied. Review it again below."
        return templates.TemplateResponse(request=request, name="junk_review/partials/card_response.html", context=context, status_code=409)

    outcome = await _apply(request, session, action, [sha256_hash])
    group = (await load_groups(session, [sha256_hash]))[sha256_hash]
    context.update(group=group, message=_message(action, outcome, [group]))
    return templates.TemplateResponse(request=request, name="junk_review/partials/card_response.html", context=context)


@router.post("/bulk", response_class=HTMLResponse)
async def bulk_act(
    request: Request,
    action: Literal["approve", "reject", "quarantine"] = Form(...),
    review_tokens: list[str] = Form(default_factory=list, max_length=GROUP_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Approve, reject or run quarantine for every selected group -- all of them, or none if any changed since review."""
    tokens = dict(_decode(review_token) for review_token in review_tokens)
    hashes = sorted(tokens)
    context: dict[str, Any] = {"request": request, "groups": []}
    if not hashes:
        context["message"] = "Select at least one group first."
        return templates.TemplateResponse(request=request, name="junk_review/partials/bulk_response.html", context=context)
    try:
        await validate_group_tokens(session, list(tokens.items()))
    except JunkReviewTokenStale:
        groups = await load_groups(session, hashes)
        context.update(
            groups=[groups[sha] for sha in hashes],
            message="At least one selected group changed after you reviewed it, so nothing was applied. The selected groups are refreshed below.",
        )
        return templates.TemplateResponse(request=request, name="junk_review/partials/bulk_response.html", context=context, status_code=409)

    outcome = await _apply(request, session, action, hashes)
    groups = await load_groups(session, hashes)
    ordered = [groups[sha] for sha in hashes]
    context.update(groups=ordered, message=_message(action, outcome, ordered))
    return templates.TemplateResponse(request=request, name="junk_review/partials/bulk_response.html", context=context)
