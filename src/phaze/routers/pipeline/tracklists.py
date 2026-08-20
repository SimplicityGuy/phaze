"""Tracklist matching, per-file lookup priority and the drain controls."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

# The suppression below is deliberate (runtime import, NOT type-only): this module carries
# `from __future__ import annotations`, so ruff offers to move `uuid` into the TYPE_CHECKING block.
# FastAPI resolves route annotations at RUNTIME via get_type_hints, so a `file_id: uuid.UUID` path
# param would raise NameError on import. (Before phaze-0jpe this import also had a plain runtime
# use -- `uuid.uuid4()` for the scan_live_set nonce -- which masked the rule; the annotation
# requirement is the real reason it must stay here.)
import uuid  # noqa: TC003

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from phaze.config import settings
from phaze.database import get_session
from phaze.models.file import FileRecord

# Kept a runtime import (phaze-oau1o): before the split this name had plain runtime uses elsewhere
# in the module, and only `_enqueue_match_jobs`'s annotation survives here. Left as-is rather than
# demoted to TYPE_CHECKING so the split stays a pure move -- and because every other ORM model in
# this package is runtime-imported for the SQLAlchemy constructs the routes build.
from phaze.models.tracklist import Tracklist  # noqa: TC001
from phaze.routers.pipeline._common import _background_tasks, logger, router, templates
from phaze.routers.response_shape import RENDERABLE_ALERT_STATUS
from phaze.services import enqueue_router
from phaze.services.pipeline import get_match_pending_tracklists
from phaze.services.tracklist_candidate_queue import DAILY_LOOKUP_CEILING
from phaze.services.tracklist_drain_arm import arm_drain, disarm_drain, get_arm_state
from phaze.services.tracklist_priority import flag_file_for_lookup, get_file_tracklist_review, unflag_file
from phaze.tasks.tracklist import refresh_tracklists
from phaze.tasks.tracklist_drain import tracklist_drain_status


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


# --- Bulk match tracklist endpoint (Phase 41, REQ-41-2) ---
#
# phaze-2akf: the SEARCH ALL (POST /pipeline/search-tracklists) and SCRAPE ALL
# (POST /pipeline/scrape-tracklists) endpoints that used to sit here are GONE, with the two SAQ
# tasks behind them. They fanned one job out per file / per tracklist against a host that publishes
# a whole-system budget of ~1 request / 8 s, with no cache, no queue and no resumption -- and the
# detail-page selectors they ultimately called matched zero nodes, so every job they enqueued
# produced an empty tracklist. The replacement is not another bulk button: it is
# POST /pipeline/run-tracklist-drain, which enqueues ONE bounded slice of the resumable drain and
# reports its queue depth and honest ETA through GET /pipeline/tracklist-drain-status.


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


# --- Per-file 1001Tracklists priority + review (phaze-fq9h.8) ---


@router.post("/pipeline/tracklists/{file_id}/prioritize", response_class=HTMLResponse)
async def prioritize_tracklist_lookup_ui(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: persist an operator priority flag for ``file_id`` and queue one bounded
    drain slice to answer it (phaze-fq9h.8, the "trigger/prioritize a lookup for a file"
    acceptance criterion).

    Two things happen, and both matter:

    1. :func:`~phaze.services.tracklist_priority.flag_file_for_lookup` PERSISTS the flag, so it
       survives past this one job -- the gap phaze-fq9h.7 left open (see
       ``models.tracklist_priority_flag``'s module docstring). ``build_drain_queue`` reads it on
       every future call, whether that call comes from this endpoint, a scheduled slice, or a
       worker restart.
    2. A single ``drain_tracklists`` job is enqueued with ``limit=1`` and this exact file as its
       target, so the operator sees this set start now rather than only ever affecting some later,
       unscheduled run.
       ``drain_tracklists`` is deliberately UNKEYED (phaze-fq9h.7's ``_UNKEYED_TASKS`` entry), so
       this can never silently dedup onto an unrelated in-flight slice.

    Only flags files that could actually reach the queue: a file that already has a tracklist, or
    one the classifier reads as ``TRACK``/``UNKNOWN``, never becomes a
    :class:`~phaze.services.tracklist_drain.DrainCandidate` at all (see
    ``FileTracklistReview.eligible``), so flagging it would silently do nothing while a
    ``limit=1`` slice spent its one request on whatever UNRELATED set actually sits at the front
    of the queue -- a wasted, misattributed lookup. The review renders the honest reason instead.

    phaze-z8xq7: ``eligible`` alone is NOT the gate -- it says nothing about the drain's OWN cache
    verdict for this set. A cache-suppressed set (a definitive negative still inside its 180-day
    TTL, a transient failure inside its backoff window, or one parked after
    ``TRANSIENT_MAX_ATTEMPTS``) is still ``eligible`` by classification, but
    :func:`~phaze.services.tracklist_candidate_queue.build_queue_from_signals` keeps a forced file
    out of the queue when its cache row was not cleared -- so flagging it would upsert an inert
    flag, claim "a lookup has been dispatched" that will never happen, and still spend the enqueued
    ``limit=1`` slice's one request on whatever UNRELATED set sits at the front of the real queue.
    ``FileTracklistReview.actionable`` is ``eligible`` AND the cache verdict says it would actually
    be queried now -- that is the real gate here.

    Renders the review fragment IMMEDIATELY, before the enqueued job runs -- it can only ever say
    "queued", never "found", because the lookup has not happened yet (mirrors the record page's
    snapshot discipline, D-02: no poll here either).
    """
    review = await get_file_tracklist_review(session, file_id)
    if review is None:
        # phaze-9xyjp: the file vanished (a concurrent delete_scan cascade or duplicate
        # resolve) between the button render and this click. htmx 2.x's stock
        # responseHandling never swaps a 4xx body (response_shape.py rule 3), so a raw 404
        # here would be silently dropped and the slide-in would sit unchanged with no
        # feedback. The fragment already renders "File not found." for review is None --
        # answer with that at RENDERABLE_ALERT_STATUS instead of raising.
        return templates.TemplateResponse(
            request=request,
            name="record/partials/_tracklist_review_body.html",
            context={"request": request, "file_id": file_id, "review": None, "just_queued": False, "just_refreshed": False},
            status_code=RENDERABLE_ALERT_STATUS,
        )

    queued = False
    if review.tracklist is None and review.actionable:
        await flag_file_for_lookup(session, file_id)
        await session.commit()
        routed = await enqueue_router.resolve_queue_for_task("drain_tracklists", request.app.state, session)
        await routed.queue.enqueue("drain_tracklists", limit=1, target_file_ids=[str(file_id)])
        review = await get_file_tracklist_review(session, file_id)
        queued = True

    return templates.TemplateResponse(
        request=request,
        name="record/partials/_tracklist_review_body.html",
        context={"request": request, "file_id": file_id, "review": review, "just_queued": queued, "just_refreshed": False},
    )


@router.post("/pipeline/tracklists/{file_id}/refresh", response_class=HTMLResponse)
async def refresh_tracklist_lookup_ui(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: re-read this file's 1001Tracklists page, on demand (phaze-2akf).

    The replacement for the retired monthly ``refresh_tracklists`` cron and for the removed
    per-tracklist re-scrape trigger. The cron re-fetched every tracklist older than 90 days, which
    contradicts the drain's cache ("a published tracklist does not change, so never re-fetch") and
    put a second, unbounded consumer on a whole-host budget of ~1 request / 8 s. The operator
    decision was to keep the drain never-re-fetching and make refresh explicit, targeted, and
    operator-initiated -- this button is that trigger, and there is no scheduled counterpart.

    :func:`phaze.tasks.tracklist.refresh_tracklists` is called DIRECTLY rather than enqueued, for
    the same reason ``tracklist_drain_status`` is: it spends NO host requests. All it does is drop
    the positive cache row for this page and flag the files it serves, which re-admits them to the
    drain queue. The ``limit=1`` slice enqueued afterwards is what actually pays for the re-read,
    out of the same budget and through the same single path as every other lookup.

    Offered only where a tracklist already exists -- refreshing a file that has none is what
    Prioritize is for.
    """
    review = await get_file_tracklist_review(session, file_id)
    if review is None:
        # phaze-9xyjp: same vanished-file race as prioritize_tracklist_lookup_ui -- answer
        # with the renderable fragment at RENDERABLE_ALERT_STATUS rather than a 404 htmx
        # would silently drop.
        return templates.TemplateResponse(
            request=request,
            name="record/partials/_tracklist_review_body.html",
            context={"request": request, "file_id": file_id, "review": None, "just_queued": False, "just_refreshed": False},
            status_code=RENDERABLE_ALERT_STATUS,
        )

    refreshed = False
    if review.tracklist is not None:

        @contextlib.asynccontextmanager
        async def _session_factory() -> AsyncIterator[AsyncSession]:
            yield session

        outcome = await refresh_tracklists({"async_session": _session_factory}, file_ids=[str(file_id)])
        refreshed = bool(outcome["refreshed"])
        if refreshed:
            routed = await enqueue_router.resolve_queue_for_task("drain_tracklists", request.app.state, session)
            await routed.queue.enqueue("drain_tracklists", limit=1, target_file_ids=[str(file_id)])
        review = await get_file_tracklist_review(session, file_id)

    return templates.TemplateResponse(
        request=request,
        name="record/partials/_tracklist_review_body.html",
        context={"request": request, "file_id": file_id, "review": review, "just_queued": False, "just_refreshed": refreshed},
    )


@router.post("/pipeline/tracklists/{file_id}/unprioritize", response_class=HTMLResponse)
async def unprioritize_tracklist_lookup_ui(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: clear ``file_id``'s priority flag -- the operator changed their mind.

    A plain no-op (not an error) when the file was never flagged, so a double-click is safe.
    """
    file = await session.get(FileRecord, file_id)
    if file is None:
        # phaze-9xyjp: same vanished-file race as the other two tracklist buttons -- render
        # the fragment (get_file_tracklist_review also returns None for a missing file) at
        # RENDERABLE_ALERT_STATUS instead of a 404 htmx would silently drop.
        return templates.TemplateResponse(
            request=request,
            name="record/partials/_tracklist_review_body.html",
            context={"request": request, "file_id": file_id, "review": None, "just_queued": False, "just_refreshed": False},
            status_code=RENDERABLE_ALERT_STATUS,
        )

    await unflag_file(session, file_id)
    await session.commit()

    review = await get_file_tracklist_review(session, file_id)
    return templates.TemplateResponse(
        request=request,
        name="record/partials/_tracklist_review_body.html",
        context={"request": request, "file_id": file_id, "review": review, "just_queued": False, "just_refreshed": False},
    )


# --- Drain progress fragment + manual slice trigger (phaze-fq9h.8) ---


async def _render_drain_status(request: Request, session: AsyncSession) -> HTMLResponse:
    """Shared render for the drain-status fragment -- read by the GET view and by both the
    arm/disarm POST endpoints below (phaze-6nrrf), so all three always agree on exactly what
    "the current state" means: funnel data from :func:`tracklist_drain_status` (no host
    requests) plus the durable :class:`~phaze.models.tracklist_drain_arm_state.TracklistDrainArmState`
    row (no queue at all -- a plain read).
    """

    @contextlib.asynccontextmanager
    async def _session_factory() -> AsyncIterator[AsyncSession]:
        yield session

    ctx: dict[str, Any] = {"async_session": _session_factory}
    status = await tracklist_drain_status(ctx)
    arm = await get_arm_state(session)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_tracklist_drain_status.html",
        context={
            "request": request,
            "status": status,
            "daily_ceiling": DAILY_LOOKUP_CEILING,
            "arm": arm,
            "tracklist_drain_cooldown_sec": settings.tracklist_drain_cooldown_sec,
        },
    )


@router.get("/pipeline/tracklist-drain-status", response_class=HTMLResponse)
async def tracklist_drain_status_ui(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """HTMX fragment: queue depth, throughput vs the daily ceiling, an honest ETA, and the
    ARM/DISARM state (phaze-6nrrf).

    Calls :func:`phaze.tasks.tracklist_drain.tracklist_drain_status` DIRECTLY (per its own
    docstring: it spends no host requests, so routing it through a queue and a poll would buy
    nothing but latency) rather than reimplementing its funnel here. The task function wants a
    SAQ-shaped ``ctx["async_session"]`` sessionmaker; this request already has a session from the
    normal ``get_session`` dependency (the same one every other fragment in this router reads
    with), so it is wrapped in a trivial one-shot async-context-manager factory rather than
    pulling in the module-level production ``phaze.database.async_session`` -- which would open a
    SECOND connection outside this request's transaction (invisible to it under tests, and an
    unnecessary extra pool checkout in production).
    """
    return await _render_drain_status(request, session)


@router.post("/pipeline/run-tracklist-drain", response_class=HTMLResponse)
async def run_tracklist_drain_ui(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """HTMX endpoint: enqueue one bounded ``drain_tracklists`` slice (phaze-fq9h.8).

    The drain is deliberately operator-initiated, never a cron (see ``tasks.tracklist_drain``'s
    module docstring's ethics bound: it deploys from a residential IP, runs a headful browser,
    and spends a shared public host's published budget) -- this endpoint is that trigger.
    ``limit`` is left at the default (:data:`~phaze.services.tracklist_drain.DEFAULT_LOOKUP_LIMIT`
    lookups); any operator-flagged files are picked up automatically by ``build_drain_queue``
    from the persisted store without needing to be passed here.
    """
    routed = await enqueue_router.resolve_queue_for_task("drain_tracklists", request.app.state, session)
    await routed.queue.enqueue("drain_tracklists")
    response = templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_run_drain_response.html",
        context={"request": request},
    )
    # phaze-k2ob4: the drain-status panel (_tracklist_drain_status.html) is loaded ONCE on mount
    # (hx-trigger=load) and carries no self-poll (WORK-05/R-2 forbids a second loop here) -- so
    # without this, Queued/Answered-by-cache/Prioritized/ETA never move after this click, exactly
    # contradicting the promise in _run_drain_response.html. HX-Trigger fires drain-refresh on the
    # element that issued this POST; it bubbles to <body>, where
    # #tracklist-drain-status-view's own hx-trigger (load, drain-refresh from:body) re-GETs the
    # panel -- one bounded re-fetch per click, never an interval.
    response.headers["HX-Trigger"] = "drain-refresh"
    return response


@router.post("/pipeline/arm-tracklist-drain", response_class=HTMLResponse)
async def arm_tracklist_drain_ui(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """HTMX endpoint: the operator's ONE consent decision to continuously drain (phaze-6nrrf).

    Persists ``armed=true`` on the durable :class:`~phaze.models.tracklist_drain_arm_state.TracklistDrainArmState`
    row and returns immediately -- it does NOT enqueue a slice itself. The
    ``continue_armed_tracklist_drain`` CronJob (``tasks/tracklist_drain_control.py``, every-minute
    cadence like this controller's other reapers) picks the armed flag up on its next tick and
    enqueues the first slice; letting the cron own every enqueue decision (rather than this
    endpoint racing it for one) keeps ``in_flight`` a single source of truth with no double-enqueue
    window to reason about. The single-slice ``Run tracklist lookups`` button above remains for an
    immediate one-off boost that does not wait for the next tick.

    The single-consent framing matters: this is the ONLY code path that ever sets ``armed=true`` --
    a restart, a redeploy, or the continuous-drain cron itself never do (see that module's and the
    model's own docstrings for the full ethics-bound rationale).
    """
    await arm_drain(session)
    await session.commit()
    return await _render_drain_status(request, session)


@router.post("/pipeline/disarm-tracklist-drain", response_class=HTMLResponse)
async def disarm_tracklist_drain_ui(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """HTMX endpoint: the operator's explicit "stop the continuous drain" control (phaze-6nrrf).

    Persists ``armed=false`` immediately. A slice already ``in_flight`` (enqueued by the cron
    before this click) is left completely alone -- this endpoint touches only the arm row, never
    ``saq_jobs`` -- so it finishes normally; :func:`~phaze.tasks.tracklist_drain_control.record_drain_slice_completion`
    clears ``in_flight`` when it does. The acceptance criterion is "disarm stops after the
    in-flight slice, never kills one mid-flight", and doing nothing to the running job IS that:
    there is no cancellation path here, deliberately.

    A no-op (not an error) when already disarmed -- reachable at any time per the bead's own
    acceptance criteria, including a double-click or a race with an auto-disarm that just fired.
    """
    await disarm_drain(session, reason="operator")
    await session.commit()
    return await _render_drain_status(request, session)
