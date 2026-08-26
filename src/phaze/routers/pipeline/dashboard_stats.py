"""Pipeline dashboard + stats-poll routes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from phaze.config import settings
from phaze.database import get_session
from phaze.enums.stage import Status
from phaze.models.agent import Agent
from phaze.routers.pipeline._common import logger, router, templates
from phaze.routers.pipeline_scans import build_recent_scans
from phaze.services.agent_liveness import derive_compute_lane_identities
from phaze.services.backends import (
    derive_cloud_hold_reason,
    derive_localqueue_unreachable,
    get_analysis_activity_counts,
    get_analysis_live_count,
    get_analyze_queue_totals,
    get_backend_lane_snapshot,
)
from phaze.services.pipeline import (
    _read_in_own_session,
    _stats_fanout,
    analyze_lanes_content_hash,
    count_active_agents,
    get_analysis_failed_count,
    get_analysis_stalled_count,
    get_awaiting_cloud_count,
    get_cached_stage_orphan_counts,
    get_cloud_phase_counts,
    get_global_reconciliation,
    get_inadmissible_count,
    get_match_busy_count,
    get_metadata_selection_summary,
    get_pushed_count,
    get_pushing_count,
    get_queue_activity,
    get_stage_activity_snapshot,
    get_stage_controls,
    get_stage_progress,
    queue_progress_percent,
)
from phaze.services.pipeline_counters import read_counters
from phaze.telemetry.pipeline import record_backlog, record_stage_inflight


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Maps each DAG node whose ``done`` is DB-sourced to the maintained ``completed``
# counter function(s) backing it (35-01). Used as a DOCUMENTED degrade-fallback (D-02):
# when a node's ``get_stage_progress`` ``done`` reads 0 (its ``_safe_count`` degraded OR
# the stage is genuinely empty) AND the mapped ``completed`` counter is > 0, the counter
# value renders as the fallback ``done``. DB-truth ALWAYS wins when ``done > 0`` (D-03:
# the DB reconcile is the authority; the counter is a backstop cache, never an override).
# ``discovery`` and ``execute`` have no maintained counter (``scan_directory`` /
# ``execute_approved_batch`` are deterministic-key-exempt), so they never fall back.
# phaze-y0wz: the counter only exceeds 0 after real completions, but it is a durable, never-reset
# INCR (services/pipeline_counters.py) that OUTLIVES the rows it counted — a full corpus delete
# (``delete_scan``) cascades away FileRecord/metadata/analysis but never touches Redis. So
# ``done==0`` does NOT imply "counter is also 0 there"; a genuinely-emptied corpus reads
# ``done==0``/``total==0`` while the counter still carries the whole pre-delete completion count.
# ``_reconciled_done`` (phaze-89tw) only renders the fallback when it is a genuine, boundable
# partial progress signal — ``0 < fallback < stage_total`` — UNCONDITIONALLY, including
# ``stage_total == 0``, where the fallback always degrades to ``stage_done``. A fallback at or
# beyond ``stage_total`` never renders (not even capped to the total): the durable counters are
# never-reset and routinely exceed a re-scanned/degraded node's current total, so rendering the
# total itself would falsely claim 100% done exactly when the DB says nothing is done.
# WR-03 unit constraint: a node may map ONLY to per-file SAQ functions, because the node's
# ``done`` is a distinct-file/tracklist count and the fallback renders the counter AS that
# ``done``. ``generate_proposals`` is a BATCH task (one job == N files), so its ``completed``
# counter counts batches, not files — mapping it here would render a batch count as a file
# ``done`` (e.g. 1 batch of 10 files -> proposalsDone=1). It is therefore intentionally OMITTED;
# proposalsDone falls back to DB-truth (0 when degraded) rather than a wrong-unit number.
# phaze-2akf: the former ``scan_search`` -> ``search_tracklist`` and ``scrape`` ->
# ``scrape_and_store_tracklist`` entries are gone with those tasks. The ``tracklist`` node (the
# renamed ``scan_search``) is deliberately NOT remapped onto ``drain_tracklists``: the WR-03 unit
# constraint below requires a per-FILE SAQ function, and one drain job is a bounded SLICE covering
# many files, so its ``completed`` counter counts slices. Mapping it here would render a slice count
# as a file ``done``. The node falls back to DB-truth instead, which for this node is exact.
_NODE_COMPLETED_FNS: dict[str, tuple[str, ...]] = {
    "metadata": ("extract_file_metadata",),
    "analyze": ("process_file",),
    "match": ("match_tracklist_to_discogs",),
}


async def _read_pipeline_counters(app_state: Any) -> dict[str, dict[str, int]]:
    """Read the maintained per-function Redis counters, degrading to ``{}`` on any failure.

    Mirrors :func:`get_queue_activity`'s failure isolation: a missing ``app.state``
    handle (the test client skips the lifespan) or any Redis hiccup must degrade the
    counter source to an empty dict so the 5s dashboard poll renders from DB-truth and
    NEVER 500s (threat T-35-09). Reads the shared ``app.state.redis`` cache client
    (decode_responses), which the lifespan always wires. Phase 36: the former
    ``controller_queue.redis`` fallback is gone -- the broker is Postgres now and has no
    Redis client to borrow. When ``app.state.redis`` is absent (the test client skips the
    lifespan) the ``getattr`` returns ``None`` and ``read_counters(None)`` degrades via the
    except below.
    """
    try:
        redis = getattr(app_state, "redis", None)
        return await read_counters(redis)
    except Exception:
        logger.warning("pipeline_counters_degraded", exc_info=True)
        return {}


def _reconciled_done(node: str, stage_done: int, stage_total: int, counters: dict[str, dict[str, int]]) -> int:
    """Return the DB-truth ``done`` (D-03), or the ``completed`` counter as a backstop.

    DB-truth wins whenever ``stage_done > 0``. Only when the DB source reads 0 do we fall
    back to the sum of the node's mapped ``completed`` counters (D-02 backstop) — and only
    if that sum is itself > 0.

    phaze-89tw: the fallback is NEVER allowed to render as ``stage_total`` (100% done). The
    Redis ``completed:<function>`` counters are durable, never-reset, plain ``INCR``s (see
    ``services/pipeline_counters.py``) that OUTLIVE the rows they counted, so for any mature
    archive the cumulative counter is routinely LARGER than the node's current ``total`` —
    which used to make ``min(fallback, stage_total)`` collapse to exactly ``stage_total``
    whenever ``stage_done == 0``, i.e. the backstop rendered the node 100% complete precisely
    in the state where the DB says nothing is done (a degraded read, per phaze-89tw scenario
    A, or a corpus re-scan after delete-scan, scenario B). ``stage_done == 0`` is an
    overloaded sentinel here — ``_safe_count`` / ``_safe_bucket_counts`` return 0 for BOTH
    "query failed" and "genuinely empty", so this function cannot tell degrade from empty and
    must never manufacture a value equal to the denominator either way.

    The fallback is therefore used ONLY when it represents genuine, boundable partial
    progress: ``0 < fallback < stage_total``. Any fallback that is at or beyond the
    denominator (including the ``stage_total == 0`` case carried over from phaze-y0wz, where
    an emptied corpus must not render its pre-delete completion history) degrades to
    ``stage_done`` — already known to be 0 from the guard above — rather than the misleading
    ceiling value. This also covers ``tracklist``, whose ``total`` the DB layer documents as
    ALWAYS ``None`` -> 0 (``get_stage_progress``); since phaze-2akf it maps to no counter at all,
    so it can never render a phantom ``done`` from either direction.
    """
    if stage_done > 0:
        return stage_done
    fallback = sum(counters.get(fn, {}).get("completed", 0) for fn in _NODE_COMPLETED_FNS.get(node, ()))
    if fallback <= 0:
        return stage_done
    if stage_total <= 0 or fallback >= stage_total:
        return stage_done
    return fallback


def _derive_stats(stage_progress: dict[str, dict[str, int | None]]) -> dict[str, int]:
    """Re-express the seven former ``get_pipeline_stats`` keys off ``get_stage_progress`` (Phase 82, D-05/READ-02).

    The stats path no longer reads the raw ``files.state`` column: each of the seven keys ``stats_bar.html``
    consumes maps to a derived :func:`get_stage_progress` output-table count --
    ``discovered→discovery.done``, ``metadata_extracted→metadata.done``, ``analyzed→analyze.done``, ``proposal_generated→proposals.done``, ``approved→execute.total``,
    ``executed→execute.done``. The key NAMES are preserved so ``stats_bar.html``'s six visible cards +
    three OOB ``x-init`` store writes need NO template change (the Alpine ``$store.pipeline.*`` keys stay
    stable, Pitfall 4 -- only the server-side source changes). ``queue_progress_percent`` consumes the
    same derived ``analyzed`` numerator.

    SEMANTIC SHIFT (this is the deadlock dissolving, not a regression): ``metadata_extracted`` now counts
    every music/video file whose metadata is done (a ``metadata`` row with ``failed_at`` NULL), NOT the
    transient linear ``METADATA_EXTRACTED`` state (which a file left on advancing onward).
    These numbers legitimately differ post-cutover.
    """

    def done(node: str) -> int:
        return int(stage_progress[node]["done"] or 0)

    return {
        "discovered": done("discovery"),
        "metadata_extracted": done("metadata"),
        "analyzed": done("analyze"),
        "proposal_generated": done("proposals"),
        "approved": int(stage_progress["execute"]["total"] or 0),
        "executed": done("execute"),
    }


async def _build_dag_context(
    app_state: Any,
    session: AsyncSession,
    activity: dict[str, int],  # noqa: ARG001 — kept for caller stability; analyzeActive now derives from stage_progress (Phase 93)
    stage_progress: dict[str, dict[str, int | None]] | None = None,
) -> dict[str, dict[str, int]]:
    """Build the per-DAG-node store-key context consumed by stats_bar.html + the 35-05 canvas.

    Reconciles three sources (D-03): ``get_stage_progress`` (DB-truth ``done``/``total`` per
    node, the authority), the maintained Redis ``completed`` counters (a degrade backstop via
    :func:`_reconciled_done`), and the already-computed ``get_queue_activity`` (the per-node
    ACTIVE state). Every value is a plain ``int`` (``total=None`` em-dash sentinels collapse to
    0 — the Scan/Search node has NO ``tracklistTotal`` store key, so its em-dash stays a
    render-side concern) so it is safe to interpolate into the ``x-init`` numeric store writes.

    ``stage_progress`` is the already-computed :func:`get_stage_progress` result, passed through by
    both the dashboard and poll callers so the (heavy, multi-count) read happens ONCE per request
    (Phase 82, D-05 -- the former ``get_pipeline_stats`` pass-through this replaces). When omitted
    (direct test callers) it is computed here.

    Returns ``{"dag": {<storeKey>: int, ...}}`` carrying every per-node sub-key seeded into
    ``$store.pipeline`` (base.html, 35-04 Task 1).
    """
    stage = stage_progress if stage_progress is not None else await get_stage_progress(session)
    counters = await _read_pipeline_counters(app_state)

    def done(node: str) -> int:
        return _reconciled_done(node, int(stage[node]["done"] or 0), int(stage[node]["total"] or 0), counters)

    def total(node: str) -> int:
        return int(stage[node]["total"] or 0)

    metadata_buckets = stage["metadata"]
    metadata_status_total = sum(int(value or 0) for key, value in metadata_buckets.items() if key not in {"total", "available"})
    dag: dict[str, int] = {
        "metadataDone": done("metadata"),
        "metadataTotal": total("metadata"),
        "metadataFailed": int(stage["metadata"].get("failed") or 0),
        "metadataStatusDone": int(metadata_buckets.get(Status.DONE.value) or 0),
        "metadataStatusFailed": int(metadata_buckets.get(Status.FAILED.value) or 0),
        "metadataStatusTotal": metadata_status_total,
        "metadataStatusKnown": int(metadata_buckets.get("available") or 0),
        "analyzeDone": done("analyze"),
        "analyzeTotal": total("analyze"),
        # Phase 93 (CONSOLE-02): the DERIVED in-flight count — the same stage_status_case bucket the
        # Files matrix renders (scheduling_ledger truth, so cloud-burst dispatch counts). The former
        # SAQ agent_active source saw only LOCAL agent queues and read 0 while thousands of analyze
        # jobs were in flight on the compute lanes.
        "analyzeActive": int(stage["analyze"].get("in_flight") or 0),
        # phaze-2akf: scrapeDone / scrapeTotal are gone with the ``scrape`` node -- see
        # get_stage_progress for why that node was a tautology once the drain collapsed the
        # search/scrape split into one operation.
        "tracklistDone": done("tracklist"),
        "matchDone": done("match"),
        "matchTotal": total("match"),
        "proposalsDone": done("proposals"),
        "proposalsTotal": total("proposals"),
        # Approve→Execute gates on the approved-proposal count; execute.total IS that count.
        "approved": total("execute"),
        "executedDone": done("execute"),
        "executedTotal": total("execute"),
    }

    # Phase 38 (38-03 / REQ-38-4): overlay the live per-stage pause/priority intent so the
    # DAG controls reflect authoritative server state across every 5s poll. get_stage_controls
    # owns the never-500 degrade (returns paused=False/priority=50 defaults on any failure), so
    # NO try/except is added here. paused is coerced to int 0/1 — never a Python bool — to keep
    # every dag value a server-computed int safe to interpolate into x-init (Pitfall 3 / T-35-11).
    controls = await get_stage_controls(session)
    for stage_name in ("metadata", "analyze"):
        dag[f"{stage_name}Paused"] = int(controls[stage_name]["paused"])
        dag[f"{stage_name}Priority"] = int(controls[stage_name]["priority"])

    # Phase 87 (87-08, UI-05 / D-05): per-enrich-stage orphaned/stuck (recovery-candidate) count --
    # the exact number recover_orphaned_work would re-enqueue for the stage (ledger minus live minus
    # domain-completed minus in-flight-cloud). Phase 91 (HYG-01 / WR-02): the hot 5s /pipeline/stats
    # poll now reads the O(1) process-scope cache (get_cached_stage_orphan_counts -- no session, no
    # await) instead of materializing the full scheduling_ledger inline per tick; the FastAPI lifespan
    # _orphan_refresh_loop refreshes that cache off-request (D-01/D-02/D-04). The parity meaning is
    # unchanged: the cached ints ride the same dag.items() OOB seed loop onto the amber rail badges.
    orphans = get_cached_stage_orphan_counts()
    dag["metadataOrphan"] = int(orphans["metadata"])
    dag["analyzeOrphan"] = int(orphans["analyze"])

    # t7k FIX2 (REQ-260613-t7k-FIX2): per-stage in-flight busy counts REPLACE the single global
    # agentBusy gate so the agent enqueue buttons gate independently (run in parallel).
    # get_stage_activity_snapshot owns the never-500 degrade, and separates queued from active while
    # preserving metadataBusy/analyzeBusy as their sums for the existing enqueue gates.
    selection = await get_metadata_selection_summary(session)
    dag["metadataEligible"] = int(selection.eligible_count or 0)
    dag["metadataEligibleKnown"] = int(selection.available)

    stage_activity = await get_stage_activity_snapshot(session)
    # phaze-m1drf.1: publish the per-stage queued/active depths phaze-zaf2l sampled by hand
    # from `saq_jobs` every 120 s. Only when the read SUCCEEDED -- `available` is False on a
    # degraded read, and publishing its zeros would report an empty queue rather than an
    # unknown one, which is the failure `get_stage_activity_snapshot` exists to avoid.
    if stage_activity.available:
        record_stage_inflight(stage_activity.counts)
    dag["metadataQueued"] = int(stage_activity.counts["metadata"]["queued"])
    dag["metadataActive"] = int(stage_activity.counts["metadata"]["active"])
    dag["metadataQueueKnown"] = int(stage_activity.available)
    dag["metadataBusy"] = dag["metadataQueued"] + dag["metadataActive"]
    dag["analyzeBusy"] = int(stage_activity.counts["analyze"]["queued"] + stage_activity.counts["analyze"]["active"])

    # Phase 40 (REQ-40-3): the per-agent DAG nodes gate on an online-agent signal ("Needs agent").
    # count_active_agents owns its own never-500 SAVEPOINT degrade (returns 0 on any DB error), so NO
    # try/except is added here; the int rides the same dag.items() seed + OOB loop. It is a count where
    # 0 == "no online agent" (fail-safe default that leaves the node blocked).
    dag["agentOnline"] = int(await count_active_agents(session))

    # Phase 58 (58-04, WORK-03): the Analyze A1 lane's "compute online" capacity numeral -- a
    # READ-ONLY kind-scoped count of online compute agents, using the SAME liveness predicate as
    # agentOnline (count_active_agents owns the never-500 SAVEPOINT degrade -> 0 on any DB error,
    # so NO try/except here). It rides the existing dag.items() OOB seed loop onto the
    # dag-seed-computeOnline placeholder the Analyze workspace pre-mounts (B1: an OOB seed lands
    # only on an id already in the DOM) -- no second poll, no stats_bar.html edit, no new backend.
    dag["computeOnline"] = int(await count_active_agents(session, kind="compute"))

    # COMPUTE-02: the header "Agents · N" count includes ACTIVE compute lanes alongside
    # heartbeating agents via a NEW additive key -- agentOnline's 0-degrade fail-safe semantics
    # (scan-launch gate) are UNTOUCHED. derive_compute_lane_identities owns its own never-500
    # degrade (returns all-IDLE lanes on any DB error), so NO try/except is added here; only
    # ACTIVE lanes count (IDLE configured clusters are not "active"; WAITING is a quota alarm,
    # not an online worker). It rides the same dag.items() seed + OOB loop, no stats_bar.html edit.
    dag["computeLanesActive"] = sum(1 for lane in await derive_compute_lane_identities(session) if lane.state == "ACTIVE")

    # Phase 41 (REQ-41-3): the match_tracklist_to_discogs in-flight count gates the DAG Match trigger
    # node "busy" (Matching…). It is a controller task (NOT part of get_stage_busy_counts's agent
    # stages) -- get_match_busy_count owns its own never-500 SAVEPOINT degrade (returns 0 on any DB
    # error), so NO try/except is added here; the int rides the same dag.items() seed + OOB loop.
    # (matchTotal/matchDone are already seeded above; the gate derives pending = total - done
    # client-side.) phaze-2akf removed the searchBusy / scrapeBusy siblings along with the two
    # legacy tasks they counted -- with no such jobs left to enqueue, both were pinned at 0.
    dag["matchBusy"] = int(await get_match_busy_count(session))

    # Phase 58 (58-02, WORK-01): the Discover "not yet enriched" backlog -- a READ-ONLY derived
    # int (music/video files whose metadata is not yet done), clamped >= 0. Phase 82 (D-05) derives
    # it from the get_stage_progress metadata node (total - done) instead of the removed
    # get_pipeline_stats (discovered - metadata_extracted), which read FileRecord.state. No new query
    # path (``stage`` is already computed above) and no new poll: it rides the existing dag.items()
    # OOB seed loop onto the dag-seed-notYetEnriched placeholder the workspaces pre-mount.
    dag["notYetEnriched"] = max(int(stage["metadata"]["total"] or 0) - int(stage["metadata"]["done"] or 0), 0)

    return {"dag": dag}


def _shared_stats_context(
    *,
    stats: dict[str, int],
    analysis_failed_count: int,
    analysis_stalled_count: int,
    awaiting_cloud_count: int,
    awaiting_hold_reason: str,
    pushing_count: int,
    analyzing_cloud_count: int,
    inadmissible_count: int,
    localqueue_unreachable: bool,
    cloud_phase_counts: dict[str, int],
    lanes: list[dict[str, Any]],
    analyze_queue_totals: dict[str, int | None],
    activity: dict[str, int],
    dag_ctx: dict[str, Any],
    queue_progress: int,
) -> dict[str, Any]:
    """The ~17-key context slice :func:`build_dashboard_context` and :func:`pipeline_stats_partial` MUST
    seed identically (the OOB swap contract, called out at every card's read site below and in both
    callers) -- extracted as a single source of truth (phaze-bk9el.11) rather than the two hand-copied
    dicts this used to be, which repowise's duplication scan measured at 57 shared lines. Every value
    the two callers derive differently (which reads run sequential vs. ``asyncio.gather``-fanned-out, and
    each caller's own page-only or poll-only extras) stays in the caller; only the assembled dict shape
    that must agree between them lives here.
    """
    # phaze-m1drf.1 acceptance 3: publish the waiting-room depths the operator otherwise
    # counts in psql. POLL-DRIVEN by construction -- this function runs only when the admin
    # UI asks -- so these series go stale with no tab open. Dashboard material, never alert
    # material; see phaze/telemetry/pipeline.py.
    record_backlog(
        {
            "awaiting_cloud": awaiting_cloud_count,
            "analyzing_cloud": analyzing_cloud_count,
            "pushing": pushing_count,
            "inadmissible": inadmissible_count,
            "analysis_failed": analysis_failed_count,
            "analysis_stalled": analysis_stalled_count,
            "queued_behind_quota": cloud_phase_counts["queued_behind_quota"],
            "admitted": cloud_phase_counts["admitted"],
            "running": cloud_phase_counts["running"],
            "finished": cloud_phase_counts["finished"],
            **({"queued_analyze": analyze_queue_totals["total_queued"]} if analyze_queue_totals["total_queued"] is not None else {}),
            **({"unrouted_queued_analyze": analyze_queue_totals["unrouted_queued"]} if analyze_queue_totals["unrouted_queued"] is not None else {}),
        }
    )
    return {
        "stats": stats,
        "settings_batch_size": settings.llm_batch_size,
        "analysis_failed_count": analysis_failed_count,
        "analysis_stalled_count": analysis_stalled_count,
        "awaiting_cloud_count": awaiting_cloud_count,
        "awaiting_hold_reason": awaiting_hold_reason,
        "pushing_count": pushing_count,
        "analyzing_cloud_count": analyzing_cloud_count,
        "inadmissible_count": inadmissible_count,
        "localqueue_unreachable": localqueue_unreachable,
        "queued_behind_quota_count": cloud_phase_counts["queued_behind_quota"],
        "admitted_count": cloud_phase_counts["admitted"],
        "running_count": cloud_phase_counts["running"],
        "finished_count": cloud_phase_counts["finished"],
        "lanes": lanes,
        "total_queued_analyze": analyze_queue_totals["total_queued"],
        "unrouted_queued_analyze": analyze_queue_totals["unrouted_queued"],
        **activity,
        **dag_ctx,
        "queue_progress_percent": queue_progress,
    }


async def build_dashboard_context(app_state: Any, session: AsyncSession) -> dict[str, Any]:
    """Build the pipeline-dashboard render context, shared by ``/pipeline/`` and the shell ``/`` Analyze node.

    Factored out of :func:`dashboard` (Phase 57, RESEARCH Open-Q2 / D-01) so the legacy
    dashboard page and the v7.0 shell's Analyze default render the SAME DAG content from a
    SINGLE source — there is no duplicated query logic that could drift between the two
    entry points. Returns every context key the dashboard template tree consumes EXCEPT
    ``request`` (each caller injects its own). ``app_state`` is ``request.app.state``.

    Every read here is degrade-safe at the service layer (the services own their never-500
    SAVEPOINT/``_safe_count`` fallbacks and the queue/counter reads isolate their own
    failures), so this builder never 500s the page.
    """
    # Phase 82 (D-05, READ-02): ONE get_stage_progress read feeds both the derived seven-key `stats`
    # dict (via _derive_stats -- replacing the removed FileRecord.state get_pipeline_stats) AND the
    # per-node DAG context (passed through to _build_dag_context so the heavy multi-count read happens
    # once). queue_progress_percent's numerator is the derived stats["analyzed"] (== analyze.done).
    stage_progress = await get_stage_progress(session)
    stats = _derive_stats(stage_progress)

    # Phase 27 D-05/D-06: agents for the Trigger Scan dropdown (non-revoked, ordered).
    # SER-01: exclude kind="compute" agents (Kueue/burst backends) — they are media-less
    # and cannot be scan targets, so they must never appear in the scan-picker.
    agents_stmt = select(Agent).where(Agent.revoked_at.is_(None), Agent.kind == "fileserver").order_by(Agent.name)
    agents = (await session.execute(agents_stmt)).scalars().all()

    # Phase 27 D-05 / UI-SPEC Component 4: last 10 non-LIVE ScanBatches with their
    # transient UI attrs (_agent_name / _elapsed_seconds / _seconds_since_progress /
    # _is_stalled) attached. PR5 gap-14: the query + attachment lives in the shared
    # build_recent_scans helper so the dashboard and the delete endpoint cannot
    # drift apart (a duplicated copy once crashed this table on a tz-aware row).
    recent_scans_rows = await build_recent_scans(session)

    # Phase 34: live queue depth so an in-flight run is visible on first load (not only
    # after the first 5s poll tick). get_queue_activity isolates its own failures and
    # degrades to zeros, so no try/except is added here. queue_progress_percent precomputes
    # the DB-derived "Processing" bar percent (guarded against divide-by-zero) server-side
    # for unit-testability; the card (Plan 03) and the button gating (Plan 04) consume these.
    activity = await get_queue_activity(app_state, session)
    queue_progress = queue_progress_percent(stats["analyzed"], activity["agent_busy"])

    # Phase 35 (35-04): per-DAG-node done/total/active reconciled from get_stage_progress
    # (DB-truth) + the maintained completed counters (backstop) + the queue activity. The
    # 35-05 canvas seeds these into $store.pipeline on the full-page render; here they ride
    # the dashboard context. _build_dag_context isolates its own counter-source failures.
    # stage_progress is passed through so _build_dag_context reuses the same read (no 2nd query).
    dag_ctx = await _build_dag_context(app_state, session, activity, stage_progress)

    # Phase 44 (44-04): the ANALYSIS_FAILED count ("gave up") and the STALLED count (the
    # heartbeat-watchdog-killed subset of it) -- the Analysis Health card's two buckets
    # (44-02 D-02). The Phase 44 STRAGGLER count (long-running in-flight process_file jobs,
    # a running-age GUESS) was removed by phaze-g84sk: phaze-w55w1's stall watchdog now turns
    # a genuinely wedged job into an ANALYSIS_FAILED row (reason="timeout") before any
    # dashboard poll could observe it as "still running", and a healthy long analysis is no
    # longer distinguishable from a stuck one by running age. Per operator follow-up, STALLED
    # replaces that guess with a PRECISE count derived from the same terminal record (no new
    # telemetry, see get_analysis_stalled_count). Both reads are degrade-safe (the Plan-02
    # services own the never-500 _safe_count degrade and return 0 on any DB error), so NO
    # try/except is added here -- same service-owns-degrade wiring idiom as the busy counts
    # above (175-178).
    analysis_failed_count = await get_analysis_failed_count(session)
    analysis_stalled_count = await get_analysis_stalled_count(session)

    # Phase 49 (49-02, D-05): the "Awaiting cloud" held-file count -- long files held back
    # because no compute agent was online when analysis routed them. get_awaiting_cloud_count
    # owns the never-500 _safe_count degrade (returns 0 on any DB error), so NO try/except is
    # added here -- same service-owns-degrade wiring idiom as the analysis-failed count above.
    awaiting_cloud_count = await get_awaiting_cloud_count(session)

    # Phase 50 (50-07, D-09; re-seamed phaze-zyoag): the two bounded cloud-window count cards --
    # "Staged (pushing)" (pre-submit / mid-transfer, per-backend-kind aware) and "Analyzing (cloud)"
    # (post-submit, in the cloud window -- includes a kueue row waiting on cluster quota, NOT just
    # "landed"). Both service reads own the never-500 _safe_count degrade (return 0 on any DB
    # error), so NO try/except here -- same service-owns-degrade idiom as awaiting_cloud_count.
    pushing_count = await get_pushing_count(session)
    analyzing_cloud_count = await get_pushed_count(session)

    # Phase 54 (54-04, D-06): the Inadmissible operator alert count -- cloud_job rows the reconcile
    # cron flagged as Inadmissible (a misconfigured LocalQueue/ClusterQueue, NOT a healthy quota
    # wait). get_inadmissible_count owns the never-500 _safe_count degrade (returns 0 on any DB
    # error), so NO try/except here -- same service-owns-degrade idiom as awaiting_cloud_count.
    inadmissible_count = await get_inadmissible_count(session)

    # Phase 55 (55-05, D-04, KROUTE-06): the four per-cloud_phase admission-state counts driving the
    # admission_state_card. get_cloud_phase_counts owns the never-500 _safe_count degrade per phase
    # (returns 0 on any DB error), so NO try/except here -- same service-owns-degrade idiom as
    # inadmissible_count. Seeded IDENTICALLY in pipeline_stats_partial() for the 5s OOB re-push.
    cloud_phase_counts = await get_cloud_phase_counts(session)

    # phaze-5462: the Analyze workspace no longer server-renders ANY file rows inline. It used to seed
    # `analyze_files` here from get_analyze_working_set, whose "active working set" branch was UNBOUNDED
    # (10,132 rows / 12.7 MB in prod -- ~180x the metadata tab, which renders a ~70 KB shell
    # with zero rows). phaze-zqvh bounded only the completions window and trusted a docstring assertion
    # for the other half. The list now loads exactly like its siblings: the workspace ships an empty
    # #analyze-files-view that hx-gets GET /pipeline/analyze-files on load, which serves the BOUNDED,
    # paged working set. No DB read for the file list happens on this path at all any more.

    # quick 260622-i0w: the scanned/deduped reconciliation for the Discovery DAG-node subtitle.
    # Server-rendered on full-page load ONLY (the canvas is never OOB-swapped on the 5s poll); this
    # explains the Discovery COUNT(files) vs agent scan total gap as dedup, not lost work. The service
    # owns the never-500 degrade (returns {scanned: None, deduped: None} on any error), so NO
    # try/except here — same wiring idiom as get_queue_activity / dag_ctx above.
    recon = await get_global_reconciliation(session)

    # Phase 71 (71-03, BEUI-01 / D-04): the N-lane grid snapshot -- one rank-ascending, secret-free dict
    # per registry backend {id, kind, rank, cap, in_flight, available, quota_wait, inadmissible, queued,
    # working, processed_24h, processed_lifetime} (the last four added phaze-5c6i2). Seeded IDENTICALLY
    # in pipeline_stats_partial() below so the WHOLE #analyze-lanes grid OOB-swaps on the SAME existing
    # 5s poll (no second loop, no new read endpoint -- Pitfall 2: N is dynamic, no per-lane store keys).
    # The snapshot helper owns the never-500 degrade (-> [] on any error), so NO try/except here --
    # same service-owns-degrade idiom as the cloud counts above. This SUPERSEDES the transitional single
    # non-local lane-kind key (retired); resolved_non_local_kind stays for the :811 callers.
    lanes = await get_backend_lane_snapshot(session, app_state)

    # phaze-5c6i2 (acceptance rule 2): the global "TOTAL QUEUED (analyze)" figure + its unrouted
    # remainder, derived from the SAME lane snapshot above (each lane's own ``queued``) plus the
    # Stage.ANALYZE not_started bucket. Seeded IDENTICALLY in pipeline_stats_partial() below so the
    # OOB-swapped card re-push agrees with this first-load render (the OOB swap contract). Degrade-safe
    # at the service layer, so NO router try/except -- mirrors the lanes wiring immediately above.
    analyze_queue_totals = await get_analyze_queue_totals(session, lanes)

    # phaze-6r39 (retires 56-02/D-05/D-06's cross-process Redis flag): the K8s LocalQueue-unreachable
    # amber alert, derived from the SAME lane snapshot above rather than a separate boot-time Redis key.
    # The old mechanism was written ONCE by the controller's startup probe with no TTL, so it never
    # cleared once connectivity was restored (the reported bug) and never fired at all for an outage
    # that began after boot (the silent, more dangerous half). derive_localqueue_unreachable is a pure
    # function over `lanes` (no I/O, cannot raise), so NO try/except here -- same
    # service-owns-degrade idiom as the cloud counts above. Seeded IDENTICALLY in pipeline_stats_partial()
    # below so the OOB-swapped card re-push agrees with this first-load render (the OOB swap contract).
    localqueue_unreachable = derive_localqueue_unreachable(lanes)

    # The Cloud Routing card's truthful hold-reason sub-caption -- derived from the SAME lane snapshot
    # above via the SAME gate order the drain (stage_cloud_window) checks, so the card can never claim
    # a blocker the drain itself would not hit next tick. derive_cloud_hold_reason is fully degrade-safe
    # (collapses to the neutral "held" copy on any error), so NO try/except here -- same
    # service-owns-degrade idiom as the cloud counts above. Seeded IDENTICALLY in pipeline_stats_partial()
    # below so the OOB-swapped card re-push agrees with this first-load render (the OOB swap contract).
    awaiting_hold_reason = await derive_cloud_hold_reason(session)

    return {
        **_shared_stats_context(
            stats=stats,
            analysis_failed_count=analysis_failed_count,
            analysis_stalled_count=analysis_stalled_count,
            awaiting_cloud_count=awaiting_cloud_count,
            awaiting_hold_reason=awaiting_hold_reason,
            pushing_count=pushing_count,
            analyzing_cloud_count=analyzing_cloud_count,
            inadmissible_count=inadmissible_count,
            localqueue_unreachable=localqueue_unreachable,
            cloud_phase_counts=cloud_phase_counts,
            # Phase 71 (71-03, BEUI-01 / D-04): the N-lane grid snapshot (seeded above, mirrored
            # identically in pipeline_stats_partial via the shared helper).
            lanes=lanes,
            # phaze-5c6i2 (acceptance rule 2): the global TOTAL QUEUED (analyze) figure + its unrouted
            # remainder (seeded above, mirrored identically in pipeline_stats_partial).
            analyze_queue_totals=analyze_queue_totals,
            activity=activity,
            dag_ctx=dag_ctx,
            queue_progress=queue_progress,
        ),
        "current_page": "pipeline",
        "agents": agents,
        "recent_scans": recent_scans_rows,
        "reconcile_scanned": recon["scanned"],
        "reconcile_deduped": recon["deduped"],
    }


@router.get("/pipeline/", response_class=HTMLResponse)
async def dashboard() -> RedirectResponse:
    """Redirect the legacy ``/pipeline/`` route to the v7.0 shell root.

    CUT-02 (Phase 62 / D-03b): ``/pipeline/`` was renamed to the shell root ``/`` in Phase
    57 (SHELL-01). The shell's Analyze default renders the live lane-card workspace
    (``/s/analyze``) and polls ``/pipeline/stats``; nothing hx-gets ``/pipeline/`` any more,
    so the legacy ``dashboard.html`` render path -- the ONE genuinely-dead HX branch in the
    cutover -- is removed and ``/pipeline/`` becomes a pure 302 redirect. The route stays
    registered so old bookmarks keep resolving into the shell. The DAG dashboard *context*
    still lives in :func:`build_dashboard_context`, which the shell Analyze render consumes.
    """
    return RedirectResponse(url="/", status_code=302)


@router.get("/pipeline/stats", response_class=HTMLResponse)
async def pipeline_stats_partial(
    request: Request,
    lane: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the stats bar partial for HTMX polling refresh.

    Phase 88 (88-01, DRILL-03 / D-02): the single persistent ``#pipeline-stats`` poll carries the
    pushed ``?lane=`` via ``hx-vals`` (shell.html), so this hot 5s tick re-emits the selected-lane
    highlight (aria-current + ring) on the matching card through the OOB ``_analyze_lanes`` grid.
    ``lane`` is resolved by lookup-in-known-set against the snapshot (T-88-01): an unknown/absent id
    resolves to ``None`` and highlights nothing — never a 422/500 into the poll.
    """
    # Phase 82 (D-05, READ-02): ONE get_stage_progress read feeds the derived seven-key `stats` dict
    # (via _derive_stats -- the removed FileRecord.state get_pipeline_stats) AND the per-node DAG
    # context below (passed through so the heavy multi-count read runs once on the hot 5s poll).
    stage_progress = await get_stage_progress(session)
    stats = _derive_stats(stage_progress)

    # Phase 95 (CONSOLE-04, DENORM-01 revisit): the ~12 reads below used to run as SERIAL awaits on
    # the shared request `session` (measured over the ~1s soft budget, D-07 -- see
    # .planning/milestones/2026.7.7-phases/95-analyze-view-browser-slowdown/). They are mutually
    # independent -- none consumes another's RESOLVED VALUE (derive_cloud_hold_reason re-derives its
    # own lane snapshot rather than reading the `lanes` result below) -- so they now fan out
    # CONCURRENTLY via asyncio.gather, mirroring the Phase 92 get_stage_progress pattern exactly:
    # each read runs in its OWN AsyncSession via _read_in_own_session, bounded by the SAME
    # _stats_fanout() cap (process-global cap-4, shared with every OTHER concurrently in-flight poll
    # -- phaze-28wi; the test suite's _route_stats_fanout fixture overrides _STATS_FANOUT to
    # Semaphore(1) and routes phaze.database.async_session onto the per-test connection, so this
    # reuses that EXISTING test-isolation seam with no new fixture).
    #
    # activity feeds queue_progress below AND is a required (if internally-unused-by-design --
    # see _build_dag_context's docstring) positional argument to _build_dag_context: a TRUE
    # dependency by signature, so _build_dag_context stays a sequential await AFTER this gather,
    # once activity is a resolved value rather than a pending coroutine. It reuses the shared
    # request `session` directly (unchanged from before this refactor) -- safe because nothing else
    # touches that session concurrently once the fan-out (which reads through its own sessions) is
    # under way.
    fanout = _stats_fanout()
    (
        activity,
        analysis_failed_count,
        analysis_stalled_count,
        awaiting_cloud_count,
        pushing_count,
        analyzing_cloud_count,
        inadmissible_count,
        cloud_phase_counts,
        lanes,
        awaiting_hold_reason,
        analysis_live,
        analysis_activity,
        # asyncio.gather with >6 awaitables of mixed return types collapses to list[object] under
        # mypy (mirrors the identical cast in services/pipeline.py:get_stage_progress) -- pin the
        # exact per-read tuple shape with a single cast.
    ) = cast(
        "tuple[dict[str, int], int, int, int, int, int, int, dict[str, int], list[dict[str, Any]], str, int | None, dict[str, int | None]]",
        await asyncio.gather(
            # Phase 34: surface live queue depth through the EXISTING 5s poll (no new loop).
            # get_queue_activity degrades to zeros on a Redis hiccup / missing app.state, so the
            # poll can never 500. queue_progress_percent (below) precomputes the guarded "Processing"
            # bar percent server-side; the OOB store-write nodes in stats_bar.html push agent_busy /
            # controller_busy into $store.pipeline on each tick to drive the Plan 04 button gating.
            _read_in_own_session(
                fanout,
                lambda s: get_queue_activity(request.app.state, s),
                {"agent_queued": 0, "agent_active": 0, "controller_queued": 0, "controller_active": 0, "agent_busy": 0, "controller_busy": 0},
            ),
            # Phase 44 (44-04): the same ANALYSIS_FAILED + STALLED buckets the dashboard seeds,
            # re-pushed on every 5s poll so the Analysis Health card stays live. The STRAGGLER
            # bucket this used to ride alongside was removed by phaze-g84sk and replaced with the
            # precise STALLED subset (see services/pipeline.py). Degrade-safe at the service layer
            # (44-02), so NO router try/except -- mirrors the dashboard() wiring.
            _read_in_own_session(fanout, lambda s: get_analysis_failed_count(s), 0),
            _read_in_own_session(fanout, lambda s: get_analysis_stalled_count(s), 0),
            # Phase 49 (49-02, D-05): the same AWAITING_CLOUD held count the dashboard seeds, re-pushed
            # on every 5s poll so the awaiting_cloud_card stays live via its OOB swap. Degrade-safe at the
            # service layer (Plan 01), so NO router try/except -- mirrors the analysis-failed wiring.
            _read_in_own_session(fanout, lambda s: get_awaiting_cloud_count(s), 0),
            # Phase 50 (50-07, D-09): the same PUSHING/PUSHED window counts the dashboard seeds, re-pushed
            # on every 5s poll so the staged_pushing_card / analyzing_cloud_card stay live via their OOB
            # swaps. Degrade-safe at the service layer, so NO router try/except -- mirrors the awaiting wiring.
            _read_in_own_session(fanout, lambda s: get_pushing_count(s), 0),
            _read_in_own_session(fanout, lambda s: get_pushed_count(s), 0),
            # Phase 54 (54-04, D-06): the same Inadmissible count the dashboard seeds, re-pushed on every 5s
            # poll so the inadmissible_card stays live via its OOB swap. Degrade-safe at the service layer,
            # so NO router try/except -- mirrors the awaiting_cloud_count wiring.
            _read_in_own_session(fanout, lambda s: get_inadmissible_count(s), 0),
            # Phase 55 (55-05, D-04, KROUTE-06): the same four per-cloud_phase admission counts the dashboard
            # seeds, re-pushed on every 5s poll so the admission_state_card stays live via its OOB swap.
            # Degrade-safe at the service layer (per-phase _safe_count), so NO router try/except -- mirrors
            # the inadmissible_count wiring.
            _read_in_own_session(fanout, lambda s: get_cloud_phase_counts(s), {"queued_behind_quota": 0, "admitted": 0, "running": 0, "finished": 0}),
            # Phase 71 (71-03, BEUI-01 / D-04): the SAME N-lane snapshot the dashboard seeds, re-pushed on every
            # 5s poll so the WHOLE #analyze-lanes grid OOB-swaps as a unit (stats_bar.html includes _analyze_lanes
            # with oob=True inside the oob_counts gate). Seeded IDENTICALLY to build_dashboard_context (degrade-safe
            # -> [], never 500) -- one existing poll, no second loop, no new read endpoint.
            _read_in_own_session(fanout, lambda s: get_backend_lane_snapshot(s, request.app.state), cast("list[dict[str, Any]]", [])),
            # The SAME hold-reason derivation build_dashboard_context seeds on first load, re-pushed on every 5s
            # poll so the awaiting_cloud_card sub-caption stays live via its OOB swap (the OOB swap contract:
            # both render paths must agree). Degrade-safe at the service layer, so NO router try/except -- mirrors
            # the lanes wiring immediately above. "held" mirrors services.backends._HOLD_REASON_DEGRADED, the
            # SAME neutral no-causal-claim copy that function's own try/except already degrades to.
            _read_in_own_session(fanout, lambda s: derive_cloud_hold_reason(s), "held"),
            _read_in_own_session(fanout, lambda s: get_analysis_live_count(s, request.app.state), None),
            _read_in_own_session(
                fanout,
                get_analysis_activity_counts,
                cast("dict[str, int | None]", {"today": None, "lifetime": None}),
            ),
        ),
    )
    # phaze-6r39: the same live-lane derivation build_dashboard_context seeds on first load, re-pushed
    # on every 5s poll so the localqueue_card stays live via its OOB swap (the OOB swap contract: both
    # render paths must agree). Pure function over the `lanes` snapshot just resolved above -- no I/O,
    # cannot raise -- so NO router try/except, mirroring the lanes wiring immediately above it.
    localqueue_unreachable = derive_localqueue_unreachable(lanes)
    # phaze-5c6i2 (acceptance rule 2): the same TOTAL QUEUED (analyze) derivation build_dashboard_context
    # seeds on first load, re-pushed on every 5s poll so the total-queued card stays live via its OOB
    # swap (the OOB swap contract: both render paths must agree). Depends on the JUST-resolved `lanes`
    # value, so it runs sequentially here rather than inside the gather above.
    analyze_queue_totals = await get_analyze_queue_totals(session, lanes)
    queue_progress = queue_progress_percent(stats["analyzed"], activity["agent_busy"])
    # Phase 35 (35-04): same per-node reconcile as dashboard(), re-pushed on every 5s
    # poll via the OOB x-init seeds in stats_bar.html (gated behind oob_counts). The store
    # write keeps the 35-05 DAG bindings live without re-rendering the canvas or buttons.
    # stage_progress is passed through so _build_dag_context reuses the same read (no 2nd query).
    dag_ctx = await _build_dag_context(request.app.state, session, activity, stage_progress)
    # D-02 poll survival: resolve the pushed ?lane= by lookup-in-known-set (T-88-01) so the OOB
    # _analyze_lanes grid re-emits the selected ring only for a real, currently-rendered lane.
    selected_lane = lane if any(one.get("id") == lane for one in lanes) else None
    # Phase 95 (phaze-zqvh.3): the content hash of the grid's render inputs (lanes + selected highlight).
    # Emitted as data-lanes-hash so the client htmx:oobBeforeSwap hook SKIPS this OOB grid swap when the
    # state is byte-identical to what is already mounted -- bounding per-tick destroy-and-recreate churn
    # on a long-lived idle tab. Computed over the SAME inputs the initial render hashes, so the first tick
    # after an unchanged load is already a no-op swap.
    lanes_hash = analyze_lanes_content_hash(lanes, selected_lane)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/stats_bar.html",
        # oob_counts=True emits the hx-swap-oob "files ready" paragraphs ONLY on
        # this poll response. The dashboard full-page include omits the flag, so
        # the OOB block is skipped at initial load (where htmx would not honor
        # hx-swap-oob and the ids would collide with the DAG canvas seeds).
        context={
            "request": request,
            "oob_counts": True,
            **_shared_stats_context(
                stats=stats,
                analysis_failed_count=analysis_failed_count,
                analysis_stalled_count=analysis_stalled_count,
                awaiting_cloud_count=awaiting_cloud_count,
                awaiting_hold_reason=awaiting_hold_reason,
                pushing_count=pushing_count,
                analyzing_cloud_count=analyzing_cloud_count,
                inadmissible_count=inadmissible_count,
                localqueue_unreachable=localqueue_unreachable,
                cloud_phase_counts=cloud_phase_counts,
                lanes=lanes,
                analyze_queue_totals=analyze_queue_totals,
                activity=activity,
                dag_ctx=dag_ctx,
                queue_progress=queue_progress,
            ),
            "selected_lane": selected_lane,
            "lanes_hash": lanes_hash,
            "summary_recent_live": analysis_live,
            "summary_recent_today": analysis_activity["today"],
            "summary_recent_lifetime": analysis_activity["lifetime"],
        },
    )
