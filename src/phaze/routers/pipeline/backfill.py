"""Cloud backfill trigger."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update

from phaze.config import settings
from phaze.database import get_session
from phaze.models.analysis import AnalysisResult
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.pipeline._common import logger, router, templates
from phaze.routers.pipeline.analysis import (
    _analysis_file_ids_scope,
    _ledger_keys_scope,
    _route_discovered_by_duration,
    _scheduling_ledger_cas_delete_stmt,
)
from phaze.services.analysis_enqueue import process_file_job_key
from phaze.services.pipeline import count_backfill_candidates, get_backfill_candidates, get_live_job_keys
from phaze.services.route_control import get_route_control


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@router.post("/pipeline/backfill-cloud", response_class=HTMLResponse)
async def trigger_backfill_cloud(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: backfill the timed-out long files to the cloud (Phase 49, D-08/D-10; 83-06 REVERSES D-09).

    Selects EXACTLY the timed-out long set — ``ANALYSIS_FAILED ∧ duration >= cloud_route_threshold_sec``
    (the explicit :func:`count_backfill_candidates` / :func:`get_backfill_candidates` filter, NOT a
    whole-backlog ``ANALYSIS_FAILED`` sweep), and routes the candidates through the SAME per-file
    duration router (:func:`_route_discovered_by_duration`) "Run Analysis" uses, so the two paths cannot
    drift: every candidate is long, so the router HOLDS it in ``AWAITING_CLOUD`` (an awaiting
    ``cloud_job`` sidecar row), never a direct enqueue.

    83-06 (OPTION A, CONSCIOUSLY REVERSES D-09 — accepted by the phase owner): the held file is made a
    CLEAN drainable candidate for BOTH cloud targets (compute AND kueue; the all-local case early-returns
    below). After the hold, in one transaction, the endpoint (1) CLEARS the ``analysis.failed_at`` /
    ``error_message`` marker (mirrors :func:`retry_analysis_failed`) and (2) DELETES the orphaned
    ``process_file:<id>`` scheduling-ledger row, KEEPING only the awaiting ``cloud_job`` row as the SOLE
    in-flight/recovery registry (exactly like a normal "Run Analysis"-held file and the k8s path). The
    former D-09 held-file ledger SEED (compute) / SKIP (kueue) fork is GONE — neither branch seeds a
    ledger row now, so the compute/kueue paths are unified.

    WHY the reversal is safe (net over-enqueue REDUCTION): a RETAINED ``failed_at`` made the held file
    analyze-domain-completed and a RETAINED ledger row made it analyze-in-flight, so
    ``awaiting_candidate_clause`` (``~inflight ∧ ~domain_completed``) EXCLUDED it and
    :func:`stage_cloud_window` never drained it (83-06). Clearing both markers lets the bounded drain
    dispatch it to the compute/kueue backend — the single owner. D-09's stated ledger-replay recovery
    purpose was ALREADY dead: ``analysis.failed_at`` put the held file in ``recover_orphaned_work``'s
    analyze domain-completed exclusion, so the seeded row was never replayed.

    The explicit ``failed_clause(ANALYZE)`` filter plus the ``~exists(active cloud_job)`` idempotency
    guard in :func:`_backfill_candidates_stmt` still close the over-enqueue class (D-10): a double-click
    selects nothing new (the held files now carry an awaiting ``cloud_job`` row), and short / never-failed
    files are never touched.
    """
    # Phase 51 (D-03, Pitfall 2 / T-51-02): explicit cloud on/off guard BEFORE the candidate query.
    # Gating only the routing seam is insufficient -- backfill would still reset the 144
    # ANALYSIS_FAILED long files to DISCOVERED and re-route them local to re-time-out. When the
    # registry is all-local (cloud_enabled False, Phase 67 / D-14) this is a clean no-op that mutates
    # ZERO file.state rows -- byte-identical to the former all-local selector guard.
    # Phase 71 (BEUI-02, D-08, T-71-08): the force-local override is the THIRD gate site. Forced-local
    # must behave EXACTLY like the all-local path here too -- otherwise backfill would reset the failed
    # long files to DISCOVERED and HOLD them in AWAITING_CLOUD while the (forced) drain no-ops, stranding
    # them. Folding force_local into this same early-return keeps backfill a clean ZERO-mutation no-op.
    if not settings.cloud_enabled or await get_route_control(session):
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/backfill_response.html",
            context={"request": request, "count": 0, "disabled": True},
        )

    threshold = settings.cloud_route_threshold_sec
    count = await count_backfill_candidates(session, threshold)
    if count == 0:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/backfill_response.html",
            context={"request": request, "count": 0},
        )

    candidates = await get_backfill_candidates(session, threshold)

    # phaze-l1km: the candidate query keys on "a process_file:<id> ledger row EXISTS", which is TRUE
    # both for an orphaned row (a timed-out job -- the set this backfill re-drives) AND for the LIVE
    # in-flight marker of a still-running re-analysis. A producer that re-enqueues process_file
    # WITHOUT clearing failed_at (the removed deepen path did this; a recovery replay can too) leaves
    # a long ANALYSIS_FAILED file whose live job grinds for hours satisfying every candidate conjunct
    # -- and exhaustive analysis makes "grinds for hours" the normal case, not the rare one. Deleting its ledger row + holding it for
    # the cloud drain would DOUBLE-DISPATCH the same file to local + cloud and orphan the live local
    # job from queue-loss recovery. Skip any candidate whose deterministic process_file key is LIVE
    # (queued/active) in saq_jobs -- the same liveness signal recovery's ledger-minus-live-keys set
    # uses. get_live_job_keys is degrade-safe (empty set on a missing/unreadable saq_jobs table), so an
    # env without the broker table backfills exactly as before (every candidate treated as an orphan).
    live_keys = await get_live_job_keys(session)
    if live_keys:
        skipped = [file for file, _ in candidates if process_file_job_key(file.id) in live_keys]
        if skipped:
            candidates = [(file, dur) for file, dur in candidates if process_file_job_key(file.id) not in live_keys]
            logger.info(
                "backfill_cloud: skipped files with a LIVE process_file job (phaze-l1km double-dispatch guard)",
                skipped=[str(file.id) for file in skipped],
            )
    # The response count reflects the files actually acted on (live-job files were excluded above).
    count = len(candidates)
    if count == 0:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/backfill_response.html",
            context={"request": request, "count": 0},
        )

    # 83-06 (OPTION A, CONSCIOUSLY REVERSES D-09): make every cloud-routed backfill candidate a CLEAN
    # drainable held file, for BOTH the compute AND the kueue target (the all-local case already
    # returned early above). The hold (``hold_awaiting_cloud`` inside ``_route_discovered_by_duration``)
    # and the two marker strips below MUST land in ONE transaction:
    #   1. Clear ``analysis.failed_at`` / ``error_message`` (mirrors :func:`retry_analysis_failed`): a
    #      RETAINED marker made the held file analyze-domain-completed, so ``~domain_completed_clause``
    #      was False and the drain skipped it.
    #   2. DELETE the orphaned ``process_file:<id>`` ledger row (the backfill candidate query REQUIRES
    #      it): its presence made the file analyze-in-flight, so ``~inflight_clause`` was False.
    # phaze-7g4t: STAGE the marker strips BEFORE routing (do NOT commit them separately). The old code
    # committed the holds inside ``_route_discovered_by_duration`` and THEN committed the marker strips
    # in a SECOND transaction -- an interruption (DB error on the UPDATE/DELETE, server restart, or
    # handler-task cancellation) between the two commits left every candidate as
    # {cloud_job='awaiting' + failed_at set + ledger row}, which every forward path excludes: the drain
    # never picks it (retained ledger => in-flight, retained failed_at => domain-completed), the
    # Awaiting-cloud card shows 0, re-running Backfill selects nothing (active cloud_job), Run Analysis
    # skips it (~failed conjunct), and recovery excludes any awaiting cloud_job -- a permanent invisible
    # strand. Staging the strips into the session first means the hold's single commit inside
    # ``_route_discovered_by_duration`` flushes ALL THREE mutations atomically. Every backfill candidate
    # is long (the query filters ``duration >= threshold``) and cloud is enabled here, so the router
    # ALWAYS holds >=1 file and therefore ALWAYS commits -- the staged strips can never be left dangling.
    # phaze-r7j9: both id/key lists below are array-bound as ONE Postgres array parameter
    # (`_analysis_file_ids_scope` / `_ledger_keys_scope`) rather than a bare `.in_(...)`, which
    # SQLAlchemy expands to one bind parameter per element and exceeds asyncpg's 32767-parameter
    # cap on a large enough candidate set (same failure mode as `retry_analysis_failed`'s marker
    # clear, lower reachability here since candidates are duration-gated).
    candidate_ids = [file.id for file, _ in candidates]
    if candidate_ids:
        await session.execute(
            update(AnalysisResult).where(_analysis_file_ids_scope(candidate_ids, "candidate_ids")).values(failed_at=None, error_message=None),
        )
        # phaze-g31m: CAS the ledger DELETE on the ``enqueued_at`` THIS transaction observes right here,
        # not a bare key-membership DELETE. The live_keys snapshot above is a lock-free read taken before
        # this point, so a concurrent process_file enqueue (retry_analysis_failed's background loop,
        # a recovery replay) for one of these EXACT candidates can land in the gap between that snapshot
        # and this statement (each such gap is a single DB round trip wide). ``upsert_ledger_entry`` --
        # the SAQ before_enqueue write hook every process_file producer shares -- refreshes
        # ``enqueued_at`` on EVERY re-enqueue of a still-existing key, including that race. Conditioning
        # the delete on the value just read here means a concurrent re-enqueue's ledger commit landing in
        # that gap changes the row this DELETE is looking for, so it can never remove a ledger row a live
        # producer has claimed -- the row survives, the file stays correctly in-flight, and this
        # candidate is silently left for a later backfill click instead of being double-dispatched to
        # local AND cloud.
        #
        # This closes the window where the CONCURRENT enqueue's ledger write itself lands here (the
        # candidate's row visibly changes). It does NOT close the narrower "ledger already committed,
        # the matching saq_jobs row insert still pending" interleaving the live_keys snapshot can also
        # miss -- that shape is indistinguishable from a genuine stale orphan by ANY read of this row's
        # own content (its enqueued_at never changes).
        #
        # phaze-8xbv (ADR-0003, docs/design/0003-backfill-ledger-race-residual-window.md): this
        # residual window is a PERMANENT, DELIBERATE acceptance, not open follow-up work. Closing it
        # would require intercepting SAQ's PostgresQueue.enqueue() to hold one advisory lock across
        # its before_enqueue hook chain (our asyncpg ledger write) AND its internal job insert (SAQ's
        # own psycopg3 pool) as a single unit -- a third-party-library-internals intrusion to close a
        # window that is one DB round trip wide, never causes double-dispatch, and self-heals on the
        # next backfill click. See the ADR for the full two-pool analysis.
        ledger_keys = [process_file_job_key(fid) for fid in candidate_ids]
        observed_ledger_rows = (
            await session.execute(select(SchedulingLedger.key, SchedulingLedger.enqueued_at).where(_ledger_keys_scope(ledger_keys, "ledger_keys")))
        ).all()
        if observed_ledger_rows:
            await session.execute(_scheduling_ledger_cas_delete_stmt(observed_ledger_rows))

    counts = await _route_discovered_by_duration(
        request.app.state,
        session,
        candidates,
        threshold,
        # cloud is enabled here: the `not settings.cloud_enabled` early-return guard above already
        # short-circuited the all-local case, so the registry holds a non-local backend and the router
        # must hold the long files for the cloud path. Pass True unconditionally.
        True,
        settings.models_path,
    )

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/backfill_response.html",
        context={
            "request": request,
            "count": count,
            "cloud": counts["cloud"],
            "awaiting": counts["awaiting"],
        },
    )
