"""Discogs matching over stored tracklists."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from phaze.database import get_session

# Kept a runtime import (phaze-oau1o): before the split this name had plain runtime uses elsewhere
# in the module, and only `_enqueue_match_jobs`'s annotation survives here. Left as-is rather than
# demoted to TYPE_CHECKING so the split stays a pure move -- and because every other ORM model in
# this package is runtime-imported for the SQLAlchemy constructs the routes build.
from phaze.models.tracklist import Tracklist  # noqa: TC001
from phaze.routers.pipeline._common import _background_tasks, logger, router, templates
from phaze.services import enqueue_router
from phaze.services.pipeline import get_match_pending_tracklists


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


@router.post("/pipeline/match-tracklists", response_class=HTMLResponse)
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
    count = len(tracklists)

    if count > 0:
        routed = await enqueue_router.resolve_queue_for_task("match_tracklist_to_discogs", request.app.state, session)
        task = asyncio.create_task(_enqueue_match_jobs(routed.queue, tracklists))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_tracklist_response.html",
        context={"request": request, "action": "matching", "count": count},
    )
