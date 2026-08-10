"""Pipeline orchestration router -- trigger endpoints and dashboard UI."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

# The suppression below is deliberate (runtime import, NOT type-only): this module carries
# `from __future__ import annotations`,
# so ruff offers to move `uuid` into the TYPE_CHECKING block. FastAPI resolves route annotations at
# RUNTIME via get_type_hints, so a `file_id: uuid.UUID` path param would raise NameError on import.
# (Before phaze-0jpe this import also had a plain runtime use -- `uuid.uuid4()` for the scan_live_set
# nonce -- which masked the rule; the annotation requirement is the real reason it must stay here.)
import uuid  # noqa: TC003

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import ARRAY, DateTime, String, bindparam, delete, exists, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import UUID as PGUUID, insert as pg_insert
from sqlalchemy.exc import IntegrityError
import structlog

from phaze.config import settings
from phaze.database import async_session, get_session
from phaze.enums.stage import ELIGIBILITY_DAG, ELIGIBLE_AFTER_FAILURE, Stage, Status, eligible, resolve_status
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.models.tracklist import Tracklist
from phaze.routers.column_sort import SortableColumn, SortContract
from phaze.routers.pipeline_scans import build_recent_scans
from phaze.routers.request_guards import MALFORMED_PAYLOAD_STATUS
from phaze.routers.response_shape import wants_fragment
from phaze.schemas.agent_tasks import ExtractMetadataPayload
from phaze.services import enqueue_router
from phaze.services.agent_liveness import derive_compute_lane_identities
from phaze.services.analysis_enqueue import classify_process_file_collision, enqueue_process_file, process_file_job_key
from phaze.services.backends import (
    LANE_RECENT_N,
    derive_cloud_hold_reason,
    derive_localqueue_unreachable,
    get_backend_lane_snapshot,
    get_lane_queue_depths,
    get_lane_recent_completions,
    hold_awaiting_cloud,
)
from phaze.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MIN_PAGE_SIZE
from phaze.services.pg_text import sanitize_pg_text
from phaze.services.pipeline import (
    ANALYZE_FILTERS,
    _read_in_own_session,
    _stats_fanout,
    analyze_lanes_content_hash,
    count_active_agents,
    count_backfill_candidates,
    get_analysis_failed_count,
    get_analysis_failed_files,
    get_analyze_files_page,
    get_analyze_working_set,
    get_awaiting_cloud_count,
    get_backfill_candidates,
    get_cached_stage_orphan_counts,
    get_cloud_phase_counts,
    get_discovered_files_with_duration,
    get_file_stage_buckets,
    get_files_page,
    get_global_reconciliation,
    get_inadmissible_count,
    get_live_job_keys,
    get_match_busy_count,
    get_match_pending_tracklists,
    get_metadata_failed_files,
    get_metadata_pending_files,
    get_pending_files_page,
    get_proposal_pending_batches,
    get_pushed_count,
    get_pushing_count,
    get_queue_activity,
    get_stage_busy_counts,
    get_stage_controls,
    get_stage_progress,
    get_straggler_count,
    get_tracklist_sets_page,
    queue_progress_percent,
)
from phaze.services.pipeline_counters import read_counters
from phaze.services.route_control import get_route_control
from phaze.services.stage_status import failed_clause, stage_status_sort_case
from phaze.services.tracklist_candidate_queue import DAILY_LOOKUP_CEILING
from phaze.services.tracklist_priority import flag_file_for_lookup, get_file_tracklist_review, unflag_file
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION
from phaze.tasks.reenqueue import recover_orphaned_work
from phaze.tasks.tracklist import refresh_tracklists
from phaze.tasks.tracklist_drain import tracklist_drain_status
from phaze.web.static import static_asset_url


logger = structlog.get_logger(__name__)

_NO_ACTIVE_AGENT_MESSAGE = "No active agent available — start an agent worker and retry"

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

    dag: dict[str, int] = {
        "metadataDone": done("metadata"),
        "metadataTotal": total("metadata"),
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
    # get_stage_busy_counts owns the never-500 degrade (all-zeros on any DB error), so NO try/except
    # is added here; these ints ride the same dag.items() seed + OOB loop with no stats_bar.html edit.
    busy = await get_stage_busy_counts(session)
    dag["metadataBusy"] = int(busy["metadata"])
    dag["analyzeBusy"] = int(busy["analyze"])

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


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# phaze-315t: fingerprinted, cache-forever static asset URLs. `pipeline/files.html` extends
# `base.html`, which carries the app.css link + favicon set.
templates.env.globals["static_url"] = static_asset_url
router = APIRouter(tags=["pipeline"])

# Hold references to background enqueue tasks to prevent GC (same pattern as scan.py). Typed
# `Task[Any]` (not `Task[None]`) because `_enqueue_analysis_jobs` returns `list[uuid.UUID]`
# (phaze-4ter) while every other producer here returns `None`.
_background_tasks: set[asyncio.Task[Any]] = set()


def _analysis_file_ids_scope(file_ids: list[uuid.UUID], name: str) -> Any:
    """``AnalysisResult.file_id = ANY(:name)`` -- ONE Postgres array bind, never a bare ``.in_(file_ids)``.

    phaze-r7j9: mirrors ``tasks/reenqueue.py::_fids_scope``, the repo's established idiom for this
    exact problem. A bare ``.in_(file_ids)`` expands to one bind parameter per element, and asyncpg's
    Bind message caps a statement at 32767 parameters -- this module's own docstrings cite ~44.5K-file
    failure sets as the incident scale the bulk retry / cloud-backfill paths were hardened for, well
    past that ceiling. Binding the whole id list as ONE ``uuid[]`` array parameter sidesteps the
    ceiling regardless of list size, and is a single (cheaper) round trip besides.
    """
    return AnalysisResult.file_id == func.any(bindparam(name, value=file_ids, type_=ARRAY(PGUUID(as_uuid=True))))


def _ledger_keys_scope(keys: list[str], name: str) -> Any:
    """``SchedulingLedger.key = ANY(:name)`` -- the same array-bind idiom, string keys (phaze-r7j9)."""
    return SchedulingLedger.key == func.any(bindparam(name, value=keys, type_=ARRAY(String())))


def _scheduling_ledger_cas_delete_stmt(observed_rows: Sequence[Any]) -> Any:
    """CAS-guarded ``DELETE ... WHERE (key, enqueued_at) IN (observed_rows)`` at CONSTANT bind cost.

    phaze-krzz5: a bare ``tuple_(SchedulingLedger.key, SchedulingLedger.enqueued_at).in_(observed_rows)``
    renders a composite ``(key, enqueued_at) IN ((p1, p2), (p3, p4), ...)`` -- SQLAlchemy expands
    each row of a composite IN-list individually, so this is TWO bind parameters per row, hitting
    asyncpg's 32767-parameter cap at roughly HALF the row count the sibling single-column
    ``_analysis_file_ids_scope`` / ``_ledger_keys_scope`` array-bind idiom tolerates (the phaze-r7j9
    fix those two apply, one column short of covering this composite-key DELETE).

    Binds the observed rows as TWO parallel Postgres arrays and matches them via ``unnest`` run in
    lockstep (multiple set-returning functions in one SELECT list iterate together in Postgres),
    extending the same array-bind idiom to a composite key: exactly 2 bind parameters regardless of
    how many rows were observed, same as the single-column helpers above.
    """
    observed_keys = [key for key, _ in observed_rows]
    observed_enqueued_ats = [enqueued_at for _, enqueued_at in observed_rows]
    observed_pairs = select(
        func.unnest(bindparam("cas_delete_keys", value=observed_keys, type_=ARRAY(String()))).label("key"),
        func.unnest(bindparam("cas_delete_enqueued_ats", value=observed_enqueued_ats, type_=ARRAY(DateTime(timezone=True)))).label("enqueued_at"),
    ).subquery()
    return delete(SchedulingLedger).where(
        tuple_(SchedulingLedger.key, SchedulingLedger.enqueued_at).in_(select(observed_pairs.c.key, observed_pairs.c.enqueued_at)),
    )


async def _enqueue_analysis_jobs(queue: Any, files: list[FileRecord], agent_id: str, models_path: str) -> list[uuid.UUID]:
    """Background coroutine to enqueue process_file jobs for a list of files.

    Delegates each enqueue to the FastAPI-free shared producer
    ``services.analysis_enqueue.enqueue_process_file``. That helper owns the
    deterministic job key (``process_file:<file_id>``), the complete 5-field
    ``ProcessFilePayload``, and the job policy (``timeout=7200`` / ``retries=2``)
    -- so this dashboard path and the Wave-2 agent-reboot re-enqueue path cannot
    drift: both emit the IDENTICAL key, letting SAQ's per-queue deterministic-key
    dedup collapse a repeat enqueue of an in-flight file to a no-op (32-RESEARCH §Q4).

    ``files`` attributes (``id`` / ``original_path`` / ``file_type``) are already
    loaded by ``get_files_by_state`` and the request never commits, so reading them
    here (after the request session may have closed) does not trigger a lazy load.

    All process_file trigger endpoints (``/api/v1/analyze`` + the HTMX
    ``/pipeline/analyze``) funnel through this one helper, so the key + policy are
    applied identically at every enqueue site.

    phaze-4ter: each file's enqueue is now individually contained -- a raised exception
    (e.g. a transient queue-pool error) is logged and the file's id is collected into the
    returned list instead of propagating, so ONE failure can no longer abort every
    remaining enqueue in the group. Returns the ids that failed to enqueue (empty when every
    file succeeded) so a caller that already cleared a durable failure marker BEFORE
    backgrounding this call (:func:`retry_analysis_failed`) can restore it for exactly the
    files that never got a replacement job, instead of the marker and the job both vanishing.

    phaze-ewen: a ``None`` return (deterministic-key collision) is not logged as anything --
    including the case where the key is held by a DEAD job (aborting/failed/stuck), which means
    this file was silently OMITTED from a bulk run the dashboard reports as "N enqueued". This
    does not change behavior (still no retry, and a blocked file is deliberately NOT added to
    ``failed_ids`` -- restoring its ``failed_at`` would be wrong, because the file's marker
    state is whatever the dead key-holder left, and un-wedging the key is the aborting-reaper's
    job, not this loop's), just makes a blocked file visible in logs.
    """
    failed_ids: list[uuid.UUID] = []
    for f in files:
        try:
            job = await enqueue_process_file(queue, f, agent_id, models_path)
        except Exception:
            logger.exception("enqueue_analysis_jobs: failed to enqueue process_file job", file_id=str(f.id))
            failed_ids.append(f.id)
            continue
        if job is None and classify_process_file_collision(await queue.job(process_file_job_key(f.id))) == "blocked":
            logger.warning(
                "_enqueue_analysis_jobs: deterministic key held by a dead job -- file omitted from this run",
                file_id=str(f.id),
                key=process_file_job_key(f.id),
            )
    return failed_ids


async def _retry_analysis_group(queue: Any, group: list[FileRecord], agent_id: str, models_path: str) -> None:
    """Background: enqueue one routed group's ``process_file`` jobs for the bulk retry (phaze-4ter).

    :func:`retry_analysis_failed` clears + commits ``analysis.failed_at`` for the WHOLE routed set
    BEFORE backgrounding this call (RESEARCH Pitfall 3 -- the red count must drop regardless of the
    enqueue outcome). That is safe only if a per-file enqueue failure can never be lost: previously
    the background task's done-callback was a bare ``_background_tasks.discard`` that never called
    ``task.result()``, so the FIRST raised enqueue both aborted every remaining file in the group
    (``_enqueue_analysis_jobs`` had no per-file containment) and vanished without a log correlated to
    this request -- the marker was gone, no job was ever enqueued, and nothing recorded that the file
    had ever failed.

    ``_enqueue_analysis_jobs`` now contains each enqueue individually and returns the ids that
    failed; this wrapper re-stamps ``failed_at`` for exactly those ids (a fresh ``async_session`` --
    the request session that cleared the marker is closed by the time this background task runs), so
    a transient enqueue error degrades to "still shows failed, retryable" instead of a silent,
    permanent loss of both the job and the failure record. Any exception this coroutine itself raises
    (e.g. the restore write failing too) is caught and logged here rather than left for a bare
    discard callback to swallow.
    """
    try:
        failed_ids = await _enqueue_analysis_jobs(queue, group, agent_id, models_path)
        if not failed_ids:
            return
        # Deferred import (services/pipeline.py::_read_in_own_session precedent): re-reads
        # `phaze.database.async_session` at CALL time rather than binding this module's
        # import-time reference, so a test that monkeypatches the source attribute onto a
        # per-test connection (`tests/conftest.py::_route_stats_fanout`) is honored here too.
        from phaze.database import async_session  # noqa: PLC0415

        async with async_session() as restore_session:
            # phaze-6ib1n: guard on `analysis_completed_at IS NULL`, mirroring
            # `report_analysis_failed`'s own conflict predicate (routers/agent_analysis.py) and its
            # documented reason -- "guard the failure stamp so it NEVER downgrades a row that already
            # reads DONE". Without it, a file that raced this loop (its own enqueue landed and
            # `put_analysis` stamped `analysis_completed_at` WHILE this background task was still
            # grinding through the rest of the group) trips the `analysis_completed_xor_failed` CHECK.
            # That is a STATEMENT-level violation: this multi-row UPDATE aborts as a whole, the `except`
            # below only logs, and every OTHER id in `failed_ids` -- whose enqueue genuinely never
            # happened -- permanently loses its failure marker with no replacement job. A completed row
            # needs no restore (it is done, not failed), so excluding it here is correct independent of
            # the crash, and it means one raced id can never void the restore for the whole group.
            await restore_session.execute(
                update(AnalysisResult)
                .where(_analysis_file_ids_scope(failed_ids, "restore_ids"), AnalysisResult.analysis_completed_at.is_(None))
                .values(failed_at=func.now(), error_message="retry_analysis_failed: enqueue error, see agent logs (phaze-4ter)"),
            )
            await restore_session.commit()
        logger.error(
            "retry_analysis_failed: restored failed_at marker after enqueue error",
            count=len(failed_ids),
            file_ids=[str(fid) for fid in failed_ids],
        )
    except Exception:
        # phaze-4ter: this coroutine runs detached (asyncio.create_task + a `_background_tasks.discard`
        # done-callback, which never calls `task.result()`) -- an exception escaping here would
        # otherwise surface only via asyncio's uncorrelated "Task exception was never retrieved" GC-time
        # log, never structlog, never tied to this request. Contain and log explicitly instead.
        logger.exception("retry_analysis_failed: background retry group failed")


async def _route_discovered_by_duration(
    app_state: Any,
    session: AsyncSession,
    files_with_duration: list[tuple[FileRecord, float | None]],
    threshold_sec: int,
    cloud_enabled: bool,
    models_path: str,
) -> dict[str, int]:
    """Route each DISCOVERED file to a queue by its duration (Phase 49 seam, reshaped in Phase 50).

    The single per-file routing decision shared by the "Run Analysis" trigger (this module)
    and the Plan-03 backfill producer, so the two paths cannot drift. phaze-c9w9: local routing
    is per-OWNER -- the short/null candidates are grouped by ``FileRecord.agent_id`` via
    :func:`enqueue_router.resolve_queues_for_owned_files` (never one most-recently-seen pick for
    the whole set, which misrouted every other agent's files); each group's queue comes from
    ``app_state.task_router.queue_for`` (the Phase-30 invariant -- never the consumer-less
    default queue).

    Per file, on the captured ``(file, duration)`` tuples:

    - ``duration is None`` or ``< threshold_sec`` AND the file's OWNING fileserver agent is
      online -> enqueue ``process_file`` onto that owner's queue (``local``).
    - ``duration is None`` or ``< threshold_sec`` AND the owning agent is offline -> count as
      ``skipped`` (cannot route locally) -- NO enqueue, NO state change, the run continues.
    - ``duration >= threshold_sec`` -> ALWAYS set the row's state to ``AWAITING_CLOUD``
      (``awaiting``), regardless of whether a compute agent is online (Phase 50 CLOUDPIPE-01).

    Phase 50 reshape (T-50-bypass): there is NO direct-to-compute enqueue here any more. A long
    file is ALWAYS HELD in AWAITING_CLOUD; the bounded ``stage_cloud_window`` controller cron is
    the SINGLE entry to the compute pipeline (it tops the ≤N window up to ``cloud_max_in_flight``
    by staging ``push_file`` for the oldest held files). Holding in exactly one place is what
    makes the window unbypassable -- a 144-file backlog can never blow up the compute scratch
    disk. A held long file is NEVER silently analyzed locally (the load-bearing CLOUDROUTE-02
    safety invariant, T-49-03).

    The held AWAITING_CLOUD UPDATEs are committed with an explicit ``await session.commit()``
    BEFORE the enqueues are backgrounded (``get_session`` does NOT auto-commit -- RESEARCH
    Pitfall 3).

    Returns ``{"local": N, "cloud": 0, "awaiting": K, "skipped": S, "no_active_agent": 0|1}``;
    ``cloud`` is always 0 (no direct compute enqueue remains). ``no_active_agent`` is 1 when
    local candidates exist but NO owning fileserver agent is online (nothing can route locally):
    the caller then surfaces the no-active-agent response, whose template still reports any HELD
    long files (WR-01) via the ``awaiting`` count -- a held long file is real, durable work the
    staging cron will drain.
    """
    local_candidates: list[FileRecord] = []
    skipped = 0
    held = 0
    # phaze-e8kv: tallied separately from `skipped` -- the local-routing block below UNCONDITIONALLY
    # reassigns `skipped` (never accumulates into it), so a count folded into `skipped` inside this
    # loop would be silently discarded before the function returns.
    deleted_before_hold = 0

    for file, duration in files_with_duration:
        # Phase 51 (D-02): when cloud-burst is OFF nothing is "long" -- every file falls to the
        # local branch, so no row is ever held in AWAITING_CLOUD and the cloud pipeline stays dormant.
        is_long = cloud_enabled and duration is not None and duration >= threshold_sec
        if is_long:
            # Phase 50 (CLOUDPIPE-01): ALWAYS hold -- no direct-to-compute path. The bounded
            # stage_cloud_window cron is the single, unbypassable entry to the compute pipeline.
            # Phase 83 (D-01): hold via the shared writer so every go-forward hold carries its
            # cloud_job(status='awaiting', attempts=0) sidecar row -- closing the missing-writer gap
            # that violated the hard shadow invariant AWAITING_CLOUD => cloud_job(status='awaiting')
            # on every held file since migration 032. The helper dual-writes file.state (D-00c) and
            # NEVER commits; the existing post-loop commit below is the hold's own commit boundary.
            #
            # phaze-e8kv: hold_awaiting_cloud's INSERT carries a NOT NULL FK to files.id, and this
            # candidate list was read well before this loop reaches it (a loop over potentially
            # hundreds of files) -- a concurrent delete_scan cascade (services/scan_deletion.py) can
            # remove the FileRecord in that window and FK-violate the INSERT. Mirror force_skip_stage's
            # two-layer discipline (rule 4/5): run the hold inside a SAVEPOINT and treat a caught
            # IntegrityError as "file concurrently deleted -- skip and count it", so one vanished row
            # costs one skipped file instead of an unhandled 500 aborting the whole run (and every
            # earlier hold in the same transaction along with it).
            try:
                async with session.begin_nested():
                    await hold_awaiting_cloud(session, file)
            except IntegrityError:
                logger.info(
                    "route_discovered_by_duration: file deleted before hold could commit; skipping",
                    file_id=str(file.id),
                )
                deleted_before_hold += 1
                continue
            held += 1
        else:
            local_candidates.append(file)

    # Commit the AWAITING_CLOUD held-state UPDATEs BEFORE backgrounding the enqueues
    # (get_session does not auto-commit -- RESEARCH Pitfall 3).
    if held:
        await session.commit()

    # phaze-c9w9: group the local candidates by their OWNING agent and route each group to that
    # agent's queue. A candidate whose owner is offline is SKIPPED (never rerouted to a different
    # agent's mount); NoActiveAgentError here means no candidate's owner is live -- kept as the
    # no_active_agent=1 signal.
    routed_groups: list[tuple[enqueue_router.RoutedQueue, list[FileRecord]]] = []
    no_active_agent = False
    if local_candidates:
        try:
            routed_groups, skipped_files = await enqueue_router.resolve_queues_for_owned_files("process_file", app_state, session, local_candidates)
        except enqueue_router.NoActiveAgentError:
            skipped = len(local_candidates)
            no_active_agent = True
        else:
            skipped = len(skipped_files)
    else:
        # No local candidates (an all-long / empty run): preserve the pre-c9w9 signal shape --
        # no_active_agent=1 iff NO fileserver is online at all, so the WR-01 held-count
        # no-active-agent fragment still renders on a cold-boot "Run Analysis" of long files.
        try:
            await enqueue_router.select_active_agent(session, kind="fileserver")
        except enqueue_router.NoActiveAgentError:
            no_active_agent = True

    local = 0
    for routed, group in routed_groups:
        local += len(group)
        local_task = asyncio.create_task(_enqueue_analysis_jobs(routed.queue, group, cast("str", routed.agent_id), models_path))
        _background_tasks.add(local_task)
        local_task.add_done_callback(_background_tasks.discard)

    return {
        "local": local,
        "cloud": 0,
        "awaiting": held,
        "skipped": skipped + deleted_before_hold,
        "no_active_agent": int(no_active_agent),
    }


async def _enqueue_proposal_jobs(queue: Any, batches: list[list[str]]) -> None:
    """Background coroutine to enqueue generate_proposals jobs for batched file IDs."""
    for idx, batch in enumerate(batches):
        await queue.enqueue("generate_proposals", file_ids=batch, batch_index=idx)


@router.post("/api/v1/analyze")
async def trigger_analysis(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue process_file jobs for all DISCOVERED files, routed per-file by duration (Phase 49
    D-06/D-11/D-12; Phase 50 CLOUDPIPE-01 reshape).

    Short/null-duration files route to the FILESERVER queue exactly as before. Long
    (``>= cloud_route_threshold_sec``) files are ALWAYS held in ``AWAITING_CLOUD`` -- there is no
    direct-to-compute enqueue here any more (see :func:`_route_discovered_by_duration`); the
    bounded ``stage_cloud_window`` controller cron is the sole entry to the compute pipeline.
    Short/null files with no fileserver agent online are reported ``skipped`` without aborting the
    run. One SAQ job per locally-routed file; the enqueues run in a background task (via the shared
    router helper) to avoid HTTP timeout on large file counts. Returns the split counts (``cloud``
    is always 0). The no-active-agent message is returned when NO fileserver agent is online
    (nothing can route locally) -- any long files are still committed to ``AWAITING_CLOUD``
    regardless.
    """
    files_with_duration = await get_discovered_files_with_duration(session)
    if not files_with_duration:
        return {"enqueued": 0, "message": "No files in DISCOVERED state"}

    # Phase 71 (BEUI-02, D-08): fold the force-local override into the routing flag. The effective
    # cloud_enabled is ``registry cloud_enabled AND NOT force_local`` -- when forced, nothing is "long"
    # so every file routes local (byte-identical to an all-local registry), and no new row is held in
    # AWAITING_CLOUD. select_backend stays pure (untouched); the flag is read only here at the caller.
    effective_cloud_enabled = settings.cloud_enabled and not await get_route_control(session)
    counts = await _route_discovered_by_duration(
        request.app.state,
        session,
        files_with_duration,
        settings.cloud_route_threshold_sec,
        effective_cloud_enabled,
        settings.models_path,
    )

    if counts["no_active_agent"]:
        # Both kinds absent -- nothing was routable. Any long files were still committed to
        # AWAITING_CLOUD (surfaced via the count card); short/null files were skipped.
        return {
            "enqueued": 0,
            "local": 0,
            "cloud": 0,
            "awaiting_cloud": counts["awaiting"],
            "skipped": counts["skipped"],
            "message": _NO_ACTIVE_AGENT_MESSAGE,
        }

    enqueued = counts["local"] + counts["cloud"]
    return {
        "enqueued": enqueued,
        "local": counts["local"],
        "cloud": counts["cloud"],
        "awaiting_cloud": counts["awaiting"],
        "skipped": counts["skipped"],
        "message": (
            f"Enqueued {counts['local']} local, {counts['cloud']} cloud; "
            f"{counts['awaiting']} awaiting cloud, {counts['skipped']} skipped (no local agent)"
        ),
    }


@router.post("/api/v1/proposals/generate")
async def trigger_proposals(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue generate_proposals jobs for files with both metadata and analysis (per D-02 convergence gate).

    Uses settings.llm_batch_size (default 10) for batch chunking.
    """
    # Per D-02: convergence gate via the shared, deterministically-sorted pending-set helper
    # (D-03 anti-drift): recovery and this manual trigger build the SAME sorted batches, so their
    # generate_proposals:<sha256(sorted file_ids)> keys align and dedup (42-RESEARCH Pitfall 2).
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

    # Phase 44 (44-04): the STRAGGLER count (long-running in-flight process_file jobs,
    # "still grinding") and the ANALYSIS_FAILED count ("gave up") -- two distinct buckets
    # (44-02 D-02). Both reads are degrade-safe (the Plan-02 services own the never-500
    # SAVEPOINT/_safe_count degrade and return 0 on any DB error), so NO try/except is added
    # here -- same service-owns-degrade wiring idiom as the busy counts above (175-178).
    straggler_count = await get_straggler_count(session, settings.straggler_threshold_sec)
    analysis_failed_count = await get_analysis_failed_count(session)

    # Phase 49 (49-02, D-05): the "Awaiting cloud" held-file count -- long files held back
    # because no compute agent was online when analysis routed them. get_awaiting_cloud_count
    # owns the never-500 _safe_count degrade (returns 0 on any DB error), so NO try/except is
    # added here -- same service-owns-degrade wiring idiom as the straggler/failed counts above.
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
    # per registry backend {id, kind, rank, cap, in_flight, available, quota_wait, inadmissible}. Seeded
    # IDENTICALLY in pipeline_stats_partial() below so the WHOLE #analyze-lanes grid OOB-swaps on the SAME
    # existing 5s poll (no second loop, no new read endpoint -- Pitfall 2: N is dynamic, no per-lane store
    # keys). The snapshot helper owns the never-500 degrade (-> [] on any error), so NO try/except here --
    # same service-owns-degrade idiom as the cloud counts above. This SUPERSEDES the transitional single
    # non-local lane-kind key (retired); resolved_non_local_kind stays for the :811 callers.
    lanes = await get_backend_lane_snapshot(session)

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
        "stats": stats,
        "current_page": "pipeline",
        "settings_batch_size": settings.llm_batch_size,
        "agents": agents,
        "recent_scans": recent_scans_rows,
        "straggler_count": straggler_count,
        "analysis_failed_count": analysis_failed_count,
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
        "reconcile_scanned": recon["scanned"],
        "reconcile_deduped": recon["deduped"],
        # Phase 58 (58-04): the all-in-stage Analyze file list (D-03).
        # Phase 71 (71-03, BEUI-01 / D-04): the N-lane grid snapshot (seeded above, mirrored identically
        # in pipeline_stats_partial). Retires the transitional single non-local lane-kind context key.
        "lanes": lanes,
        **activity,
        **dag_ctx,
        "queue_progress_percent": queue_progress,
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
        straggler_count,
        analysis_failed_count,
        awaiting_cloud_count,
        pushing_count,
        analyzing_cloud_count,
        inadmissible_count,
        cloud_phase_counts,
        lanes,
        awaiting_hold_reason,
        # asyncio.gather with >6 awaitables of mixed return types collapses to list[object] under
        # mypy (mirrors the identical cast in services/pipeline.py:get_stage_progress) -- pin the
        # exact per-read tuple shape with a single cast.
    ) = cast(
        "tuple[dict[str, int], int, int, int, int, int, int, dict[str, int], list[dict[str, Any]], str]",
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
            # Phase 44 (44-04): the same straggler + ANALYSIS_FAILED buckets the dashboard seeds,
            # re-pushed on every 5s poll so the straggler_failed_card stays live. Degrade-safe at the
            # service layer (44-02), so NO router try/except -- mirrors the dashboard() wiring.
            _read_in_own_session(fanout, lambda s: get_straggler_count(s, settings.straggler_threshold_sec), 0),
            _read_in_own_session(fanout, lambda s: get_analysis_failed_count(s), 0),
            # Phase 49 (49-02, D-05): the same AWAITING_CLOUD held count the dashboard seeds, re-pushed
            # on every 5s poll so the awaiting_cloud_card stays live via its OOB swap. Degrade-safe at the
            # service layer (Plan 01), so NO router try/except -- mirrors the straggler/failed wiring.
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
            _read_in_own_session(fanout, lambda s: get_backend_lane_snapshot(s), cast("list[dict[str, Any]]", [])),
            # The SAME hold-reason derivation build_dashboard_context seeds on first load, re-pushed on every 5s
            # poll so the awaiting_cloud_card sub-caption stays live via its OOB swap (the OOB swap contract:
            # both render paths must agree). Degrade-safe at the service layer, so NO router try/except -- mirrors
            # the lanes wiring immediately above. "held" mirrors services.backends._HOLD_REASON_DEGRADED, the
            # SAME neutral no-causal-claim copy that function's own try/except already degrades to.
            _read_in_own_session(fanout, lambda s: derive_cloud_hold_reason(s), "held"),
        ),
    )
    # phaze-6r39: the same live-lane derivation build_dashboard_context seeds on first load, re-pushed
    # on every 5s poll so the localqueue_card stays live via its OOB swap (the OOB swap contract: both
    # render paths must agree). Pure function over the `lanes` snapshot just resolved above -- no I/O,
    # cannot raise -- so NO router try/except, mirroring the lanes wiring immediately above it.
    localqueue_unreachable = derive_localqueue_unreachable(lanes)
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
            "stats": stats,
            "settings_batch_size": settings.llm_batch_size,
            "oob_counts": True,
            "straggler_count": straggler_count,
            "analysis_failed_count": analysis_failed_count,
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
            "selected_lane": selected_lane,
            "lanes_hash": lanes_hash,
            **activity,
            **dag_ctx,
            "queue_progress_percent": queue_progress,
        },
    )


@router.get("/pipeline/lanes/{backend_id}", response_class=HTMLResponse)
async def lane_detail(
    request: Request,
    backend_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the lane-detail body fragment for a backend lane (DRILL-01 / D-06 / D-07 / D-00b).

    Swapped as innerHTML into the shared ``#detail-pane`` (88-01 shell). ``backend_id`` is operator-
    declared, so it is resolved by lookup-in-known-set against the degrade-safe
    :func:`get_backend_lane_snapshot` (T-88-03): an unknown/offline id renders the friendly "Lane
    offline" empty fragment (200, HTML -- never a 500/JSON/HTTPException, never a raw-param-driven read),
    so htmx still swaps a body into the pane. For a resolved lane the kind-adaptive body renders the
    last ``LANE_RECENT_N`` newest-first completions -- ALL lane kinds, including local (phaze-2u8v.3;
    D-07) -- and the per-lane queue depths; every read is bounded + degrade-safe (D-00b) and the body
    carries its own bounded 5s tick (D-03). Read-only -- no commit. Only secret-free filename/timestamp/id
    scalars leave here (T-88-04); ``backend_id``/``kind`` stay Jinja-autoescaped (T-88-05).
    """
    lanes = await get_backend_lane_snapshot(session)  # degrade-safe -> []
    lane = next((one for one in lanes if one["id"] == backend_id), None)
    if lane is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/_lane_detail.html",
            context={
                "lane": None,
                "backend_id": backend_id,
                "recent_completions": [],
                "queue_depths": {},
                "queue_depths_agent": None,
                "queue_depths_note": None,
                "refreshed_at": None,
                "recent_n": LANE_RECENT_N,
            },
        )
    recent_completions = await get_lane_recent_completions(session, backend_id, lane["kind"])
    # phaze-2u8v.1: the depths are read off the AGENT this lane's dispatch actually enqueues to (a local
    # lane -> the live fileserver agent), never off the registry id. ``depths is None`` is "this lane has
    # no SAQ queue", carried to the template as a NOTE -- the template must not render it as zeros.
    queue_depths = await get_lane_queue_depths(session, request.app.state, backend_id, lane["kind"])
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_lane_detail.html",
        context={
            "lane": lane,
            "backend_id": backend_id,
            "recent_completions": recent_completions,
            "queue_depths": queue_depths.depths,
            "queue_depths_agent": queue_depths.agent_id,
            "queue_depths_note": queue_depths.note,
            "refreshed_at": datetime.now(UTC),
            "recent_n": LANE_RECENT_N,
        },
    )


_VALID_BUCKETS: frozenset[str] = frozenset(s.value for s in Status)


# --- phaze-a6hm.1 sortable-column contracts ------------------------------------------------------
# One SortContract per table, declared at import time so a mis-wired whitelist fails on startup
# rather than degrading every header click (column_sort contract rule 6). Each `expression` is a real
# column bound HERE -- a request `sort=` value is matched against these keys by equality and can
# never name anything else (rule 2). Every `target` is the table's EXISTING host container: this
# feature adds no OOB fragment and no new element id, so it cannot introduce a duplicate one.

# The pending-files fragment is shared machinery keyed by stage; each stage renders its own column
# labels, so each needs its own contract -- labels are how the shared partial recognises a header.
# Only ``metadata`` remains a pending-set workspace (phaze-0jpe removed the fingerprint one), but the
# dict shape is kept because it IS the per-stage seam, not a two-entry convenience.
_PENDING_SORTS: dict[str, SortContract] = {
    "metadata": SortContract(
        endpoint="/pipeline/pending-files",
        target="#metadata-files-view",
        columns=(
            SortableColumn(key="filename", label="File", expression=FileRecord.original_filename),
            SortableColumn(key="file_type", label="Format", expression=FileRecord.file_type),
            SortableColumn(key="file_size", label="Size", expression=FileRecord.file_size),
        ),
        default_key="filename",
    ),
}

# phaze-6not3: sort what is shown -- FILES_SORT's own "sort what you show" precedent (see its comment
# below) was violated on BOTH of these columns. `_tracklist_sets_page_stmt` already outerjoins
# `FileRecord`, so both expressions below can reach it directly (no additional join needed).
#   - "Set" renders `set_name = filename if matched else (artist or event or external_id)`
#     (services/pipeline.py::get_tracklist_sets_page) -- NOT bare `Tracklist.artist`, which put a
#     matched row's audio filename in a column that ordered by a value never shown for that row.
#   - "Tracklist" renders ONLY the two-value `tracklist_state` ("matched"/"candidate") derived from
#     `Tracklist.file_id IS NOT NULL` -- NOT `Tracklist.event`, a column that appears nowhere on
#     screen. `Tracklist.file_id.is_not(None)` groups matched apart from candidate exactly like the
#     rendered state does; a boolean ORDER BY sorts False-then-True (or the reverse on desc), which is
#     the grouping the header promises.
TRACKLIST_SETS_SORT = SortContract(
    endpoint="/pipeline/tracklist-sets",
    target="#tracklist-sets-view",
    columns=(
        SortableColumn(
            key="artist",
            label="Set",
            expression=func.coalesce(FileRecord.original_filename, Tracklist.artist, Tracklist.event, Tracklist.external_id),
        ),
        SortableColumn(key="event", label="Tracklist", expression=Tracklist.file_id.is_not(None)),
    ),
    default_key="artist",
)

# The Files matrix (:func:`pipeline_files`) -- ALL SEVEN rendered columns (phaze-cvn6.1).
#
# `key="file"` orders by the SAME column the File cell renders (`FileRecord.current_path`, the full
# path -- this table has no separate filename-only column), matching the sibling tables' "sort what
# you show" precedent.
#
# phaze-cvn6.1 -- WHY THE STAGE COLUMNS ARE HERE NOW. phaze-a6hm ("make every data table
# sortable") shipped this table with File + Type only; child phaze-a6hm.3 recorded the stage
# columns as "per-page DERIVED stage_status_case CASE expressions, not stable columns a SQL ORDER BY
# can address". That is a GAP, not a regression -- the columns were never sortable and nothing was
# lost -- but the stated reason does not hold: `_files_page_stmt` has ORDERED nothing by them, yet has
# FILTERED by the very same expression (`stage_status_case(stage) == bucket`) since Phase 87, so the
# expression plainly is addressable in SQL. What was really missing was a DEFINED ORDER for an
# enum-like status; alphabetical would interleave `failed` between `done` and `in_flight` and answer
# nothing. `stage_status_sort_case` supplies that order -- see STAGE_STATUS_DISPLAY_ORDER in
# `services/stage_status.py` for the ladder and its cost note.
#
# The five stage labels below MUST stay identical to `_stage_cols` in `files_table_view.html` -- a label is
# how the template recognises a header as sortable, so a typo degrades the header to plain text
# rather than erroring. `tests/integration/test_files_sort.py` asserts the two agree, in both
# directions, so the pair cannot drift silently.
#
# The 6-stage -> 5-pill remap LANDMINE applies to the KEYS too: `Appr` reads the `review` bucket and
# `Exec` reads `apply`. The wire keys are the canonical `Stage` values rather than the header words,
# so the URL names the model's vocabulary and no third naming scheme is invented.
FILES_SORT = SortContract(
    endpoint="/pipeline/files",
    target="#files-table-view",
    columns=(
        SortableColumn(key="file", label="File", expression=FileRecord.current_path),
        SortableColumn(key="type", label="Type", expression=FileRecord.file_type),
        SortableColumn(key="metadata", label="Meta", expression=stage_status_sort_case(Stage.METADATA)),
        SortableColumn(key="analyze", label="Analyze", expression=stage_status_sort_case(Stage.ANALYZE)),
        SortableColumn(key="propose", label="Prop", expression=stage_status_sort_case(Stage.PROPOSE)),
        SortableColumn(key="review", label="Appr", expression=stage_status_sort_case(Stage.REVIEW)),
        SortableColumn(key="apply", label="Exec", expression=stage_status_sort_case(Stage.APPLY)),
    ),
    default_key="file",
)


@router.get("/pipeline/files", response_class=HTMLResponse)
async def pipeline_files(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    stage: str | None = Query(None),
    bucket: str | None = Query(None),
    sort: str | None = Query(None),
    order: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the paginated, per-row-derived files table (UI-01 / D-02).

    The scannable "where's this file at?" overview: each row carries the six-pill stage matrix
    derived per page (never the raw ``files.state`` column string, never a whole-corpus scan per poll
    -- see :func:`phaze.services.pipeline.get_files_page`). The ``stage``+``bucket`` query params are
    validated against the ``Stage`` / ``Status`` allowlists (T-87-14 -- an unknown value degrades to
    an unfiltered page rather than 422-ing the poll) and plumbed through NOW so Plan 05's status-filter
    bar is templates-only. The read is SAVEPOINT degrade-safe at the service layer, so NO router
    try/except -- a DB hiccup renders a safe empty page, never a 500.

    phaze-a6hm.3 sortable-column contract -- see :func:`pending_files_fragment`. ``stage``/``bucket``
    ride ``sort``'s ``view_state`` (contract rule 4) alongside ``page_size``, so a header click keeps
    the operator's filter lens and a Prev/Next click keeps the operator's chosen order via
    :meth:`~phaze.routers.column_sort.SortState.query_state` in the template.

    phaze-p7ox: ``_status_filter_bar.html``'s filter form and Clear-filter anchor both
    ``hx-push-url="true"`` THIS endpoint into the address bar (D-03's URL-carried-lens idiom), but
    this handler used to unconditionally return the chrome-less ``files_table_view.html`` fragment --
    no ``wants_fragment()`` fork, no full-document fallback. Per ``response_shape.py`` rule 2, a
    history-restore (Back/Forward after htmx's 10-entry cache evicts the snapshot -- routine) sets
    ``HX-Request: true`` too but IGNORES ``hx-target`` and swaps into ``<body>``, so the fragment
    replaced the whole page with an orphaned filter bar + table; a plain reload/bookmark of the
    pushed URL (no htmx headers at all) hit the exact same branch and served a raw fragment with no
    ``<html>``, CSS, htmx, or Alpine. Mirroring ``audit_log`` (``execution.py``): a live htmx swap
    (``wants_fragment`` True) still gets the same bare fragment, unchanged; anything else -- a plain
    request or a restore -- gets the full ``pipeline/files.html`` page instead.
    """
    stage_enum: Stage | None = None
    if stage:
        try:
            stage_enum = Stage(stage)
        except ValueError:
            stage_enum = None
    bucket_val = bucket if bucket in _VALID_BUCKETS else None
    sort_state = FILES_SORT.resolve(
        sort=sort,
        order=order,
        view_state={"page_size": page_size, "stage": stage_enum.value if stage_enum is not None else None, "bucket": bucket_val},
    )
    files_page = await get_files_page(session, page=page, page_size=page_size, stage=stage_enum, bucket=bucket_val, sort=sort_state)
    context = {
        "files_page": files_page,
        "active_stage": stage_enum.value if stage_enum is not None else None,
        "active_bucket": bucket_val,
        "sort": sort_state,
    }
    if wants_fragment(request):
        return templates.TemplateResponse(request=request, name="pipeline/partials/files_table_view.html", context=context)

    return templates.TemplateResponse(request=request, name="pipeline/files.html", context=context)


@router.get("/pipeline/analyze-files", response_class=HTMLResponse)
async def analyze_files_fragment(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    status: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the Analyze per-file table fragment: the bounded default working set OR a filtered page (phaze-zqvh.2).

    The status-filter bar in ``_analyze_files.html`` hx-gets this endpoint into ``#analyze-files-view`` --
    the SAME URL-carried-lens idiom as ``/pipeline/files`` / ``_status_filter_bar.html``. ``status`` is
    validated against the ``ANALYZE_FILTERS`` allowlist (T-57-01 / T-87-14: an unknown value NEVER reaches
    a template path or SQL string -- it degrades to the default view, never a 422 into the render):

      * no / unknown ``status`` -> the DEFAULT bounded working-set view (the active-first working set,
        PAGED, plus the LIMIT-ed recent-completions window on the final page), with a pager.
      * a valid ``status`` -> the full analyze-stage listing under that lens, served as a bounded page
        (``get_analyze_files_page``: OFFSET + ``page_size + 1`` sentinel, never a whole-corpus COUNT).

    Both service reads are SAVEPOINT degrade-safe (never 500 the fragment). This endpoint is a SIBLING of
    the 5s ``/pipeline/stats`` poll -- it is NEVER in the poll's OOB fan-out, so the operator's page position
    and filter selection survive every tick (the file grid stays outside the poll, phaze-zqvh.2 acceptance).
    """
    status_val = status if status in ANALYZE_FILTERS else None
    if status_val is None:
        # DEFAULT bounded working-set view (no explicit filter): the active-first set, PAGED, with the
        # completions window appended on the final page. phaze-5462: this branch is now paged like every
        # other -- it used to return the whole unbounded working set.
        working_set = await get_analyze_working_set(session, page=page, page_size=page_size)
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/_analyze_files.html",
            context={
                "analyze_rows": working_set.rows,
                "analyze_page": working_set,
                "active_status": None,
            },
        )
    analyze_page = await get_analyze_files_page(session, page=page, page_size=page_size, status=status_val)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_analyze_files.html",
        context={
            "analyze_rows": analyze_page.rows,
            "analyze_page": analyze_page,
            "active_status": status_val,
        },
    )


# phaze-5462: the stage allowlist for the shared pending-files fragment. Validated as a SET so an
# unknown value can NEVER reach SQL or a template path (T-57-01 / T-87-14) -- it degrades to
# "metadata", never a 422 into the render.
_PENDING_STAGES: dict[str, Stage] = {"metadata": Stage.METADATA}


@router.get("/pipeline/pending-files", response_class=HTMLResponse)
async def pending_files_fragment(
    request: Request,
    stage: str = Query("metadata"),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    sort: str | None = Query(None),
    order: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render ONE bounded page of an enrich workspace's pending set (phaze-5462).

    The sibling of :func:`analyze_files_fragment`, on the SAME paging contract
    (:mod:`phaze.services.pagination`): shared page size, OFFSET paging, a ``page_size + 1`` sentinel
    for ``has_next`` (never a whole-corpus COUNT), and the mandatory unique tiebreaker. Both enrich
    enrich workspaces hx-get this on load into their empty host div, so none server-renders a file
    row inline any more.

    ``stage`` is validated against :data:`_PENDING_STAGES` (unknown -> metadata) and is carried into
    the template only as an autoescaped query value -- never a template path (T-57-01).

    NOTE: this is the RENDER read. The EXTRACT ALL button still enqueues the
    UNBOUNDED pending set (paging contract rule 7) -- paging the enqueue would silently drop work.
    """
    stage_key = stage if stage in _PENDING_STAGES else "metadata"
    # phaze-a6hm.1: resolve BEFORE the read. `sort`/`order` are raw wire strings here and whitelisted
    # strings after; the query below never sees the untrusted value. `stage` rides view_state so a
    # header click keeps the operator on their own lens (contract rule 4). `page` deliberately does
    # NOT -- a re-sort returns to page 1.
    sort_state = _PENDING_SORTS[stage_key].resolve(sort=sort, order=order, view_state={"stage": stage_key, "page_size": page_size})
    pending_page = await get_pending_files_page(session, _PENDING_STAGES[stage_key], page=page, page_size=page_size, sort=sort_state)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_pending_files.html",
        context={
            "pending_page": pending_page,
            "stage": stage_key,
            "host_id": f"{stage_key}-files-view",
            "sort": sort_state,
        },
    )


@router.get("/pipeline/tracklist-sets", response_class=HTMLResponse)
async def tracklist_sets_fragment(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    sort: str | None = Query(None),
    order: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render ONE bounded page of the per-set Tracklist coverage table (phaze-1wvb).

    Same paging contract as :func:`pending_files_fragment`. The Tracklist workspace hx-gets this into
    the empty host div BELOW its three step cards; the step cards themselves (and their SEARCH /
    SCRAPE / MATCH ALL triggers, which enqueue the UNBOUNDED pending sets -- rule 7) are untouched
    and still server-rendered by the shell.
    """
    # phaze-a6hm.1 sortable-column contract -- see :func:`pending_files_fragment`.
    sort_state = TRACKLIST_SETS_SORT.resolve(sort=sort, order=order, view_state={"page_size": page_size})
    sets_page = await get_tracklist_sets_page(session, page=page, page_size=page_size, sort=sort_state)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_tracklist_sets.html",
        context={"sets_page": sets_page, "host_id": "tracklist-sets-view", "sort": sort_state},
    )


@router.post("/pipeline/analyze", response_class=HTMLResponse)
async def trigger_analysis_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger per-file duration-routed analysis and return the split-count fragment (Phase 49; Phase 50 CLOUDPIPE-01 reshape).

    Mirrors :func:`trigger_analysis`: short/null files route to the fileserver as before; long
    files are ALWAYS held in ``AWAITING_CLOUD`` (no direct-to-compute enqueue -- see
    :func:`_route_discovered_by_duration`), and short/null files with no fileserver online are
    skipped without aborting the run. The fragment reports ``N local, M cloud, K awaiting cloud``
    (+ a skipped bucket); ``cloud`` is always 0. The no-active-agent fragment is rendered when NO
    fileserver agent is online (nothing can route locally).
    """
    files_with_duration = await get_discovered_files_with_duration(session)
    count = len(files_with_duration)

    if count == 0:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/trigger_response.html",
            context={"request": request, "action": "analysis", "count": 0, "no_active_agent": False},
        )

    # Phase 71 (BEUI-02, D-08): same force-local fold as the JSON trigger -- effective cloud_enabled is
    # ``registry cloud_enabled AND NOT force_local``, so a forced registry routes every file local.
    effective_cloud_enabled = settings.cloud_enabled and not await get_route_control(session)
    counts = await _route_discovered_by_duration(
        request.app.state,
        session,
        files_with_duration,
        settings.cloud_route_threshold_sec,
        effective_cloud_enabled,
        settings.models_path,
    )

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={
            "request": request,
            "action": "analysis",
            "count": count,
            "no_active_agent": bool(counts["no_active_agent"]),
            "split_counts": True,
            "local": counts["local"],
            "cloud": counts["cloud"],
            "awaiting": counts["awaiting"],
            "skipped": counts["skipped"],
        },
    )


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
    # in-flight marker of a still-running deepen. deepen_analysis re-enqueues process_file WITHOUT
    # clearing failed_at (unlike retry_analysis_failed), so a long ANALYSIS_FAILED file with a deepen
    # grinding for hours satisfies every candidate conjunct. Deleting its ledger row + holding it for
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
    # The response count reflects the files actually acted on (live-deepen files were excluded above).
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
        # this point, so a concurrent process_file enqueue (deepen_analysis / retry_analysis_failed's
        # background loop) for one of these EXACT candidates can land in the gap between that snapshot
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


@router.post("/pipeline/analysis-failed/retry", response_class=HTMLResponse)
async def retry_analysis_failed(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated BULK retry of every ANALYSIS_FAILED file (quick-260707-d79).

    ANALYSIS_FAILED is a terminal state that ``recover_orphaned_work`` deliberately treats as
    analyze-DONE (:func:`phaze.tasks.reenqueue._select_done_analyze_ids`), so a genuinely
    un-analyzable file is never auto-looped. This endpoint is that invariant's deliberate,
    operator-gated counterpart: it re-drives EVERY ANALYSIS_FAILED file through the SAME guarded
    funnel :func:`deepen_analysis` uses -- per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`enqueue_process_file` (COMPLETE ``ProcessFilePayload`` + deterministic
    ``process_file:<id>`` key) -- but with NORMAL caps: a retry is a fresh re-analysis, NOT a
    deepen, so ``fine_cap`` / ``coarse_cap`` are left at their None default (the standard 60/30
    window budget), not the deepen sentinel 0.

    Ordering follows the Phase-30 / RESEARCH-Pitfall-3 guards:
    - Resolve the per-agent queue ONCE. ``process_file`` is an AGENT_TASK; if no agent is online
      ``NoActiveAgentError`` is caught and the endpoint returns a fragment WITHOUT flipping any
      state or enqueuing -- it never falls through to the consumer-less default queue.
    - Clear the ``analysis.failed_at`` / ``error_message`` marker, then ``commit`` BEFORE any
      enqueue (get_session does NOT auto-commit): the files leave the red bucket immediately;
      ``put_analysis`` clears it again (a no-op) on success, or ``report_analysis_failed``
      re-stamps it only if it fails AGAIN. Phase 90 (D-09) removed the companion
      ``FileRecord.state = FINGERPRINTED`` reset -- clearing the ``analysis`` marker is now the
      sole required mutation (see the inline note below).
    - The deterministic key dedups any file with a live in-flight job to a no-op, so re-enqueuing
      the WHOLE failed set is safe (dedup-safe; no silent cap).
    - phaze-zecg: the enqueue loop itself runs as a BACKGROUND task (``asyncio.create_task`` + the
      ``_background_tasks`` discipline), not inline in the request -- the state-clearing commit
      above already moved every file off the red bucket, so a client/proxy timeout cancelling this
      HTTP request can no longer leave the tail of a large failed set with its marker cleared but
      no job ever enqueued.
    - phaze-r7j9: the marker-clearing UPDATE binds the routed id list as ONE ``uuid[]`` array
      parameter (:func:`_analysis_file_ids_scope`) instead of a bare ``.in_(...)``, which expands to
      one bind parameter per id and exceeds asyncpg's 32767-parameter cap at the ~44.5K-file incident
      scale this docstring cites -- that used to make the bulk retry itself deterministically 500 at
      the exact scale it exists for.
    - phaze-4ter: :func:`_retry_analysis_group` contains each background enqueue individually and
      restores the marker for any file whose enqueue failed, so a transient queue error can no longer
      silently drop a file off the red bucket with no job and no trace that it ever failed.
    """
    files = await get_analysis_failed_files(session)
    if not files:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        # phaze-c9w9: group the failed set by each file's OWNING agent -- never one
        # most-recently-seen pick for the whole set. Files whose owner is offline are skipped
        # (marker left in place, retryable later), never rerouted onto another agent's mount.
        routed_groups, skipped_files = await enqueue_router.resolve_queues_for_owned_files("process_file", request.app.state, session, files)
    except enqueue_router.NoActiveAgentError:
        # Do NOT flip state, do NOT enqueue, do NOT fall through to the default queue (Phase-30).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    if skipped_files:
        logger.warning("retry_analysis_failed: owning agent offline -- files left failed", skipped=len(skipped_files))
    routed_files = [f for _, group in routed_groups for f in group]

    # RESEARCH Pitfall 3: flip out of the terminal bucket and COMMIT before any enqueue so the
    # red count drops on the next 5s poll regardless of the enqueue outcome.
    #
    # Clear the durable `analysis.failed_at` marker and COMMIT before any enqueue so the red count
    # drops on the next 5s poll. Phase 90 (D-09): the paired `files.state = FINGERPRINTED` reset was
    # removed -- `analysis.failed_at` is now the sole failure authority (`failed_clause(Stage.ANALYZE)`,
    # readers cut over in PR-A). Clearing it moves the row off the failed disjunct so it derives
    # `not_started` -- exactly what a fresh re-analysis should see (the XOR CHECK guarantees
    # `analysis_completed_at IS NULL` on a failed row). phaze-c9w9: cleared ONLY for the files that
    # actually route -- clearing a skipped (owner-offline) file's marker would drop it off the red
    # bucket with no job ever enqueued.
    #
    # phaze-r7j9: array-bind the whole routed id list as ONE `uuid[]` parameter
    # (`_analysis_file_ids_scope`) rather than a bare `.in_(...)`, which SQLAlchemy expands to one
    # bind parameter per id -- past asyncpg's 32767-parameter wire cap at the ~44.5K-file incident
    # scale this handler is documented to be hardened for.
    routed_file_ids = [f.id for f in routed_files]
    await session.execute(
        update(AnalysisResult).where(_analysis_file_ids_scope(routed_file_ids, "retry_ids")).values(failed_at=None, error_message=None),
    )
    await session.commit()

    # phaze-zecg: BACKGROUND the enqueue loop -- do not await it inline in the request. The failed
    # set is unbounded (repo incident history documents ~44.5K-job failure sets) and the markers
    # for the WHOLE set were just committed above; an inline loop that a client/proxy timeout
    # cancels mid-way leaves the tail of the set with its failure marker cleared but NO job ever
    # enqueued -- a silent state-loss window between the commit and the last enqueue. Backgrounding
    # via the same `_background_tasks` discipline every other bulk trigger in this router uses
    # (e.g. `_route_discovered_by_duration`, `trigger_metadata_extraction`) removes that window: the
    # response returns immediately and the enqueue loop keeps running to completion regardless of
    # what the client/proxy does with the connection.
    #
    # NORMAL caps: NO fine_cap/coarse_cap override -- a retry is a fresh re-analysis, not a deepen.
    # The single funnel (_enqueue_analysis_jobs -> enqueue_process_file) guarantees the full payload
    # + deterministic dedup key. phaze-4ter: routed through `_retry_analysis_group`, which contains
    # per-file enqueue failures and restores `failed_at` for any file that never got a replacement
    # job, instead of the marker cleared above and the job both silently vanishing.
    for routed, group in routed_groups:
        task = asyncio.create_task(_retry_analysis_group(routed.queue, group, cast("str", routed.agent_id), settings.models_path))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    logger.info("retry_analysis_failed re-queued files", count=len(routed_files))
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/retry_failed_response.html",
        context={"request": request, "count": len(routed_files), "no_active_agent": False},
    )


@router.post("/pipeline/metadata-failed/retry", response_class=HTMLResponse)
async def retry_metadata_failed(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated BULK retry of every terminally-failed metadata file (FAIL-03).

    Closes gap G-01 (SC#3): a metadata failure persisted by the 81-03 writer (``metadata`` row
    with ``failed_at`` set, payload NULL) derives FAILED and would otherwise be a permanent
    dead-end blocking the file from ever reaching ``propose``. This is the operator-gated retry:
    it re-drives EVERY ``metadata.failed_at IS NOT NULL`` file through the SAME guarded funnel the
    manual metadata triggers use -- per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`_enqueue_extraction_jobs` (the COMPLETE ``ExtractMetadataPayload``, not a
    dead-lettering file_id-only enqueue) + the central deterministic ``extract_file_metadata:<id>``
    dedup key.

    It mirrors :func:`retry_analysis_failed`'s Phase-30-hardened ordering, MINUS the state flip:
    - Resolve the per-agent queue ONCE. ``extract_file_metadata`` is an AGENT_TASK; if no agent is
      online ``NoActiveAgentError`` is caught and the endpoint returns a fragment WITHOUT enqueuing
      or mutating any state -- it never falls through to the consumer-less default queue (Phase-30).
    - D-11: NO ``f.state`` flip. Metadata has no terminal FileState -- the failure lives only in the
      ``metadata`` failure row. The row is LEFT in place; clearing ``failed_at`` here would make a
      zero-metadata file read DONE forever. ``put_metadata``'s clear-on-success (81-03) wipes the
      marker only when real metadata lands, or ``report_metadata_failed`` re-stamps it on another
      failure. With no state mutation there is nothing to commit before the enqueue.
    - The deterministic key dedups any file with a live in-flight job to a no-op, so re-enqueuing
      the WHOLE failed set is safe (dedup-safe; no silent cap).
    - phaze-zecg: ``_enqueue_extraction_jobs`` runs as a BACKGROUND task here too (matching every
      other caller of it), so a large failed set can no longer time out the HTTP request/proxy.
    """
    files = await get_metadata_failed_files(session)
    if not files:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        # phaze-c9w9: group the failed set by each file's OWNING agent -- never one
        # most-recently-seen pick for the whole set (owner-offline files are skipped, not rerouted).
        routed_groups, skipped_files = await enqueue_router.resolve_queues_for_owned_files("extract_file_metadata", request.app.state, session, files)
    except enqueue_router.NoActiveAgentError:
        # Do NOT enqueue, do NOT mutate state, do NOT fall through to the default queue (Phase-30).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    if skipped_files:
        logger.warning("retry_metadata_failed: owning agent offline -- files left failed", skipped=len(skipped_files))
    routed_count = sum(len(group) for _, group in routed_groups)

    # D-11: no state flip, so no pre-enqueue commit. Build the COMPLETE payload via the shared
    # producer (a file_id-only enqueue dead-letters every job) and rely on the central
    # extract_file_metadata:<file_id> key for in-flight dedup.
    #
    # phaze-zecg: BACKGROUND the enqueue loop instead of awaiting it inline -- `_enqueue_extraction_
    # jobs` is docstring-labeled "Background coroutine" and every OTHER caller (`trigger_metadata_
    # extraction`, `trigger_extraction_ui`) already wraps it in `asyncio.create_task` + the
    # `_background_tasks` discipline specifically "to avoid HTTP timeout on large file counts" -- this
    # bulk retry was the one caller that awaited it inline, so a large failed set could time out the
    # HTTP request/proxy mid-loop.
    for routed, group in routed_groups:
        task = asyncio.create_task(_enqueue_extraction_jobs(routed.queue, group, cast("str", routed.agent_id)))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    logger.info("retry_metadata_failed re-queued files", count=routed_count)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/metadata_retry_response.html",
        context={"request": request, "count": routed_count, "no_active_agent": False},
    )


# --------------------------------------------------------------------------------------------------
# Per-file scoped retry variants (87-07 / UI-02 / D-04): the console's per-row Retry on a failed
# enrich cell. Each re-drives ONE file through the SAME Phase-30-hardened guarded funnel the bulk
# endpoints use (``enqueue_router.resolve_queue_for_task`` -> ``NoActiveAgentError`` guard ->
# enqueue), filtered to a single ``file_id`` instead of the whole failed set, and reuses the bulk
# response partials VERBATIM (a count of 1 / 0). The analyze variant preserves the manual-only
# terminal-analyze path (``ELIGIBLE_AFTER_FAILURE[ANALYZE]=False``): it flips ANALYSIS_FAILED ->
# FINGERPRINTED + clears ``analysis.failed_at`` in ONE transaction and commits BEFORE enqueue (the
# Phase-81 CR-01 rule) so the file leaves the failed disjunct -- it NEVER creates an auto-retry loop
# (the 44.5K over-enqueue guard, behavior 8).
# --------------------------------------------------------------------------------------------------
@router.post("/pipeline/files/{file_id}/analysis-failed/retry", response_class=HTMLResponse)
async def retry_analysis_failed_file(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated PER-FILE retry of ONE ANALYSIS_FAILED file (87-07, UI-02 / D-04).

    The scoped twin of :func:`retry_analysis_failed`: it re-drives EXACTLY ONE file through the
    identical guarded funnel (per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`enqueue_process_file` with the COMPLETE ``ProcessFilePayload`` + deterministic
    ``process_file:<id>`` key), scoped by ``id == file_id AND`` the derived terminal analyze-failure
    marker (``failed_clause(Stage.ANALYZE)``, Phase 90 PR-A -- no longer the retired
    ``files.state == ANALYSIS_FAILED`` column) so a non-failed (or unknown) file is a safe no-op ack
    (T-87-27 input validation — a UUID path param + the failure-marker guard, never an unscoped
    enqueue).

    MANUAL-ONLY, no auto-loop (D-00b, behavior 8, T-87-24): analyze is the ONLY enrich carve-out
    (``ELIGIBLE_AFTER_FAILURE[ANALYZE]=False``) — a FAILED analyze is terminal and is NEVER
    auto-retried by ``recover_orphaned_work`` / the derived pending set. This endpoint is that
    invariant's deliberate operator-gated counterpart: it clears the ``analysis.failed_at`` /
    ``error_message`` marker, then ``commit`` BEFORE the enqueue (``get_session`` does NOT
    auto-commit) so the file leaves the failed disjunct immediately and derives ``not_started`` for a
    fresh re-analysis. Phase 90 (D-09) removed the companion ``FileRecord.state = FINGERPRINTED``
    reset -- clearing the ``analysis`` marker is now the sole required mutation. The deterministic key
    dedups a live in-flight job to a no-op (T-87-26). The ack is count/bool-only — no operator
    free-text crosses into Jinja (T-d79-04).
    """
    file = (
        # Phase 90 (PR-A, D-09): scope on the DERIVED terminal analyze-failure marker
        # (``failed_clause(Stage.ANALYZE)`` -- an analysis row with ``failed_at`` set), no longer the
        # retired ``files.state == ANALYSIS_FAILED`` column. A non-failed (or unknown) file is a safe no-op.
        await session.execute(select(FileRecord).where(FileRecord.id == file_id, failed_clause(Stage.ANALYZE)))
    ).scalar_one_or_none()
    if file is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        # phaze-c9w9: route to the FILE's owning agent (agent_id=file.agent_id), never the
        # most-recently-seen fileserver -- an owner-offline file surfaces no_active_agent.
        routed = await enqueue_router.resolve_queue_for_task("process_file", request.app.state, session, agent_id=file.agent_id)
    except enqueue_router.NoActiveAgentError:
        # Do NOT flip state, do NOT enqueue, do NOT fall through to the default queue (Phase-30, T-87-25).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    # process_file is an AGENT_TASK -- resolve always returns a non-None agent_id.
    agent_id = cast("str", routed.agent_id)

    # CR-01 (Phase 81): clear the durable `analysis.failed_at` marker and COMMIT before the enqueue so
    # the row leaves the failed disjunct and derives not_started (a fresh re-analysis). Phase 90 (D-09):
    # the paired `files.state = FINGERPRINTED` reset was removed -- `analysis.failed_at` is now the sole
    # failure authority (`failed_clause(Stage.ANALYZE)`, readers cut over in PR-A).
    await session.execute(
        update(AnalysisResult).where(AnalysisResult.file_id == file_id).values(failed_at=None, error_message=None),
    )
    await session.commit()

    # phaze-gcdih: the marker-clear above is ALREADY committed, so an exception raised by the enqueue
    # itself (SAQ's job insert runs on its OWN psycopg3 pool, independent of this session -- see the
    # two-pool analysis at ADR-0003 / pipeline.py:1576-1583) must not be allowed to propagate bare: that
    # would leave the file with no failure marker AND no replacement job, invisible to both the
    # ANALYSIS_FAILED bucket and recover_orphaned_work (ANALYZE is manual-only, D-00b). Mirror the bulk
    # twin's restore (`_retry_analysis_group`): re-stamp the marker on a failed enqueue and tell the
    # operator honestly instead of a dropped htmx 500.
    try:
        # NORMAL caps: a retry is a fresh re-analysis, not a deepen -- no fine_cap/coarse_cap override.
        await enqueue_process_file(routed.queue, file, agent_id, settings.models_path)
    except Exception:
        logger.exception("retry_analysis_failed_file: failed to enqueue process_file job", file_id=str(file_id))
        await session.execute(
            update(AnalysisResult)
            .where(AnalysisResult.file_id == file_id)
            .values(failed_at=func.now(), error_message="retry_analysis_failed_file: enqueue error, see agent logs (phaze-gcdih)"),
        )
        await session.commit()
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": False, "enqueue_failed": True},
        )

    logger.info("retry_analysis_failed_file re-queued", file_id=str(file_id))
    # phaze-bgz26: this endpoint's ONLY caller is the Files matrix per-row Retry button
    # (files_table_view.html), and the bucket genuinely changed (failed -> not_started) by the
    # `failed_at` clear + commit above -- but nothing re-renders that row without a full poll
    # this surface never gets (files_table_view.html has NO self-poll by design). Re-derive the
    # bucket AFTER the commit and OOB-push the single Files-matrix pill this write invalidated,
    # the same shape force_skip_stage uses for the record pane (see `_stage_pill_oob`).
    buckets = await get_file_stage_buckets(session, file_id)
    ack = templates.get_template("pipeline/partials/retry_failed_response.html").render(count=1, no_active_agent=False)
    pill_oob = _stage_pill_oob(file_id, "analyze", buckets.get("analyze", "not_started"), id_prefix="files-stage-pill")
    return HTMLResponse(ack + pill_oob)


@router.post("/pipeline/files/{file_id}/metadata-failed/retry", response_class=HTMLResponse)
async def retry_metadata_failed_file(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated PER-FILE retry of ONE terminally-failed metadata file (87-07, UI-02 / D-04).

    The scoped twin of :func:`retry_metadata_failed`: it re-drives EXACTLY ONE file through the
    identical guarded funnel (per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`_enqueue_extraction_jobs` with the COMPLETE ``ExtractMetadataPayload`` + the central
    ``extract_file_metadata:<id>`` dedup key), scoped by ``id == file_id AND EXISTS(a metadata row
    with failed_at)`` so a non-failed (or unknown) file is a safe no-op ack (T-87-27).

    D-11: NO ``f.state`` flip and NO ``failed_at`` clear — metadata has no terminal FileState and the
    failure lives only in the ``metadata`` row; clearing it here would make a zero-metadata file read
    DONE forever. ``put_metadata``'s clear-on-success wipes the marker only when real metadata lands.
    With no state mutation there is nothing to commit before the enqueue. The deterministic key dedups
    a live in-flight job to a no-op (T-87-26). The ack is count/bool-only (T-d79-04).
    """
    file = (
        await session.execute(
            select(FileRecord).where(
                FileRecord.id == file_id,
                exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id, FileMetadata.failed_at.isnot(None))),
            ),
        )
    ).scalar_one_or_none()
    if file is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        # phaze-c9w9: route to the FILE's owning agent (agent_id=file.agent_id), never the
        # most-recently-seen fileserver -- an owner-offline file surfaces no_active_agent.
        routed = await enqueue_router.resolve_queue_for_task("extract_file_metadata", request.app.state, session, agent_id=file.agent_id)
    except enqueue_router.NoActiveAgentError:
        # Do NOT enqueue, do NOT mutate state, do NOT fall through to the default queue (Phase-30, T-87-25).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    # extract_file_metadata is an AGENT_TASK -- resolve always returns a non-None agent_id.
    agent_id = cast("str", routed.agent_id)

    # D-11: no state flip, so no pre-enqueue commit. The shared producer builds the COMPLETE payload
    # and the central extract_file_metadata:<file_id> key dedups an in-flight job.
    await _enqueue_extraction_jobs(routed.queue, [file], agent_id)

    logger.info("retry_metadata_failed_file re-queued", file_id=str(file_id))
    # phaze-bgz26: same shape as retry_analysis_failed_file -- OOB-push the Files-matrix pill this
    # write invalidated. D-11 means `failed_at` is NOT cleared here, so the re-derived bucket
    # stays "failed" until `put_metadata`'s clear-on-success later lands real metadata; pushing it
    # anyway keeps the pill's id fresh in the DOM rather than claiming a bucket flip that has not
    # happened yet (it will land on the next per-file retry / force-skip / poll-driven action).
    buckets = await get_file_stage_buckets(session, file_id)
    ack = templates.get_template("pipeline/partials/metadata_retry_response.html").render(count=1, no_active_agent=False)
    pill_oob = _stage_pill_oob(file_id, "metadata", buckets.get("metadata", "failed"), id_prefix="files-stage-pill")
    return HTMLResponse(ack + pill_oob)


# --------------------------------------------------------------------------------------------------
# Force-skip writer (UI-04 / D-08/D-09/D-10): the right-pane escape hatch that lets the ``failed``
# bucket converge for genuinely-unprocessable files. The correctness-sensitive mutating endpoint of
# this phase: enrich-only (approval-bypass hazard, D-10), additive (never clears a failure marker, so
# the Phase-79 shadow-compare gate stays green), reason required + sanitized (NUL-abort footgun), and
# committed (get_session NEVER auto-commits).
# --------------------------------------------------------------------------------------------------
@router.post("/pipeline/files/{file_id}/skip/{stage}", response_class=HTMLResponse)
async def force_skip_stage(
    file_id: uuid.UUID,
    stage: str,
    reason: Annotated[str, Form()],
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Force-skip an ENRICH stage for one file: write a distinct ``skipped`` marker (UI-04, D-08/D-09/D-10).

    The escape hatch that lets the ``failed`` bucket converge for genuinely-unprocessable files. It is
    deliberately NOT ``done``: a ``stage_skip`` marker row derives the honest ``skipped`` bucket, which
    stays distinguishable from real completion forever (D-08). Discipline (mirrors the retry endpoints +
    ``pipeline_stages._validate_stage``):

    - ENRICH-ONLY (D-10, T-87-18): ``stage`` must be in :data:`STAGE_TO_FUNCTION`
      (metadata/analyze) — a ``propose``/``review``/``apply`` skip is an approval-bypass
      hazard and returns 422 BEFORE any write, backstopped by the Plan-01 DB CHECK.
    - REASON REQUIRED (D-09, T-87-22): a blank/whitespace reason returns the inline validation fragment
      with NO write.
    - SANITIZED (T-87-19): ``sanitize_pg_text`` strips NUL / lone surrogates before persist — a NUL in
      free text passes pydantic then aborts the PG txn (the unbounded-recovery-loop footgun).
    - ADDITIVE-ONLY (T-87-20): the writer ONLY adds the marker row; it NEVER clears ``analysis.failed_at``
      or any failure marker, so a terminally-failed stage keeps its failure fact and the shadow-compare
      gate stays green. Precedence (``done ≻ skipped ≻ failed``) — not the writer — decides the bucket.
    - COMMITTED (Pitfall 7): ``get_session`` does NOT auto-commit, so the writer commits itself.

    The pill flips to ``⊘ skipped`` on the NEXT poll tick (not optimistic) — the ack is a toast only.
    ``reason`` is never echoed back into the response (T-87-21 — no XSS surface via the free text).

    UNKNOWN/CONCURRENTLY-DELETED FILE (request_guards.py contract rule 4): ``StageSkip.file_id`` is a
    NOT NULL FK to ``files.id`` that ``on_conflict_do_nothing(index_elements=["file_id", "stage"])``
    does not shield — that arbiter only absorbs the UNIQUE(file_id, stage) replay, not a FK whose
    referent is missing. ``file_id`` is a typed ``uuid.UUID`` path param already well-formed by the
    time this body runs, so no stricter signature could reject a nonexistent-but-well-formed id; this
    is a genuine race (a concurrent ``delete_scan_cascade`` removing the row between page-render and
    submit), not a wire-boundary defect. Two layers, not one:

    - A pre-check (:func:`_force_skip_file_exists`) short-circuits the common case with a clean no-op
      toast and skips the DB round trip through Postgres's error path -- but it is a TOCTOU hole on
      its own, since the row can vanish between the check and the INSERT.
    - The INSERT itself runs inside a SAVEPOINT (``session.begin_nested()``) and a caught
      ``IntegrityError`` unwinds only the nested scope (rule 5), leaving the session usable for the
      rest of the request, so the race window between the pre-check and the write is closed too.

    Mirrors the sibling per-file endpoints' T-87-27 discipline: an unknown well-formed UUID is a safe
    no-op, never a 500.
    """
    if stage not in STAGE_TO_FUNCTION:  # D-10 enrich-only — mirror pipeline_stages._validate_stage
        raise HTTPException(status_code=422, detail="stage not force-skippable")
    # Sanitize BEFORE the blank check (WR-01): str.strip() alone does not remove NUL / control chars, so a
    # NUL-only reason would slip past a raw-input gate and then persist as "". Validate the SANITIZED value.
    clean_reason = sanitize_pg_text(reason).strip()  # project memory: NUL aborts the PG txn (services/pg_text.py)
    if not clean_reason:  # D-09 reason required — inline validation on the sanitized value, NO write
        return HTMLResponse(
            '<p class="text-sm font-medium text-red-600 dark:text-red-400" role="alert">A reason is required.</p>',
            status_code=422,
        )
    # Pre-check (T-87-27 discipline, mirrors the sibling retry endpoints): short-circuits the common
    # "stale Files-matrix row" case with a clean no-op ack and no DB error round trip. NOT sufficient
    # alone -- see the IntegrityError catch below for the race this cannot close.
    if not await _force_skip_file_exists(session, file_id):
        return _force_skip_no_op_toast(stage)
    # Idempotent additive write (CR-01): re-submitting a force-skip for the same (file, stage) is a NORMAL
    # path — `_force_skip_dialog.html` is not hidden after success — and the UNIQUE(file_id, stage) constraint
    # would turn a bare INSERT into an unhandled IntegrityError → HTTP 500. on_conflict_do_nothing mirrors
    # `insert_ledger_if_absent`: the marker's existence IS the desired end state, so a duplicate is a no-op
    # success. Never clears failed_at (additive-only, T-87-20).
    #
    # Rule 4 + rule 5: the pre-check above is TOCTOU-vulnerable (delete_scan_cascade can remove the file
    # between the SELECT and this INSERT), so the INSERT itself runs inside a SAVEPOINT and a genuine FK
    # violation is caught and converted to the same no-op ack rather than an unhandled 500. Rolling back
    # only the nested scope (not a full session.rollback()) keeps the session usable for the rest of the
    # request.
    stmt = pg_insert(StageSkip).values(file_id=file_id, stage=stage, reason=clean_reason).on_conflict_do_nothing(index_elements=["file_id", "stage"])
    try:
        async with session.begin_nested():
            await session.execute(stmt)
    except IntegrityError:
        logger.info("force_skip_stage race: file deleted between pre-check and insert", file_id=str(file_id), stage=stage)
        return _force_skip_no_op_toast(stage)
    await session.commit()  # get_session does NOT auto-commit (Pitfall 7)
    logger.info("force_skip_stage wrote marker", file_id=str(file_id), stage=stage)
    # HTMX ack: the refreshed stage pill (oob, outerHTML) + the success toast (oob to #toast-container).
    # stage is allowlisted (safe to interpolate); the operator reason is NOT echoed (T-87-21).
    # Re-derive the bucket AFTER the commit rather than hardcoding "skipped": precedence is
    # ``in_flight ≻ done ≻ skipped ≻ failed``, so a stage that is genuinely done must keep reading
    # ✓ done even once a skip marker exists. Reusing the record router's own derivation keeps the
    # pane and the Files matrix on ONE status source (CONSOLE-01).
    buckets = await get_file_stage_buckets(session, file_id)
    toast = (
        f'<div hx-swap-oob="beforeend:#toast-container">'
        f'<div role="status" aria-live="polite" x-data="{{ show: true }}" x-show="show" '
        f'x-init="setTimeout(() => show = false, 5000)" x-transition '
        f'class="rounded bg-gray-800 px-4 py-2 text-sm text-white shadow dark:shadow-none dark:ring-1 dark:ring-phaze-border">'
        f"Skipped {stage} — reason recorded.</div></div>"
    )
    return HTMLResponse(_stage_pill_oob(file_id, stage, buckets.get(stage, "skipped")) + toast)


# The record pane enrich stage labels (the stage loop in record_body.html) — informational text inside the
# pill's aria-label only. Enrich-only, mirroring STAGE_TO_FUNCTION, because non-enrich stages are
# rejected 422 before this is ever reached (D-10).
_ENRICH_STAGE_LABELS = {"metadata": "Meta", "analyze": "Analyze"}


def _stage_pill_oob(file_id: uuid.UUID, stage: str, bucket: str, *, id_prefix: str = "stage-pill") -> str:
    """Render the shared five-bucket pill as an ``hx-swap-oob`` fragment addressed to ONE (file, stage).

    phaze-5p43: ``_force_skip_dialog.html``'s header contract promises the pill flips to ``⊘ skipped``
    "on the NEXT poll tick", but the dialog ships ONLY inside ``record_body.html``, which is a
    deliberate SNAPSHOT (D-02: renders once, no ``hx-trigger="every"``) — so no tick ever comes and the
    pill contradicts the operator's just-taken action for the life of the open record. Adding a
    full-body poll is forbidden (it would clobber in-progress inline edits in the pending-approval
    ``_diff_row`` islands), so the WRITER — which already knows the new bucket — pushes the single pill
    it invalidated, and nothing else.

    ID UNIQUENESS (load-bearing — this repo has a history of duplicate-id OOB bugs: phaze-gzrd,
    phaze-op6f, phaze-7j50): the default ``stage-pill-{stage}-{file_id}`` id is emitted from exactly
    ONE place, ``record_body.html``'s stage loop, once per (stage, file) — six ids for the one open
    record, and ``record_host.html`` hosts at most one record at a time. The id lives on a WRAPPER
    span, not on the pill itself, so ``_stage_pill.html`` stays a pure, id-free token.

    phaze-bgz26: the Files matrix (``files_table_view.html``) needs the SAME shape for its own
    per-row pill, but MUST NOT reuse the ``stage-pill-*`` id space — that id is reserved for the
    (at-most-one) open record pane, and the matrix can render many rows of the same (stage, file)
    simultaneously with the record pane. ``id_prefix`` namespaces the two: callers targeting the
    Files matrix pass ``id_prefix="files-stage-pill"`` (matching the wrapper span
    ``files_table_view.html`` renders around each cell's ``_stage_pill.html`` include), so a
    per-file retry can safely OOB-push into both surfaces from one response with no id collision.

    ``bucket`` is a derived enum value and ``stage`` is allowlisted; the pill template autoescapes.
    """
    pill = templates.get_template("pipeline/partials/_stage_pill.html").render(
        stage_label=_ENRICH_STAGE_LABELS.get(stage, stage),
        bucket=bucket,
    )
    return f'<span id="{id_prefix}-{stage}-{file_id}" class="inline-flex" hx-swap-oob="true">{pill}</span>'


async def _force_skip_file_exists(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """``True`` iff a ``FileRecord`` row named by ``file_id`` currently exists.

    Extracted to a named helper (rather than inlined) so a regression test can force the
    :func:`force_skip_stage` race branch deterministically -- monkeypatching this to always return
    ``True`` reproduces "file existed at pre-check time, vanished before the INSERT" without a second
    concurrent connection.
    """
    result = await session.execute(select(FileRecord.id).where(FileRecord.id == file_id))
    return result.scalar_one_or_none() is not None


def _force_skip_no_op_toast(stage: str) -> HTMLResponse:
    """Benign toast for an unknown or concurrently-deleted ``file_id`` (T-87-27 discipline).

    ``stage`` is allowlisted before this is ever called (``STAGE_TO_FUNCTION`` membership), so it is
    safe to interpolate. Returned by both the pre-check miss and the ``IntegrityError`` race branch in
    :func:`force_skip_stage` so the two failure paths are indistinguishable to the client.
    """
    return HTMLResponse(
        '<div hx-swap-oob="beforeend:#toast-container">'
        '<div role="status" aria-live="polite" x-data="{ show: true }" x-show="show" '
        'x-init="setTimeout(() => show = false, 5000)" x-transition '
        'class="rounded bg-gray-800 px-4 py-2 text-sm text-white shadow dark:shadow-none dark:ring-1 dark:ring-phaze-border">'
        f"File not found — nothing to skip {stage}.</div></div>"
    )


# --------------------------------------------------------------------------------------------------
# Per-file eligibility trace (UI-03 / D-06/D-07): the diagnostic whose absence hid the deadlock. A
# single-row resolve_status/eligible() evaluation (NOT a corpus scan, T-87-23) that names the ONE
# unmet blocker keeping a stage out of the pending set.
# --------------------------------------------------------------------------------------------------

# Display label per stage for the five-pill matrix + trace verdict (the 6->5 remap: tracklist is
# omitted; review renders as Appr, apply as Exec). Mirrors the _stage_matrix partial pill order.
_STAGE_TRACE_LABELS: dict[Stage, str] = {
    Stage.METADATA: "Meta",
    Stage.ANALYZE: "Analyze",
    Stage.PROPOSE: "Prop",
    Stage.REVIEW: "Appr",
    Stage.APPLY: "Exec",
}


async def _one_stage_scalars(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> dict[str, Any]:
    """Read ONE file's per-stage scalars in the DB-free ``resolve_status`` shape (mirrors ``load_scalars``).

    Every read is strictly ``file_id``-scoped (T-87-23 — a single-row evaluation, never a corpus scan).
    """
    func_name = STAGE_TO_FUNCTION.get(stage.value)
    inflight = False
    if func_name is not None:
        ledger_row = (await session.execute(select(SchedulingLedger.key).where(SchedulingLedger.key == f"{func_name}:{file_id}"))).first()
        inflight = ledger_row is not None

    async def _skipped() -> bool:
        found = (await session.execute(select(StageSkip.id).where(StageSkip.file_id == file_id, StageSkip.stage == stage.value))).first()
        return found is not None

    if stage is Stage.ANALYZE:
        arow = (
            await session.execute(select(AnalysisResult.analysis_completed_at, AnalysisResult.failed_at).where(AnalysisResult.file_id == file_id))
        ).first()
        return {"completed_at": arow[0] if arow else None, "failed_at": arow[1] if arow else None, "inflight": inflight, "skipped": await _skipped()}
    if stage is Stage.METADATA:
        mrow = (await session.execute(select(FileMetadata.failed_at).where(FileMetadata.file_id == file_id))).first()
        return {"row_present": mrow is not None, "failed_at": mrow[0] if mrow else None, "inflight": inflight, "skipped": await _skipped()}
    if stage is Stage.TRACKLIST:
        present = (await session.execute(select(Tracklist.id).where(Tracklist.file_id == file_id))).first() is not None
        return {"row_present": present, "failed": False, "inflight": inflight}
    if stage in (Stage.PROPOSE, Stage.REVIEW):
        present = (await session.execute(select(RenameProposal.id).where(RenameProposal.file_id == file_id))).first() is not None
        failed = (
            await session.execute(select(RenameProposal.id).where(RenameProposal.file_id == file_id, RenameProposal.status == "failed"))
        ).first() is not None
        return {"row_present": present, "failed": failed, "inflight": inflight}
    # apply: execution_log joined through proposals (execution_log has NO file_id)
    present = (
        await session.execute(
            select(ExecutionLog.id)
            .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
            .where(RenameProposal.file_id == file_id, ExecutionLog.status == "completed")
        )
    ).first() is not None
    failed = (
        await session.execute(
            select(ExecutionLog.id)
            .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
            .where(RenameProposal.file_id == file_id, ExecutionLog.status == "failed")
        )
    ).first() is not None
    return {"row_present": present, "failed": failed, "inflight": inflight}


async def _has_approved_proposal(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """file_id-scoped single-row probe: does an APPROVED proposal exist? (apply's ELIG-02 gate)."""
    row = (
        await session.execute(
            select(RenameProposal.id).where(RenameProposal.file_id == file_id, RenameProposal.status == ProposalStatus.APPROVED.value)
        )
    ).first()
    return row is not None


async def _eligibility_trace_context(session: AsyncSession, file_id: uuid.UUID, stage: Stage) -> dict[str, Any]:
    """Evaluate ``eligible()`` for ONE file/stage and build the named-conjunct trace context (UI-03, D-06/D-07).

    Loads the stage's own status plus its ``ELIGIBILITY_DAG`` upstream statuses (single-row reads),
    evaluates the REAL ``eligible()`` (the scheduler's source of truth) in Python, and names the single
    unmet blocker. Enrich stages have no upstream, so ``upstream met?`` is vacuously true. The upstream
    conjunct STRICTLY mirrors ``eligible()`` (upstream must be DONE): under the OQ-1 SCOPE-MINIMAL
    resolution a force-skipped enrich upstream does NOT unblock its downstream (Phase 90), so a SKIPPED
    upstream is rendered as still-gating — a lenient "skipped = met" display would make the trace claim a
    downstream is eligible when the scheduler permanently gates it (the deadlock UI-03 exists to expose).
    NOT a corpus query (T-87-23).
    """
    label = _STAGE_TRACE_LABELS.get(stage, stage.value)
    upstreams = ELIGIBILITY_DAG[stage]
    statuses: dict[Stage, Status] = {stage: resolve_status(stage, await _one_stage_scalars(session, stage, file_id))}
    for u in upstreams:
        statuses[u] = resolve_status(u, await _one_stage_scalars(session, u, file_id))
    has_approved = await _has_approved_proposal(session, file_id) if stage is Stage.APPLY else False

    target = statuses[stage]
    is_done = target == Status.DONE
    is_in_flight = target == Status.IN_FLIGHT
    is_skipped = target == Status.SKIPPED
    is_terminal_fail = stage in ELIGIBLE_AFTER_FAILURE and target == Status.FAILED and not ELIGIBLE_AFTER_FAILURE[stage]

    if stage is Stage.APPLY:
        # apply is gated on an APPROVED proposal (ELIG-02), NOT on bare done(review).
        upstream_met = has_approved
        upstream_phrase = "approved proposal exists" if has_approved else "no approved proposal"
    elif upstreams:
        # STRICT mirror of eligible()'s downstream check (upstream must be DONE). Under the OQ-1
        # SCOPE-MINIMAL resolution a force-skipped enrich upstream does NOT unblock its downstream
        # (deferred to Phase 90), so a SKIPPED upstream stays gating — the trace names it honestly
        # rather than claiming a downstream is eligible when the scheduler permanently gates it.
        unmet = [u for u in upstreams if statuses[u] != Status.DONE]
        upstream_met = not unmet
        if upstream_met:
            upstream_phrase = "all upstream done"
        elif statuses[unmet[0]] == Status.SKIPPED:
            upstream_phrase = f"{unmet[0].value} skipped — downstream stays gated (Phase 90)"
        else:
            upstream_phrase = f"{unmet[0].value} not done"
    else:  # enrich: empty upstream is vacuously met (ELIG-01 independence)
        upstream_met = True
        upstream_phrase = "no upstream (enrich stage)"

    # Verdict is the REAL eligible() — the single source of truth the scheduler uses. A diagnostic that
    # diverged from it would hide the very deadlock UI-03 exists to expose.
    is_eligible = eligible(statuses, stage, has_approved_proposal=has_approved)

    # The single blocker follows eligible()'s short-circuit order (skipped folds onto the settled done? line).
    blocker = ""
    if not is_eligible:
        if is_done or is_skipped:
            blocker = "done"
        elif is_in_flight:
            blocker = "inflight"
        elif is_terminal_fail:
            blocker = "terminal"
        elif not upstream_met:
            blocker = "upstream"

    if is_done:
        done_ok, done_phrase = True, "already done"
    elif is_skipped:
        done_ok, done_phrase = True, "force-skipped (⊘) — recorded as skipped, not done"
    else:
        done_ok, done_phrase = False, "not done"

    conjuncts = [
        {"question": "done?", "ok": done_ok, "phrase": done_phrase, "blocker": blocker == "done"},
        {
            "question": "in-flight?",
            "ok": is_in_flight,
            "phrase": "currently running" if is_in_flight else "not running",
            "blocker": blocker == "inflight",
        },
        {"question": "upstream met?", "ok": upstream_met, "phrase": upstream_phrase, "blocker": blocker == "upstream"},
        {
            "question": "terminal fail?",
            "ok": is_terminal_fail,
            "phrase": "terminal failure — retry is manual" if is_terminal_fail else "no terminal failure",
            "blocker": blocker == "terminal",
        },
    ]
    verdict = f"{label} — eligible (in the pending set)" if is_eligible else f"{label} — NOT eligible"
    return {"stage_label": label, "eligible": is_eligible, "verdict": verdict, "conjuncts": conjuncts, "unavailable": False}


@router.get("/pipeline/files/{file_id}/trace/{stage}", response_class=HTMLResponse)
async def eligibility_trace(
    request: Request,
    file_id: uuid.UUID,
    stage: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Per-file, per-stage eligibility trace (UI-03) — the diagnostic whose absence hid the deadlock.

    Renders ``_eligibility_trace.html`` under the clicked pill: a verdict line plus the four named
    conjuncts (``done?`` · ``in-flight?`` · ``upstream met?`` · ``terminal fail?``) with the single
    unmet blocker highlighted. It is a single-row ``resolve_status``/``eligible()`` evaluation
    (T-87-23 — never a corpus scan) and degrades to "Trace unavailable this tick." on any error, so a
    poll never 500s.
    """
    stage_enum: Stage | None
    try:
        stage_enum = Stage(stage)
    except ValueError:
        stage_enum = None
    context: dict[str, Any] = {"request": request}
    if stage_enum is None:
        context["unavailable"] = True
        return templates.TemplateResponse(request=request, name="pipeline/partials/_eligibility_trace.html", context=context)
    try:
        context.update(await _eligibility_trace_context(session, file_id, stage_enum))
    except Exception:
        logger.warning("eligibility_trace degraded", file_id=str(file_id), stage=stage, exc_info=True)
        context["unavailable"] = True
    return templates.TemplateResponse(request=request, name="pipeline/partials/_eligibility_trace.html", context=context)


@router.post("/pipeline/files/{file_id}/deepen", response_class=HTMLResponse)
async def deepen_analysis(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: re-analyze ONE sampled file at the full (unbounded) window budget.

    Phase 43 strides long files to bound per-file cost, leaving a "sampled" result. This
    "deepen analysis" re-trigger re-enqueues that single file's ``process_file`` job with
    ``fine_cap=0`` / ``coarse_cap=0`` -- the sentinel that ``analysis._stride_to_cap`` treats
    as the analyze-ALL-windows no-op (D-04) -- so the operator gets a full re-analysis on
    demand.

    Incident guards (D-05, MANDATORY):
    - Routing: the queue is resolved via ``enqueue_router.resolve_queue_for_task`` so the job
      lands on the per-agent ``process_file`` queue, NEVER the consumer-less default queue
      (Phase-30 misrouting incident). ``process_file`` is an AGENT_TASK; if no agent is online
      ``NoActiveAgentError`` is caught and the endpoint returns a fragment WITHOUT enqueuing --
      it never falls through to the default queue.
    - Payload: the re-enqueue funnels through ``enqueue_process_file`` which builds the COMPLETE
      ``ProcessFilePayload`` (v4.0.8 truncation incident -- a ``file_id``-only payload would
      dead-letter under ``extra="forbid"``).
    - Dedup: ``enqueue_process_file`` uses the deterministic ``process_file:<file_id>`` key, so a
      re-deepen of a file with a live in-flight job dedups to a no-op (D-05); re-deepening an
      already-ANALYZED file with no live job is a fresh enqueue.
    - Collision classification (phaze-ewen): a ``None`` return is NOT unconditionally "already in
      flight". SAQ's ``_enqueue`` upsert only overwrites a conflicting key whose status is in
      ``('aborted', 'complete', 'failed')`` -- a dead ``aborting``/``failed``/``aborted`` row, or a
      claimed-but-worker-dead ``active`` row past its timeout, holds the key forever without ever
      processing the file. ``classify_process_file_collision`` (``services.analysis_enqueue``)
      distinguishes the two so a BLOCKED collision renders an honest terminal fragment (and is
      logged) instead of the unconditional "Queued — starting deepen…" + an eternal 2s poll that
      will never see a matching ``analysis_completed_at``/``failed_at`` (the file's frozen
      sampled-run counters never satisfy ``deepen_progress``'s non-terminal branches either).

    The typed ``uuid.UUID`` path param yields a 422 on a malformed id; an unknown (well-formed)
    id resolves to ``None`` and returns a not-found fragment -- never a raw 500 (T-44-10).
    """
    # quick-260707-cvz: capture the click epoch BEFORE the enqueue so the poll's completion
    # predicate (analysis_completed_at > requested_at) only trips on a re-run stamped AFTER
    # this click -- a stale pre-click sampled result never shows as "complete".
    since = datetime.now(UTC).timestamp()

    result = await session.execute(select(FileRecord).where(FileRecord.id == file_id))
    file = result.scalar_one_or_none()

    not_found = file is None
    no_active_agent = False
    already_in_flight = False
    blocked = False

    if file is not None:
        try:
            # phaze-c9w9: route to the FILE's owning agent, never the most-recently-seen fileserver.
            routed = await enqueue_router.resolve_queue_for_task("process_file", request.app.state, session, agent_id=file.agent_id)
        except enqueue_router.NoActiveAgentError:
            # Do NOT fall through to the default queue (Phase-30 guard) -- surface gracefully.
            no_active_agent = True
        else:
            # process_file is an AGENT_TASK -- resolve always returns a non-None agent_id;
            # cast narrows str | None -> str for ProcessFilePayload.agent_id.
            agent_id = cast("str", routed.agent_id)
            # fine_cap=0 / coarse_cap=0 -> _stride_to_cap no-op -> analyze ALL windows (unbounded
            # deepen, D-04). The single funnel guarantees the full payload + deterministic key.
            job = await enqueue_process_file(routed.queue, file, agent_id, settings.models_path, fine_cap=0, coarse_cap=0)
            if job is None:
                # Deterministic-key collision -- classify it rather than assuming "in flight"
                # (phaze-ewen). A dead job holding the key means this deepen was silently dropped.
                existing = await routed.queue.job(process_file_job_key(file.id))
                if classify_process_file_collision(existing) == "blocked":
                    blocked = True
                    logger.warning(
                        "deepen_analysis: deterministic key held by a dead job -- deepen dropped",
                        file_id=str(file.id),
                        key=process_file_job_key(file.id),
                    )
                else:
                    already_in_flight = True

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/deepen_response.html",
        context={
            "request": request,
            "not_found": not_found,
            "no_active_agent": no_active_agent,
            "already_in_flight": already_in_flight,
            "blocked": blocked,
            # Consumed ONLY by the success/already-in-flight branches' bootstrap poller
            # (guards/branches above are unchanged). since is a numeric float threaded into the
            # poll URL.
            "file_id": file_id,
            "since": since,
        },
    )


@router.get("/pipeline/files/{file_id}/deepen-progress", response_class=HTMLResponse)
async def deepen_progress(
    request: Request,
    file_id: uuid.UUID,
    since: float,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX poll target for the "Deepen analysis" progress surface (quick-260707-cvz).

    ``since`` is the deepen-click epoch (seconds, float) threaded through the poll URL. It is a
    typed query param -- FastAPI 422s a non-numeric value (T-cvz-01) and it is used ONLY in a
    datetime compare, never rendered raw. The rendered counts are numeric ints (None-guarded to
    0), never essentia strings (T-cvz-02). An unknown file_id returns a benign "gone" fragment,
    never a 500 (T-cvz-04).

    phaze-zut8: being a valid ``float`` does not make ``since`` a valid Unix timestamp -- NaN,
    +/-infinity, and magnitudes beyond ``datetime``'s year range all raise ``ValueError`` /
    ``OverflowError`` out of ``datetime.fromtimestamp``. Per request_guards.py contract rule 1
    this is an ENVELOPE failure (a single scalar, nothing partial to do), so it is rejected with
    ``422`` rather than escaping as the 500 this docstring's "never a 500" promise forbids.

    COMPLETION PREDICATE (timestamp-gated): a re-run is complete only when ``put_analysis`` has
    stamped ``analysis_completed_at`` AFTER this click (``> requested_at``). A stale pre-click
    sampled result has ``completed_at <= requested_at`` and is NOT complete -- killing the
    misleading-complete edge. ``post_analysis_progress`` is counter-only and never touches
    ``analysis_completed_at``, so a re-deepen of an already-ANALYZED file keeps its OLD
    completed_at until the fresh ``put_analysis`` lands.

    FAILURE PREDICATE (phaze-l9nc, timestamp-gated, mirrors the completion predicate): a re-run
    is failed only when ``report_analysis_failed`` has stamped ``failed_at`` AFTER this click
    (``> requested_at``). Without this, a deepen that fails leaves the frozen fine_windows
    counters (fine_done < fine_total) satisfying ``running`` forever -- the fragment never
    reaches a terminal branch and polls every 2s for the life of the tab. A stale pre-click
    ``failed_at`` (from an unrelated earlier failure) is NOT this re-run's failure and must not
    short-circuit an in-flight retry.

    State machine (evaluated in order): malformed ``since`` -> 422 (terminal, no query run);
    missing file -> gone (terminal); failed predicate -> failed (terminal); complete predicate ->
    complete (terminal); fine_total truthy AND fine_done < fine_total -> running (poll);
    otherwise -> queued/starting (poll).
    """
    try:
        requested_at = datetime.fromtimestamp(since, tz=UTC)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(
            status_code=MALFORMED_PAYLOAD_STATUS,
            detail=f"since is not a valid timestamp: {exc}",
        ) from exc

    file_result = await session.execute(select(FileRecord).where(FileRecord.id == file_id))
    file = file_result.scalar_one_or_none()

    if file is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/deepen_progress.html",
            context={
                "request": request,
                "gone": True,
                "failed": False,
                "complete": False,
                "running": False,
                "fine_done": 0,
                "fine_total": 0,
                "file_id": file_id,
                "since": since,
            },
        )

    analysis_result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    analysis = analysis_result.scalar_one_or_none()

    # phaze-l9nc: read failed_at (never checked before this fix) so a deepen that terminally
    # fails halts the poller instead of looping the running/queued branches forever. Gated on
    # `> requested_at` for the same reason `complete` is: a stale pre-click failed_at (an
    # unrelated earlier failure on this file, since cleared by a successful re-analysis or still
    # pending a fresh retry) must not be read as THIS click's outcome.
    #
    # Known interaction (noted, not fixed here -- out of scope, phaze-ts1d owns agent_analysis.py):
    # report_analysis_failed's CAS guard (`WHERE analysis_completed_at IS NULL`) makes the failure
    # stamp a no-op whenever the deepen target already has a completed analysis -- exactly the
    # common "sampled" case the Deepen button is offered on. In that case failed_at is never set
    # for this re-run, `failed` stays False, and the fragment falls through to `running`/`queued`
    # on the frozen fine_windows counters. This predicate still correctly halts the poller for
    # every case where failed_at IS stamped (e.g. a deepen on a file with no prior completed
    # analysis); it does not regress or worsen the CAS-guarded case, which was already unable to
    # reach `complete` either.
    failed = analysis is not None and analysis.failed_at is not None and analysis.failed_at > requested_at

    complete = (not failed) and analysis is not None and analysis.analysis_completed_at is not None and analysis.analysis_completed_at > requested_at

    fine_done = (analysis.fine_windows_analyzed or 0) if analysis is not None else 0
    fine_total = (analysis.fine_windows_total or 0) if analysis is not None else 0
    running = (not failed) and (not complete) and fine_total > 0 and fine_done < fine_total

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/deepen_progress.html",
        context={
            "request": request,
            "gone": False,
            "failed": failed,
            "complete": complete,
            "running": running,
            "fine_done": fine_done,
            "fine_total": fine_total,
            "file_id": file_id,
            "since": since,
        },
    )


@router.post("/pipeline/proposals", response_class=HTMLResponse)
async def trigger_proposals_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger proposal generation and return response fragment."""
    # Per D-02: convergence gate via the shared, deterministically-sorted pending-set helper
    # (D-03 anti-drift) -- same sorted batches as the API trigger + recovery, so keys align.
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


async def _enqueue_extraction_jobs(queue: Any, files: list[FileRecord], agent_id: str) -> None:
    """Background coroutine to enqueue extract_file_metadata jobs with the COMPLETE payload.

    The agent worker validates ``ExtractMetadataPayload`` with ``extra="forbid"`` and four
    required fields (file_id, original_path, file_type, agent_id). A ``file_id``-only enqueue
    therefore fails validation and dead-letters EVERY job -- the same defect that bit the
    pre-Phase-30 ``process_file`` path (see ``analysis_enqueue.enqueue_process_file``) and the
    v4.0.8 payload incident. D-06 removed the only other producer (the agent file-upsert
    auto-enqueue), making this manual trigger the SOLE metadata producer, so the full payload
    MUST be built here. ``model_dump(mode="json")`` serializes the UUID as a string so the
    worker's ``model_validate`` accepts it. The deterministic key
    (``extract_file_metadata:<file_id>``) is applied centrally by the ``before_enqueue`` hook
    (35-01), so no explicit ``key=`` is set here.
    """
    for f in files:
        payload = ExtractMetadataPayload(
            file_id=f.id,
            original_path=f.original_path,
            file_type=f.file_type,
            agent_id=agent_id,
        )
        await queue.enqueue("extract_file_metadata", **payload.model_dump(mode="json"))


@router.post("/api/v1/extract-metadata")
async def trigger_metadata_extraction(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue extract_file_metadata jobs for eligible music/video files (READ-01 cutover).

    Per D-04: originally queued every music/video file regardless of status, for backfill. READ-01
    replaced that state-agnostic selector with the DERIVED pending set (see
    :func:`get_metadata_pending_files`, ``eligible_clause(Stage.METADATA)``): a file whose metadata
    is genuinely done is excluded, while a not-started or failed one stays eligible (auto-retry).
    Per D-09: manual API endpoint for re-extraction.
    """
    files = await get_metadata_pending_files(session)

    if not files:
        return {"enqueued": 0, "message": "No music/video files found"}

    try:
        # phaze-c9w9: group by each file's OWNING agent -- never one most-recently-seen pick
        # for the whole set (owner-offline files are skipped, not rerouted).
        routed_groups, skipped_files = await enqueue_router.resolve_queues_for_owned_files("extract_file_metadata", request.app.state, session, files)
    except enqueue_router.NoActiveAgentError:
        return {"enqueued": 0, "message": _NO_ACTIVE_AGENT_MESSAGE}

    for routed, group in routed_groups:
        task = asyncio.create_task(_enqueue_extraction_jobs(routed.queue, group, cast("str", routed.agent_id)))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    enqueued = sum(len(group) for _, group in routed_groups)
    message = f"Enqueued {enqueued} files for metadata extraction"
    if skipped_files:
        message += f" ({len(skipped_files)} skipped: owning agent offline)"
    return {"enqueued": enqueued, "message": message}


@router.post("/pipeline/extract-metadata", response_class=HTMLResponse)
async def trigger_extraction_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger metadata extraction and return response fragment."""
    files = await get_metadata_pending_files(session)
    count = 0
    no_active_agent = False

    if files:
        try:
            # phaze-c9w9: group by each file's OWNING agent -- never one most-recently-seen pick.
            routed_groups, skipped_files = await enqueue_router.resolve_queues_for_owned_files(
                "extract_file_metadata", request.app.state, session, files
            )
        except enqueue_router.NoActiveAgentError:
            no_active_agent = True
        else:
            if skipped_files:
                logger.warning("trigger_extraction_ui: owning agent offline -- files skipped", skipped=len(skipped_files))
            for routed, group in routed_groups:
                count += len(group)
                task = asyncio.create_task(_enqueue_extraction_jobs(routed.queue, group, cast("str", routed.agent_id)))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "metadata extraction", "count": count, "no_active_agent": no_active_agent},
    )


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
    """
    for tl in tracklists:
        await queue.enqueue("match_tracklist_to_discogs", tracklist_id=str(tl.id))


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
    2. A single ``drain_tracklists`` job is enqueued with ``limit=1`` so the operator sees a real
       lookup start now rather than only ever affecting some later, unscheduled run.
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
        raise HTTPException(status_code=404, detail="file not found")

    queued = False
    if review.tracklist is None and review.actionable:
        await flag_file_for_lookup(session, file_id)
        await session.commit()
        routed = await enqueue_router.resolve_queue_for_task("drain_tracklists", request.app.state, session)
        await routed.queue.enqueue("drain_tracklists", limit=1)
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
        raise HTTPException(status_code=404, detail="file not found")

    refreshed = False
    if review.tracklist is not None:

        @contextlib.asynccontextmanager
        async def _session_factory() -> AsyncIterator[AsyncSession]:
            yield session

        outcome = await refresh_tracklists({"async_session": _session_factory}, file_ids=[str(file_id)])
        refreshed = bool(outcome["refreshed"])
        if refreshed:
            routed = await enqueue_router.resolve_queue_for_task("drain_tracklists", request.app.state, session)
            await routed.queue.enqueue("drain_tracklists", limit=1)
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
        raise HTTPException(status_code=404, detail="file not found")

    await unflag_file(session, file_id)
    await session.commit()

    review = await get_file_tracklist_review(session, file_id)
    return templates.TemplateResponse(
        request=request,
        name="record/partials/_tracklist_review_body.html",
        context={"request": request, "file_id": file_id, "review": review, "just_queued": False, "just_refreshed": False},
    )


# --- Drain progress fragment + manual slice trigger (phaze-fq9h.8) ---


@router.get("/pipeline/tracklist-drain-status", response_class=HTMLResponse)
async def tracklist_drain_status_ui(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """HTMX fragment: queue depth, throughput vs the daily ceiling, and an honest ETA.

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

    @contextlib.asynccontextmanager
    async def _session_factory() -> AsyncIterator[AsyncSession]:
        yield session

    ctx: dict[str, Any] = {"async_session": _session_factory}
    status = await tracklist_drain_status(ctx)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_tracklist_drain_status.html",
        context={"request": request, "status": status, "daily_ceiling": DAILY_LOOKUP_CEILING},
    )


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


# --- Manual recovery endpoint (Phase 42, D-02/D-05) ---


_recovery_state: dict[str, Any] = {"running": False, "result": None, "failed": False}
"""In-process outcome of the MOST RECENT operator-triggered recovery (phaze-71nz).

``POST /pipeline/recover`` fires recovery as a background task and returns immediately, so the POST
response can only ever say "started". That was the whole shape of the 2026-07-31 defect at the
operator layer: a run that replayed 430 ``s3_upload`` rows into guaranteed-403 jobs returned the
IDENTICAL 200 + "Recovery started — re-enqueuing any orphaned work across all stages" as a run that
recovered cleanly. Nothing the operator could see distinguished them.

This cell is what ``GET /pipeline/recover/status`` polls so the FINAL tally -- specifically its
``unreplayable`` total, the count of orphaned work knowingly NOT re-enqueued -- reaches the operator
who pressed the button. Shape::

    {"running": bool, "result": <recover_orphaned_work return> | None, "failed": bool}

Deliberately process-local and non-durable: it is a UI echo of one click, not a record. The durable
record is the ledger (the un-recovered rows survive) plus the controller logs, both of which now name
the skipped stages explicitly. The API runs as a SINGLE uvicorn process (``Dockerfile`` CMD sets no
``--workers``), so the poll always reaches the process that owns the task; were that to change, this
would need to move to Redis and the poll would degrade to "no recent recovery", never to a wrong
answer.
"""


async def _run_recovery(ctx: dict[str, Any]) -> None:
    """Background coroutine: run the gated all-stages recovery producer (force=True).

    Calls the SAME :func:`recover_orphaned_work` producer the controller startup hook runs
    (Phase 42, D-03), so the manual and automatic recovery paths cannot drift. ``force=True``
    bypasses ONLY the no-op queue-loss DETECT gate (this is the operator-driven cold-boot
    safety net, D-05) -- it never bypasses the per-item deterministic-key dedup, so a forced
    reconcile over a live queue collapses every still-in-flight item to a skipped no-op and
    can NEVER double the backlog (Phase-32 doubling class is closed).

    Per-row failures inside ``recover_orphaned_work`` are already isolated and tallied under
    ``errored`` (phaze-o1xx) rather than raising, so this normally just logs the final tally. The
    ``try/except`` here is the LAST line of defense for a failure the producer itself cannot
    isolate (e.g. the session/DETECT-gate read at the top of the function): the previous
    fire-and-forget ``asyncio.create_task`` had no done-callback and no `except`, so that exception
    was never retrieved -- the operator's HTMX response already said "recovery started" and nothing
    else ever surfaced the failure. Log it here so a failed forced recovery is at least visible in
    the controller logs instead of silently vanishing.

    phaze-71nz: the outcome is ALSO published to :data:`_recovery_state` so the operator who pressed
    the button can see it. The controller log was the only surface before, and an operator driving an
    incident from the UI does not have it -- which is how a run that burned an entire stage into
    ``failed`` could read as an unqualified success. ``finally`` clears ``running`` on every path, so
    a crash cannot wedge the status fragment polling forever.
    """
    try:
        result = await recover_orphaned_work(ctx, force=True)
    except Exception:
        logger.exception("manual recovery trigger failed -- operator saw 'recovery started' with no further result surfaced (phaze-o1xx)")
        _recovery_state.update(running=False, result=None, failed=True)
        return
    _recovery_state.update(running=False, result=result, failed=False)
    logger.info(
        "manual recovery trigger complete",
        detected_loss=result["detected_loss"],
        unreplayable=result.get("unreplayable", 0),
        stages=result["stages"],
    )


@router.post("/pipeline/recover", response_class=HTMLResponse)
async def trigger_recover_ui(request: Request) -> HTMLResponse:
    """HTMX endpoint: manually trigger the gated all-stages recovery pass (Phase 42, D-02/D-05).

    The global DAG "Recover" button posts here. It builds a worker-shaped ``ctx`` from the API
    app -- the module-level :data:`phaze.database.async_session` sessionmaker (same DB as the
    ``saq_jobs`` broker), the lifespan-created ``app.state.controller_queue`` (controller stages),
    and ``app.state.task_router`` (per-agent stages) -- and schedules :func:`recover_orphaned_work`
    with ``force=True`` as a fire-and-forget background task (same ``_background_tasks`` discipline
    as every other pipeline trigger, so a large reconcile never blocks the HTTP response). Because
    the producer runs in the background, this returns immediately with a "recovery started" fragment
    rather than the final per-stage counts. The endpoint calls the SAME producer as controller
    startup, so the manual and automatic recovery paths cannot drift (D-03), and the deterministic-key
    dedup keeps a forced reconcile idempotent (T-42-06/T-42-07) -- it can never 500 on a healthy queue.

    phaze-71nz: the returned fragment now POLLS :func:`recover_status_ui` instead of being the last
    word. "Recovery started" is still true at the instant it is rendered, but it must not remain the
    only thing the operator ever sees -- a run that knowingly leaves a stage un-recovered has to say
    so. ``_recovery_state`` is armed to ``running`` HERE (not inside the background task) so the very
    first poll cannot race in ahead of the task starting and report the PREVIOUS run's tally.
    """
    ctx: dict[str, Any] = {
        "async_session": async_session,
        "queue": request.app.state.controller_queue,
        "task_router": request.app.state.task_router,
    }
    _recovery_state.update(running=True, result=None, failed=False)
    task = asyncio.create_task(_run_recovery(ctx))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/recover_response.html",
        context={"request": request},
    )


@router.get("/pipeline/recover/status", response_class=HTMLResponse)
async def recover_status_ui(request: Request) -> HTMLResponse:
    """HTMX poll target: the OUTCOME of the most recent operator-triggered recovery (phaze-71nz).

    The "Recover" POST fires recovery in the background and can only report that it started, so this
    is where the run's actual tally reaches the operator. It renders one of four states from
    :data:`_recovery_state`:

    - **running** -- keeps polling;
    - **failed** -- recovery raised; the operator is told to check the logs and retry;
    - **complete with ``unreplayable > 0``** -- the state this bead exists for. Some orphaned work was
      DELIBERATELY not re-enqueued (its stored payload was time-limited and could not be regenerated),
      so those files are NOT covered by the run. Rendered as a distinct warning naming the stages,
      never as the plain success copy;
    - **complete, all clear** -- the per-stage re-enqueued / already-running totals.

    Read-only and idempotent: it touches no queue and no database, so the poll is free to run at
    whatever cadence the fragment sets and safe to hit directly.
    """
    result = _recovery_state["result"]
    stages: dict[str, dict[str, int]] = (result or {}).get("stages", {}) or {}
    skipped_stages = sorted(fn for fn, tally in stages.items() if tally.get("unreplayable"))
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/recover_status.html",
        context={
            "request": request,
            "running": _recovery_state["running"],
            "failed": _recovery_state["failed"],
            "result": result,
            "unreplayable": (result or {}).get("unreplayable", 0),
            "unreplayable_stages": skipped_stages,
            "reenqueued": sum(tally.get("reenqueued", 0) for tally in stages.values()),
            "already_running": sum(tally.get("skipped", 0) for tally in stages.values()),
            "errored": sum(tally.get("errored", 0) for tally in stages.values()),
        },
    )
