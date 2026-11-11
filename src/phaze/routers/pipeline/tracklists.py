"""Discogs matching over stored tracklists, stored local sources, and bulk CUE link review."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse

from phaze.database import get_session

# Kept a runtime import (phaze-oau1o): before the split this name had plain runtime uses elsewhere
# in the module, and only `_enqueue_match_jobs`'s annotation survives here. Left as-is rather than
# demoted to TYPE_CHECKING so the split stays a pure move -- and because every other ORM model in
# this package is runtime-imported for the SQLAlchemy constructs the routes build.
from phaze.models.tracklist import Tracklist  # noqa: TC001
from phaze.routers.pipeline._common import _background_tasks, logger, router, templates
from phaze.routers.request_guards import require_same_origin_form
from phaze.services import enqueue_router
from phaze.services.cue_link_review import (
    AMBIGUITY_REASONS,
    MAX_DECISIONS,
    METHOD_LABELS,
    METHOD_ORDER,
    REASON_LABELS,
    DecisionOutcome,
    decide_cue_links,
    load_cue_link_review,
)
from phaze.services.pagination import DEFAULT_PAGE_SIZE
from phaze.services.pipeline import get_match_pending_tracklists
from phaze.services.pipeline.tracklists import get_match_pending_recording_sources
from phaze.services.source_workspace import get_existing_cue_page, get_source_workspace_page


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _enqueue_match_jobs(queue: Any, tracklists: list[Tracklist]) -> None:
    """Background coroutine to enqueue ``match_tracklist_to_discogs`` jobs (one per pending tracklist).

    ``match_tracklist_to_discogs`` is a CONTROLLER task taking only ``tracklist_id`` (mirrors the
    single-tracklist ``tracklists.match_discogs`` trigger); the deterministic key
    ``match_tracklist_to_discogs:<tracklist_id>`` is applied centrally by the ``before_enqueue`` hook
    (Phase 35), so a double-click / refresh dedups in flight (D, T-41-02). Set NO explicit ``key=``.
    Background-enqueued to avoid HTTP timeout on a large pending set (Pitfall 2).

    phaze-ysz16: each tracklist's enqueue is individually contained, mirroring phaze-4ter's
    ``_enqueue_analysis_jobs`` containment. Pre-fix a bare loop with no per-item try/except meant
    the FIRST transient broker/pool error aborted every remaining tracklist, surfacing only as
    asyncio's uncorrelated GC-time "Task exception was never retrieved" log (this is detached via
    ``asyncio.create_task`` + a bare ``_background_tasks.discard`` done-callback that never calls
    ``task.result()``) while the response had already reported the full count. Nothing here
    mutates durable state before the enqueue, so a dropped tracklist stays in the derived pending
    set for an idempotent re-click; this fix makes the drop visible in a correlated log instead of
    losing every remaining tracklist to one failure.
    """
    dropped = 0
    for tl in tracklists:
        try:
            await queue.enqueue("match_tracklist_to_discogs", tracklist_id=str(tl.id))
        except Exception:
            dropped += 1
            logger.exception("_enqueue_match_jobs: failed to enqueue match_tracklist_to_discogs job", tracklist_id=str(tl.id))
    if dropped:
        logger.warning(
            "_enqueue_match_jobs: tracklists dropped from this run -- pending set unaffected, re-click will retry",
            dropped=dropped,
            total=len(tracklists),
        )


async def _enqueue_recording_match_jobs(queue: Any, sources: list[dict[str, str]]) -> None:
    for source in sources:
        try:
            await queue.enqueue("match_recording_source_to_discogs", **source)
        except Exception:
            logger.exception("recording_discogs_enqueue_failed", media_id=source["media_id"])


@router.post(
    "/pipeline/match-tracklists", response_class=HTMLResponse, description="HTMX endpoint: bulk-trigger Discogs matching over the pending set."
)
async def trigger_match_tracklists_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: bulk-trigger Discogs matching over the pending set (Phase 41).

    Pending = tracklists NOT yet reachable from ``discogs_links`` (the exact complement of
    :func:`get_stage_progress`'s ``match.done``); already-linked tracklists are skipped so re-runs are
    cheap and idempotent. ``match_tracklist_to_discogs`` is a CONTROLLER task, routed via
    :func:`enqueue_router.resolve_queue_for_task` to the controller queue (Phase-30 rule) -- never the
    consumer-less default queue. Controller tasks never raise ``NoActiveAgentError`` (mirrors
    ``match_discogs``), so no no-active-agent branch is needed. Manual only -- NO auto-trigger
    (automatic enqueue is reserved for the Phase-42 recovery pass).
    """
    tracklists = await get_match_pending_tracklists(session)
    recording_sources = await get_match_pending_recording_sources(session)
    count = len(tracklists) + len(recording_sources)

    if count > 0:
        routed = await enqueue_router.resolve_queue_for_task("match_tracklist_to_discogs", request.app.state, session)
        task = asyncio.create_task(_enqueue_match_jobs(routed.queue, tracklists))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        source_task = asyncio.create_task(_enqueue_recording_match_jobs(routed.queue, recording_sources))
        _background_tasks.add(source_task)
        source_task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_tracklist_response.html",
        context={"request": request, "action": "matching", "count": count},
    )


@router.get("/pipeline/local-source-sets", response_class=HTMLResponse)
async def local_source_sets_fragment(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Stored source provenance and current CUE inventory; no acquisition or implicit selection."""
    raw_kind = request.query_params.get("kind", "tracklist")
    kind = cast('Literal["tracklist", "release_metadata", "cue"]', raw_kind if raw_kind in {"tracklist", "release_metadata", "cue"} else "tracklist")
    try:
        page = int(request.query_params.get("page", "1"))
    except ValueError:
        page = 1
    try:
        size = int(request.query_params.get("page_size", str(DEFAULT_PAGE_SIZE)))
    except ValueError:
        size = DEFAULT_PAGE_SIZE
    inventory_view = kind == "cue" and request.query_params.get("inventory") == "1"
    source_page = (
        await get_existing_cue_page(session, page=page, page_size=size)
        if inventory_view
        else await get_source_workspace_page(session, kind, page=page, page_size=size)
    )
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_local_source_sets.html",
        context={"request": request, "source_page": source_page, "source_kind": kind, "inventory_view": inventory_view},
    )


CUE_LINK_PAGE_SIZE = 50
"""Rows per page in each half of the CUE link review; a decision covers at most one page's checked rows."""


def _int_param(request: Request, name: str) -> int:
    try:
        return max(1, int(request.query_params.get(name, "1")))
    except ValueError:
        return 1


async def _cue_link_context(request: Request, session: AsyncSession, *, page: int, excluded_page: int) -> dict[str, Any]:
    review = await load_cue_link_review(session)
    start, excluded_start = (page - 1) * CUE_LINK_PAGE_SIZE, (excluded_page - 1) * CUE_LINK_PAGE_SIZE
    proposed = review.proposed[start : start + CUE_LINK_PAGE_SIZE]
    groups = [(method, [row for row in proposed if row.method == method]) for method in METHOD_ORDER]
    return {
        "request": request,
        "proposed_total": len(review.proposed),
        "proposed_counts": review.proposed_counts(),
        "excluded_total": len(review.excluded),
        "excluded_counts": review.excluded_counts(),
        "groups": [(method, rows) for method, rows in groups if rows],
        "excluded": review.excluded[excluded_start : excluded_start + CUE_LINK_PAGE_SIZE],
        "page": page,
        "has_next": start + CUE_LINK_PAGE_SIZE < len(review.proposed),
        "excluded_page": excluded_page,
        "excluded_has_next": excluded_start + CUE_LINK_PAGE_SIZE < len(review.excluded),
        "method_labels": METHOD_LABELS,
        "reason_labels": REASON_LABELS,
        "ambiguity_reasons": AMBIGUITY_REASONS,
    }


@router.get("/pipeline/cue-link-review", response_class=HTMLResponse)
async def cue_link_review_fragment(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Unambiguous pending CUE link candidates grouped by link method, and every exclusion with its reason.

    Read-only: nothing is selected until the operator checks rows and posts a decision.
    """
    # phaze-2dxhd. Kept out of the docstring: route docstrings become operator-facing OpenAPI text.
    context = await _cue_link_context(request, session, page=_int_param(request, "page"), excluded_page=_int_param(request, "excluded_page"))
    return templates.TemplateResponse(request=request, name="pipeline/partials/_cue_link_review.html", context=context)


@router.post("/pipeline/cue-link-review/decide", response_class=HTMLResponse, dependencies=[Depends(require_same_origin_form)])
async def decide_cue_link_review(
    request: Request,
    action: Literal["select", "reject"] = Form(...),
    review_tokens: list[str] = Form(default_factory=list, max_length=MAX_DECISIONS),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Approve or reject every checked candidate through the audited single-review path -- all, or none.

    A row that changed after it was shown (no longer proposed, or different revision, hashes or FILE
    ordinal) refuses the whole batch with 409 and redraws the review; nothing is applied blind.
    """
    try:
        outcome = await decide_cue_links(session, review_tokens, action, actor="admin")
    except ValueError as exc:
        outcome, status_code = DecisionOutcome(action=action, error=str(exc)), 422
    else:
        status_code = 409 if outcome.stale or (outcome.error and review_tokens) else 200
    if outcome.applied:
        await session.commit()
    context = await _cue_link_context(request, session, page=1, excluded_page=1)
    context.update(outcome=outcome, failed=not outcome.applied)
    return templates.TemplateResponse(request=request, name="pipeline/partials/_cue_link_review.html", context=context, status_code=status_code)
