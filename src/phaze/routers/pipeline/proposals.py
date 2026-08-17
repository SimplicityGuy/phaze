"""Proposal generation triggers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from phaze.config import settings
from phaze.database import get_session
from phaze.routers.pipeline._common import _background_tasks, logger, router, templates
from phaze.services import enqueue_router
from phaze.services.pipeline import get_proposal_busy_count, get_proposal_pending_batches


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _enqueue_proposal_jobs(queue: Any, batches: list[list[str]]) -> None:
    """Background coroutine to enqueue generate_proposals jobs for batched file IDs.

    phaze-ysz16: each batch's enqueue is individually contained, mirroring phaze-4ter's
    ``_enqueue_analysis_jobs`` containment. Pre-fix this was a bare loop with no per-item
    try/except -- every caller spawns it via ``asyncio.create_task`` with only a bare
    ``_background_tasks.discard`` done-callback (``task.result()`` never called), so the FIRST
    exception (a transient broker/pool error) aborted every remaining batch AND surfaced only as
    asyncio's uncorrelated GC-time "Task exception was never retrieved" log -- never tied to this
    request -- while the HTTP response had already reported the FULL batch count as enqueued.
    Nothing here mutates durable state before the enqueue, so a dropped batch just stays in the
    derived pending set and an idempotent re-click re-enqueues it; this fix is purely about making
    the drop visible in a correlated log instead of losing every remaining batch to one failure.
    """
    dropped = 0
    for idx, batch in enumerate(batches):
        try:
            await queue.enqueue("generate_proposals", file_ids=batch, batch_index=idx)
        except Exception:
            dropped += 1
            logger.exception("_enqueue_proposal_jobs: failed to enqueue generate_proposals batch", batch_index=idx, batch_size=len(batch))
    if dropped:
        logger.warning(
            "_enqueue_proposal_jobs: batches dropped from this run -- pending set unaffected, re-click will retry",
            dropped=dropped,
            total=len(batches),
        )


@router.post("/api/v1/proposals/generate")
async def trigger_proposals(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue generate_proposals jobs for files with both metadata and analysis (per D-02 convergence gate).

    Uses settings.llm_batch_size (default 10) for batch chunking.

    phaze-8qheu: gated on :func:`get_proposal_busy_count` -- a second trigger while a prior
    batch set is still queued/active is REFUSED (zero enqueued) rather than recomputing the
    pending set. The set-hash dedup key (``generate_proposals:<sha256(sorted file_ids)>``) is
    NOT robust to the pending set moving between the two triggers (see
    :func:`get_proposal_pending_batches`'s docstring), so this server-side gate -- not the SAQ
    key dedup -- is what prevents a mid-drain re-trigger from double-proposing the backlog.
    """
    busy = await get_proposal_busy_count(session)
    if busy > 0:
        return {
            "enqueued_batches": 0,
            "total_files": 0,
            "message": f"Proposal generation already in progress ({busy} batch job(s) queued/active); try again once it finishes.",
        }

    # Per D-02: convergence gate via the shared, deterministically-sorted pending-set helper
    # (D-03 anti-drift) -- see the busy gate above for why this alone is not enough to make a
    # re-trigger dedup-safe.
    batches = await get_proposal_pending_batches(session, settings.llm_batch_size)
    total_files = sum(len(b) for b in batches)
    if not batches:
        return {"enqueued_batches": 0, "total_files": 0, "message": "No files ready for proposals (need both metadata and analysis)"}

    routed = await enqueue_router.resolve_queue_for_task("generate_proposals", request.app.state, session)
    task = asyncio.create_task(_enqueue_proposal_jobs(routed.queue, batches))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {
        "enqueued_batches": len(batches),
        "total_files": total_files,
        "message": f"Enqueued {len(batches)} batches ({total_files} files) for proposal generation",
    }


@router.post("/pipeline/proposals", response_class=HTMLResponse)
async def trigger_proposals_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger proposal generation and return response fragment.

    phaze-8qheu: gated on :func:`get_proposal_busy_count` -- see ``trigger_proposals`` (the API
    twin) for why the set-hash dedup key alone cannot make a re-trigger safe.
    """
    busy = await get_proposal_busy_count(session)
    if busy > 0:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/trigger_response.html",
            context={
                "request": request,
                "action": "proposal generation",
                "count": 0,
                "no_active_agent": False,
                "already_running": True,
                "busy": busy,
            },
        )

    # Per D-02: convergence gate via the shared, deterministically-sorted pending-set helper
    # (D-03 anti-drift) -- see the busy gate above for why this alone is not enough to make a
    # re-trigger dedup-safe.
    batches = await get_proposal_pending_batches(session, settings.llm_batch_size)
    count = sum(len(b) for b in batches)
    batches_count = 0

    if count > 0:
        batches_count = len(batches)
        routed = await enqueue_router.resolve_queue_for_task("generate_proposals", request.app.state, session)
        task = asyncio.create_task(_enqueue_proposal_jobs(routed.queue, batches))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "proposal generation", "count": count, "batches": batches_count, "no_active_agent": False},
    )
