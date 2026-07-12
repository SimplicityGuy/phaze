"""Pipeline orchestration service -- stage counts and file queries."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import TYPE_CHECKING, Any

from saq.utils import now as saq_now
from sqlalchemy import String, and_, cast, distinct, exists, func, or_, select, text
from sqlalchemy.orm import aliased
import structlog

from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.enums.stage import Stage, Status
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.pipeline_stage_control import PipelineStageControl
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.enqueue_router import LANES
from phaze.services.stage_status import awaiting_candidate_clause, dedup_resolved_clause, eligible_clause, failed_clause, stage_status_case
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select


logger = structlog.get_logger(__name__)


# Music + video file types -- the shared denominator for the per-file parallel
# stages (Metadata/Fingerprint/Analyze). Mirrors the filter the trigger endpoints
# use at routers/pipeline.py:318-319 so the dashboard denominator matches the set
# of files those stages are actually enqueued for.
MUSIC_VIDEO_TYPES = [ext.lstrip(".") for ext, cat in EXTENSION_MAP.items() if cat in (FileCategory.MUSIC, FileCategory.VIDEO)]


# T-82-A1 (double-dispatch guard): a file whose ``cloud_job`` row is in any of these ACTIVE (non-terminal)
# statuses is currently being handled by the cloud path and MUST NOT be a local analyze candidate. The
# analyze-set trace (82-02 SUMMARY): the cloud hand-off enqueues ``push_file`` (never ``process_file``)
# and holds ``AWAITING_CLOUD``/``PUSHING`` with NO ``process_file:<id>`` scheduling-ledger row, so
# ``~inflight_clause(ANALYZE)`` alone does NOT exclude a cloud-dispatched file -- this explicit conjunct
# is load-bearing. ``FAILED`` is deliberately EXCLUDED: a terminally-failed cloud burst with no
# ``AnalysisResult`` is a legitimate local-retry candidate (the spill/recovery paths re-home it). A
# genuinely-done cloud burst (``SUCCEEDED`` with a landed ``AnalysisResult``) is already excluded by
# ``~done_clause`` inside ``eligible_clause``; listing ``SUCCEEDED`` here is the belt-and-suspenders that
# also covers the compute ``PUSHED`` window (``cloud_job.status='succeeded'`` while analysis still runs on
# the agent, before its ``process_file`` ledger row lands).
_ACTIVE_CLOUD_STATUSES: tuple[str, ...] = (
    CloudJobStatus.AWAITING.value,
    CloudJobStatus.UPLOADING.value,
    CloudJobStatus.UPLOADED.value,
    CloudJobStatus.SUBMITTED.value,
    CloudJobStatus.RUNNING.value,
    CloudJobStatus.SUCCEEDED.value,
)


# The pipeline stages in order, for display
PIPELINE_STAGES = [
    FileState.DISCOVERED,
    FileState.METADATA_EXTRACTED,
    FileState.FINGERPRINTED,
    FileState.ANALYZED,
    FileState.PROPOSAL_GENERATED,
    FileState.APPROVED,
    FileState.DUPLICATE_RESOLVED,
    FileState.EXECUTED,
]


# NOTE (Phase 82, D-05/READ-02): ``get_pipeline_stats`` -- the linear per-``FileRecord.state`` grouped
# counter -- was REMOVED here. The stats path no longer groups by (or reads) ``FileRecord.state``: its
# three former callers
# (``routers/pipeline.py`` ``_build_dag_context`` / ``build_dashboard_context`` /
# ``pipeline_stats_partial``) now derive the seven consumed keys from :func:`get_stage_progress`'s
# output-table counts (``discovered→discovery.done``, ``metadata_extracted→metadata.done``,
# ``fingerprinted→fingerprint.done``, ``analyzed→analyze.done``, ``proposal_generated→proposals.done``,
# ``approved→execute.total``, ``executed→execute.done``). ``PIPELINE_STAGES`` (above) is retained: it is
# still consumed by the ANALYSIS_FAILED-bucket invariant test + the ``get_analysis_failed_count``
# docstring, and does NOT read state on the hot poll path.


# --- Scanned / deduped / unique reconciliation (quick 260622-i0w) -----------------------
#
# The Discovery DAG node shows COUNT(files) while the agent scan total is SUM(scan_batches
# .total_files). The two legitimately differ: an agent walks total_files paths but each path
# upserts onto the NFC-normalized composite unique key (agent_id, original_path), so duplicate
# / normalization-collision walks collapse onto an existing row instead of inserting a new one.
# That gap is "deduped", NOT lost work. These helpers compute it degrade-safely so the apparent
# bug reads as a self-explaining reconciliation.
#
# LOCKED formulas:
#   scanned   = SUM over agents of (each agent's MOST RECENT completed ScanBatch).total_files
#               (re-scan-safe: a re-scan makes a NEW completed batch; summing ALL would inflate).
#   deduped   = max(0, scanned - discovery_done); discovery_done = COUNT(all FileRecord rows).
#   per-agent = max(0, agent_latest_completed.total_files - COUNT(files WHERE agent_id = X)).
# A None scanned (no completed batches OR a DB error) is the "hide the whole line" sentinel,
# deliberately distinct from a real 0.


def deduped_count(scanned: int | None, unique: int) -> int | None:
    """Pure reconciliation arithmetic: None passthrough + clamp-to-zero (no I/O, unit-testable).

    Returns None when ``scanned`` is None (the UI then HIDES the reconciliation line — a None
    scan total is "unavailable", not "zero deduped"). Otherwise returns ``max(0, scanned - unique)``
    so the deduped count can never go negative when more files exist than the latest scan walked
    (a stale/older scan total against a freshly-grown file table).
    """
    if scanned is None:
        return None
    return max(0, scanned - unique)


async def get_scanned_total(session: AsyncSession) -> int | None:
    """SUM each agent's LATEST completed ``ScanBatch.total_files``, degrading to None on any error.

    Re-scan-safe: a re-scan creates a NEW completed batch for the same agent, so summing ALL
    completed batches would double-count. Instead a window function ranks each agent's completed
    batches by ``created_at`` DESC and only ``rn == 1`` (the most recent) is summed.

    Returns None (NOT 0) both when there are no completed batches and on any DB error: None is the
    "hide the reconciliation" sentinel, distinct from a genuine scanned total of 0. Mirrors the
    :func:`_safe_count` / :func:`get_stage_controls` degrade discipline (log → guarded rollback →
    sentinel) so it never raises into the 5s dashboard poll.
    """
    try:
        ranked = (
            select(
                ScanBatch.total_files.label("total_files"),
                func.row_number().over(partition_by=ScanBatch.agent_id, order_by=ScanBatch.created_at.desc()).label("rn"),
            )
            .where(ScanBatch.status == ScanStatus.COMPLETED.value)
            .subquery()
        )
        total = (await session.execute(select(func.sum(ranked.c.total_files)).where(ranked.c.rn == 1))).scalar()
        return int(total) if total is not None else None
    except Exception:
        logger.warning("scanned_total_degraded", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("scanned_total_rollback_failed", exc_info=True)
        return None


async def get_global_reconciliation(session: AsyncSession) -> dict[str, int | None]:
    """Return ``{"scanned": int|None, "deduped": int|None}`` for the Discovery DAG-node subtitle.

    ``scanned`` is :func:`get_scanned_total`; when it degrades to None the whole reconciliation is
    the hidden state ``{"scanned": None, "deduped": None}`` (no DB work attempted). Otherwise
    ``discovery_done`` is COUNT(ALL FileRecord rows) via :func:`_safe_count` — note total_files
    counts only extractable music/video while discovery_done counts ALL rows; the LOCKED formula is
    still ``scanned - discovery_done`` (the gap IS the dedup/collision count). ``deduped`` clamps to
    0 when discovery_done ≥ scanned. Both reads degrade independently, so the dict never raises into
    the 5s poll.
    """
    scanned = await get_scanned_total(session)
    if scanned is None:
        return {"scanned": None, "deduped": None}
    # discovery_done counts ALL rows (no file_type filter) so the subtraction is consistent with the
    # Discovery node's COUNT(files); total_files counts only music/video, but scanned - all-rows is
    # the LOCKED dedup formula.
    discovery_done = await _safe_count(session, select(func.count(FileRecord.id)), node="reconcile_discovery")
    return {"scanned": scanned, "deduped": deduped_count(scanned, discovery_done)}


async def get_agent_reconciliations(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Per-agent ``{agent_id: {"scanned", "unique", "deduped"}}``, degrading to ``{}`` on any error.

    For each agent with a latest completed batch: ``scanned`` = that batch's ``total_files`` (re-scan
    -safe via the same ``row_number()`` rank as :func:`get_scanned_total`), ``unique`` = COUNT of the
    agent's FileRecord rows, ``deduped`` = ``max(0, scanned - unique)`` (mirrors :func:`deduped_count`
    — ``scanned`` is never None here so the value is always a plain int). The per-agent file counts
    come from one grouped ``SELECT agent_id, COUNT(id) GROUP BY agent_id`` joined in Python.

    An empty map means "no annotations"; the template hides any agent whose deduped is 0. Wrapped in
    the standard log → guarded rollback → ``{}`` degrade so it never raises into the dashboard poll.
    """
    try:
        ranked = (
            select(
                ScanBatch.agent_id.label("agent_id"),
                ScanBatch.total_files.label("total_files"),
                func.row_number().over(partition_by=ScanBatch.agent_id, order_by=ScanBatch.created_at.desc()).label("rn"),
            )
            .where(ScanBatch.status == ScanStatus.COMPLETED.value)
            .subquery()
        )
        latest_rows = (await session.execute(select(ranked.c.agent_id, ranked.c.total_files).where(ranked.c.rn == 1))).all()

        count_rows = (await session.execute(select(FileRecord.agent_id, func.count(FileRecord.id)).group_by(FileRecord.agent_id))).all()
        counts_by_agent = {agent_id: int(count) for agent_id, count in count_rows}

        out: dict[str, dict[str, int]] = {}
        for agent_id, total_files in latest_rows:
            scanned = int(total_files)
            unique = counts_by_agent.get(agent_id, 0)
            out[agent_id] = {"scanned": scanned, "unique": unique, "deduped": max(0, scanned - unique)}
        return out
    except Exception:
        logger.warning("agent_reconciliations_degraded", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("agent_reconciliations_rollback_failed", exc_info=True)
        return {}


async def get_queue_activity(app_state: Any, session: AsyncSession) -> dict[str, int]:
    """Read live SAQ queue depth -- the authoritative "is anything in flight" signal.

    The DB cannot distinguish "nothing queued" from "everything queued" (``process_file``
    does not move a file out of ``DISCOVERED`` until a worker finishes it), so the only
    truthful in-flight signal is the live Redis queue depth read through SAQ.

    Sums ``count("queued") + count("active")`` across every non-revoked agent's per-agent
    queue (the same ``revoked_at IS NULL`` predicate ``dashboard()`` uses -- NOT
    ``select_active_agent``, which returns one agent and raises when none is recently seen)
    plus the controller queue. Only the ``queued`` and ``active`` kinds are read: those two
    kinds exclude scheduled/cron jobs, so the idle controller crons (``reap_stalled_scans``,
    ``refresh_tracklists``) never inflate the counts. The scheduled-inclusive kind is never
    read.

    Failure isolation is split per-source and the function never raises: a Redis hiccup or
    a missing ``app.state`` attribute (the test ``client`` skips the lifespan, so the queue
    handles are absent) must degrade that source to 0, never 500 the 5s dashboard poll. The
    agent and controller reads use independent ``try`` blocks so one dead source does not
    zero the other.

    Returns a dict with keys ``agent_queued``, ``agent_active``, ``controller_queued``,
    ``controller_active``, ``agent_busy`` (= queued + active), ``controller_busy``.
    """
    agent_queued = agent_active = controller_queued = controller_active = 0

    try:
        agents_stmt = select(Agent).where(Agent.revoked_at.is_(None))
        agents = (await session.execute(agents_stmt)).scalars().all()
        for agent in agents:
            # quick-260707-dh1: sum queued+active across ALL FOUR lane queues (the authoritative
            # all-lane agent depth -- the heartbeat's queue_depth is analyze-lane-only by design)
            # PLUS the legacy base queue so the migration drain window stays visible. A 0/absent
            # base degrades cleanly through the same try/except.
            for q in (*app_state.task_router.all_lane_queues(agent.id), app_state.task_router.legacy_base_queue(agent.id)):
                agent_queued += await q.count("queued")
                agent_active += await q.count("active")
    except Exception:
        # Broad by design: a missing app.state attr (test lifespan-skip) or any Redis
        # hiccup must degrade this source to 0, never 500 the 5s dashboard poll.
        agent_queued = agent_active = 0
        logger.warning("queue_activity_degraded", source="agent", exc_info=True)

    try:
        controller_queued = await app_state.controller_queue.count("queued")
        controller_active = await app_state.controller_queue.count("active")
    except Exception:
        # Broad by design: a missing app.state attr (test lifespan-skip) or any Redis
        # hiccup must degrade this source to 0, never 500 the 5s dashboard poll.
        controller_queued = controller_active = 0
        logger.warning("queue_activity_degraded", source="controller", exc_info=True)

    agent_busy = agent_queued + agent_active
    controller_busy = controller_queued + controller_active
    return {
        "agent_queued": agent_queued,
        "agent_active": agent_active,
        "controller_queued": controller_queued,
        "controller_active": controller_active,
        "agent_busy": agent_busy,
        "controller_busy": controller_busy,
    }


def queue_progress_percent(analyzed: int, agent_busy: int) -> int:
    """Compute the DB-derived "Processing" progress percent (0-100), divide-by-zero guarded.

    The single source of truth for the operator-chosen progress formula: ``done`` is the
    existing DB ``analyzed`` count and the denominator is ``analyzed + agent_busy`` (the
    in-flight agent depth). Chosen over SAQ's aggregated ``complete`` because it survives
    worker restarts -- the bar won't jump backward. Accepted trade-off: pre-existing
    analyzed files count toward ``done``.

    Extracted as a module-level pure helper (raw int inputs, no I/O) so the formula is
    unit-testable in isolation -- proving the numerator is ``analyzed`` and the denominator
    is ``analyzed + agent_busy`` (a reversed ratio would silently pass an echo-only test).
    When ``analyzed + agent_busy == 0`` (idle) it returns 0 so the card renders empty and
    no divide-by-zero occurs.
    """
    return round(analyzed / denom * 100) if (denom := analyzed + agent_busy) else 0


async def _safe_count(session: AsyncSession, stmt: Select[Any], *, node: str) -> int:
    """Run a single-scalar COUNT statement, degrading to 0 on any failure.

    Per-source failure isolation mirroring :func:`get_queue_activity`: a bad source
    (a DB hiccup, an aborted transaction from a prior failed source) must degrade
    THIS node to 0, never raise into the 5s dashboard poll. On error the session is
    rolled back so a Postgres "current transaction is aborted" state from one failed
    source does not poison the COUNT queries for every subsequent stage.
    """
    try:
        return int((await session.execute(stmt)).scalar() or 0)
    except Exception:
        logger.warning("stage_progress_degraded", node=node, exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("stage_progress_rollback_failed", node=node, exc_info=True)
        return 0


async def _safe_bucket_counts(session: AsyncSession, stage: Stage) -> dict[str, int]:
    """Return the five-way ``{not_started, in_flight, done, skipped, failed}`` count for ``stage``, degrade-safe.

    ONE ``GROUP BY stage_status_case(stage)`` scoped to music/video files. Because every music/video
    file resolves to exactly one of the five :func:`phaze.services.stage_status.stage_status_case`
    buckets (precedence ``in_flight ≻ done ≻ skipped ≻ failed ≻ not_started``; ``skipped`` is the
    Phase-87 force-skip marker, enrich-only), the five counts SUM to
    ``music_video_total`` on a healthy query. Reuses the LOCKED ``stage_status_case`` ``CASE`` ladder
    verbatim -- NEVER a fresh ``CASE`` (D-04) -- so the buckets can never drift from the DERIV-04
    equivalence lock (and, transitively, the Python resolver).

    Mirrors the :func:`_safe_count` degrade discipline (INFLIGHT-02): the dict zero-fills first, and on
    ANY exception this logs a warning, guarded-rolls-back the aborted transaction (so a Postgres
    "current transaction is aborted" state cannot poison the later stage COUNTs), and returns the
    all-zero dict -- it NEVER raises into the hot 5s /pipeline/stats poll. On that fail-safe-to-zero
    degrade the five buckets intentionally do NOT sum to ``music_video_total``; the sum-to-total
    invariant is a healthy-query property only, NEVER a runtime assertion in the poll path (Pitfall 3).
    """
    out: dict[str, int] = {s.value: 0 for s in Status}
    # Materialize the per-row status label in an inner subquery FIRST, then GROUP BY the label in the
    # outer query. Grouping directly by ``stage_status_case(stage)`` fails on Postgres -- the CASE ladder
    # embeds correlated ``exists(... == FileRecord.id)`` subqueries, and a top-level GROUP BY on that
    # expression re-projects the ungrouped ``files.id`` ("subquery uses ungrouped column" GroupingError).
    # The derived-table form evaluates the per-file status once per row (where ``files.id`` is in scope),
    # so the outer aggregation groups a plain scalar label.
    status_subq = select(stage_status_case(stage).label("status")).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES)).subquery()
    stmt = select(status_subq.c.status, func.count()).group_by(status_subq.c.status)
    try:
        for status_label, n in (await session.execute(stmt)).all():
            if status_label in out:
                out[status_label] = int(n)
    except Exception:
        logger.warning("stage_bucket_degraded", stage=stage.value, exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("stage_bucket_rollback_failed", stage=stage.value, exc_info=True)
    return out


async def _agent_stage_buckets(session: AsyncSession, agent_id: str, stage: Stage) -> dict[str, int]:
    """Per-agent five-way ``{not_started, in_flight, done, skipped, failed}`` count for ``stage``, degrade-safe.

    A one-conjunct clone of :func:`_safe_bucket_counts` (DRILL-02 / D-04): the SAME GroupingError-safe
    inner-subquery-then-``GROUP BY``-scalar-label shape, with the SINGLE addition of
    ``.where(FileRecord.agent_id == agent_id)`` on the inner subquery so the aggregate counts ONLY the
    music/video files THIS agent owns. Reuses the LOCKED :func:`phaze.services.stage_status.stage_status_case`
    ``CASE`` ladder verbatim (D-00a / DERIV-04) -- NEVER a fresh ``CASE`` -- so the per-agent buckets can
    never drift from the single derivation (and, transitively, the Python resolver).

    Because every one of the agent's music/video files resolves to exactly one of the five
    ``stage_status_case`` buckets (precedence ``in_flight ≻ done ≻ skipped ≻ failed ≻ not_started``), the
    five counts SUM to the agent's music/video total on a HEALTHY query -- a healthy-path property only,
    NEVER a runtime assertion in the poll path (Pitfall 3). On ANY query error this mirrors the
    :func:`_safe_bucket_counts` degrade discipline (INFLIGHT-02 / D-00b): it logs a warning and returns
    the all-zero dict -- it NEVER raises into the hot ``/admin/agents/{id}/_activity`` poll. On that
    fail-safe degrade the five buckets intentionally do NOT sum to the total.

    The read runs inside a SAVEPOINT (``begin_nested``) so a bucket-query error rolls back the NESTED
    scope ALONE -- recovering the aborted transaction WITHOUT expiring the caller's already-loaded
    ``agent`` ORM object. ``agent_activity`` loads ``agent`` BEFORE these six bucket reads and renders
    its attributes AFTER, so a plain ``session.rollback()`` here would expire ``agent`` and 500 the
    render on the next lazy load (CR-01) -- exactly the hazard :func:`get_agent_recent_scans` guards
    against on the same object.
    """
    out: dict[str, int] = {s.value: 0 for s in Status}
    # Materialize the per-row status label in an inner subquery FIRST, then GROUP BY the scalar label in
    # the outer query -- grouping directly by ``stage_status_case(stage)`` fails on Postgres (the CASE
    # ladder embeds correlated ``exists(... == FileRecord.id)`` subqueries; a top-level GROUP BY re-projects
    # the ungrouped ``files.id`` -> "subquery uses ungrouped column" GroupingError). The ONLY delta from
    # :func:`_safe_bucket_counts` is the ``FileRecord.agent_id == agent_id`` conjunct (D-04).
    status_subq = (
        select(stage_status_case(stage).label("status"))
        .where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))
        .where(FileRecord.agent_id == agent_id)
        .subquery()
    )
    stmt = select(status_subq.c.status, func.count()).group_by(status_subq.c.status)
    try:
        # SAVEPOINT degrade (CR-01 / D-00b): roll back the NESTED scope alone on error so the aborted
        # transaction recovers WITHOUT expiring the caller's already-loaded ``agent`` (a plain
        # ``session.rollback()`` would expire it and 500 the render on the next lazy load).
        async with session.begin_nested():
            rows = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("agent_stage_bucket_degraded", stage=stage.value, agent_id=agent_id, exc_info=True)
        return out
    for status_label, n in rows:
        if status_label in out:
            out[status_label] = int(n)
    return out


# Recent-scan-batches cap for the agent-activity pane (D-05 / D-00b). A small fixed LIMIT keeps the
# per-poll read bounded -- the agent pane shows the most recent handful, scroll for depth is unnecessary.
_AGENT_RECENT_SCANS_N = 10


async def get_agent_lane_depths(app_state: Any, agent_id: str) -> dict[str, int]:
    """Return the agent's per-lane in-flight depth ``{analyze, fingerprint, meta, io}``, degrade-safe (D-05 / D-00b).

    Sums ``count("queued") + count("active")`` across each of the agent's four
    :data:`phaze.services.enqueue_router.LANES` queues (the same ``all_lane_queues`` seam
    :func:`get_queue_activity` uses), keyed by lane name so the agent pane can render
    ``analyze N · fingerprint N · meta N · io N``. The legacy base queue is deliberately EXCLUDED
    here -- this pane shows the live per-lane split, not the migration-drain total.

    Failure isolation mirrors :func:`get_queue_activity`: a missing ``app.state.task_router`` (the test
    client skips the lifespan, so the queue handles are absent) or a broker hiccup degrades the whole
    dict to all-zero; a single dead lane degrades THAT lane to 0 without zeroing the others. It NEVER
    raises into the 5s ``/admin/agents/{id}/_activity`` poll (D-00b).
    """
    out: dict[str, int] = dict.fromkeys(LANES, 0)
    try:
        queues = app_state.task_router.all_lane_queues(agent_id)
    except Exception:
        # Broad by design: a missing app.state attr (test lifespan-skip) or any broker hiccup must
        # degrade every lane to 0, never 500 the 5s agent-pane poll.
        logger.warning("agent_lane_depths_degraded", agent_id=agent_id, exc_info=True)
        return out
    for lane, q in zip(LANES, queues, strict=False):
        try:
            out[lane] = await q.count("queued") + await q.count("active")
        except Exception:
            logger.warning("agent_lane_depth_degraded", agent_id=agent_id, lane=lane, exc_info=True)
            out[lane] = 0
    return out


async def get_agent_recent_scans(session: AsyncSession, agent_id: str, *, limit: int = _AGENT_RECENT_SCANS_N) -> list[ScanBatch]:
    """Return the agent's most-recent ``ScanBatch`` rows (newest-first, bounded), degrade-safe (D-05 / D-00b).

    One indexed read over ``ix_scan_batches_agent_id`` (``models/scan_batch.py``): the agent's scan
    batches ordered ``created_at DESC`` with a fixed small ``LIMIT`` so the per-poll cost stays bounded
    (T-88-08). The read runs inside a SAVEPOINT (``begin_nested``) so ANY DB error rolls back the nested
    scope ALONE -- recovering the aborted transaction WITHOUT expiring the caller's already-loaded
    ``agent`` ORM object (a plain ``session.rollback()`` would expire it and 500 the render on the next
    lazy load) -- and the function returns ``[]``. It NEVER raises into the 5s agent-pane poll.
    """
    try:
        async with session.begin_nested():
            stmt = select(ScanBatch).where(ScanBatch.agent_id == agent_id).order_by(ScanBatch.created_at.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
        return list(rows)
    except Exception:
        logger.warning("agent_recent_scans_degraded", agent_id=agent_id, exc_info=True)
        return []


async def get_stage_progress(session: AsyncSession) -> dict[str, dict[str, int | None]]:
    """Authoritative per-DAG-node reconcile source (D-03) -- counts each stage's OUTPUT table.

    The single-valued linear ``FileRecord.state`` (one enum per file) STRUCTURALLY cannot report
    parallel-stage done-counts; this query instead counts each stage's OUTPUT table. A file that is
    both fingerprinted AND analyzed contributes to BOTH ``fingerprint.done`` and ``analyze.done``
    here -- impossible to express through the single-valued state enum (RESEARCH Q5). Phase 82
    (READ-02, D-05) removed the former state-grouped ``get_pipeline_stats`` entirely; the stats path
    now derives its seven keys from THIS function (no ``FileRecord.state`` read).

    Returns a dict keyed by DAG node. The three ENRICH nodes carry the FIVE-BUCKET shape
    ``{not_started, in_flight, done, skipped, failed, total}`` (Phase 82 + Phase-87 ``skipped``); every OTHER node keeps
    ``{"done": int, "total": int | None}``:

    - ``discovery``   -- done = COUNT(files); total = itself (bar is always 100%)
    - ``metadata``    -- FIVE-BUCKET via ``stage_status_case(METADATA)`` over music/video files
      (:func:`_safe_bucket_counts`); ``done`` = row present + ``failed_at`` NULL; total = music/video count
    - ``fingerprint`` -- FIVE-BUCKET via ``stage_status_case(FINGERPRINT)``; ``done`` = any engine row in
      ('success','completed'); ``failed`` = failed-only (no success); total = music/video count
    - ``analyze``     -- FIVE-BUCKET via ``stage_status_case(ANALYZE)``; ``done`` = ``analysis`` row with
      ``analysis_completed_at`` NOT NULL (a partial in-flight row is ``in_flight``, not done); total = music/video count
    - ``scan_search`` -- done = DISTINCT file_id in ``tracklists``; total = ``None`` (counter-only; the UI
      renders ``done / —``). No DB table defines "should get a tracklist" so NO denominator is fabricated.
    - ``scrape``      -- done = DISTINCT tracklist_id in ``tracklist_versions``; total = COUNT(tracklists)
    - ``match``       -- done = DISTINCT tracklist_id reachable from ``discogs_links``; total = COUNT(tracklists)
    - ``proposals``   -- done = DISTINCT file_id in ``proposals``; total = convergence set (files with BOTH
      ``metadata`` AND ``analysis``, mirroring routers/pipeline.py:116-128)
    - ``execute``     -- done = DISTINCT file_id with a completed ``execution_log`` row; total = approved-proposal count

    Each source is wrapped in :func:`_safe_count` (or :func:`_safe_bucket_counts` for the enrich
    nodes) so a single failing stage degrades to zero and the function never raises into the 5s poll.
    """
    music_video_total = await _safe_count(
        session,
        select(func.count(FileRecord.id)).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES)),
        node="music_video_total",
    )
    tracklist_total = await _safe_count(session, select(func.count(Tracklist.id)), node="tracklist_total")

    discovery_done = await _safe_count(session, select(func.count(FileRecord.id)), node="discovery")

    # Proposals denominator: the convergence-gate set -- files with BOTH metadata AND analysis
    # (mirrors routers/pipeline.py:116-128, the generate_proposals ready-set).
    convergence_total = await _safe_count(
        session,
        select(func.count(FileRecord.id))
        .where(exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id)))
        .where(exists(select(AnalysisResult.id).where(AnalysisResult.file_id == FileRecord.id))),
        node="proposals_total",
    )

    # match.done: distinct tracklist_id reachable from a discogs_link, walked
    # discogs_links -> tracklist_tracks -> tracklist_versions (discogs_links carries
    # only track_id; tracklist_id lives on the version row).
    match_done_stmt = (
        select(func.count(distinct(TracklistVersion.tracklist_id)))
        .select_from(DiscogsLink)
        .join(TracklistTrack, DiscogsLink.track_id == TracklistTrack.id)
        .join(TracklistVersion, TracklistTrack.version_id == TracklistVersion.id)
    )

    # execute.done: distinct file_id with a COMPLETED execution_log row, walked
    # execution_log -> proposals (execution_log carries only proposal_id).
    execute_done_stmt = (
        select(func.count(distinct(RenameProposal.file_id)))
        .select_from(ExecutionLog)
        .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
        .where(ExecutionLog.status == ExecutionStatus.COMPLETED)
    )

    return {
        "discovery": {"done": discovery_done, "total": discovery_done},
        # Phase 82 (READ-02, D-04/D-05) + Phase 87 (skipped): the three enrich nodes are FIVE-BUCKET
        # ({not_started, in_flight, done, skipped, failed} + total) via one GROUP BY stage_status_case(stage)
        # each -- so the DAG surfaces a VISIBLE failed count per enrich stage and the five buckets sum
        # to music_video_total on a healthy query. `total` stays music_video_total; `done` (still read
        # by _build_dag_context) is now the derived done-bucket. Degrade-safe (all-zero on any error).
        "metadata": {**await _safe_bucket_counts(session, Stage.METADATA), "total": music_video_total},
        "fingerprint": {**await _safe_bucket_counts(session, Stage.FINGERPRINT), "total": music_video_total},
        "analyze": {**await _safe_bucket_counts(session, Stage.ANALYZE), "total": music_video_total},
        "scan_search": {
            "done": await _safe_count(session, select(func.count(distinct(Tracklist.file_id))), node="scan_search"),
            "total": None,  # counter-only: no table defines "should get a tracklist" (RESEARCH Q5 / UI-SPEC)
        },
        "scrape": {
            "done": await _safe_count(session, select(func.count(distinct(TracklistVersion.tracklist_id))), node="scrape"),
            "total": tracklist_total,
        },
        "match": {
            "done": await _safe_count(session, match_done_stmt, node="match"),
            "total": tracklist_total,
        },
        "proposals": {
            "done": await _safe_count(session, select(func.count(distinct(RenameProposal.file_id))), node="proposals"),
            "total": convergence_total,
        },
        "execute": {
            "done": await _safe_count(session, execute_done_stmt, node="execute"),
            "total": await _safe_count(
                session,
                select(func.count(distinct(RenameProposal.file_id))).where(RenameProposal.status == ProposalStatus.APPROVED),
                node="execute_total",
            ),
        },
    }


# Per-stage pause/priority defaults (Phase 38, REQ-38-4). Mirror the Phase 37 control-table
# semantics for the three agent stages: unpaused, mid-range priority 50. Returned verbatim
# whenever the control table is unreadable/absent so the 5s /pipeline/stats poll degrades to a
# sane default instead of 500ing (T-38-DEGRADE — identical discipline to _safe_count above).
_DEFAULT_CONTROLS: dict[str, dict[str, int | bool]] = {s: {"paused": False, "priority": 50} for s in ("metadata", "analyze", "fingerprint")}


async def get_stage_controls(session: AsyncSession) -> dict[str, dict[str, int | bool]]:
    """Read the 3 ``pipeline_stage_control`` rows, degrading to defaults so the 5s poll never 500s.

    Returns ``{metadata, analyze, fingerprint}`` each mapping to ``{"paused": bool, "priority": int}``.
    On the happy path each present stage row overlays its ``paused`` / ``priority`` onto a fresh copy
    of :data:`_DEFAULT_CONTROLS`; unknown ``stage`` values are ignored (guarded by ``if r.stage in out``).

    Failure isolation mirrors :func:`_safe_count` / :func:`get_queue_activity`: the
    ``pipeline_stage_control`` table may be absent (pre-migration env) or a DB hiccup may occur, and
    EITHER must degrade to the three-stage defaults rather than raise into the hot 5s poll path
    (T-38-DEGRADE). On any exception this logs a warning, rolls back the aborted transaction (guarded,
    so a failed rollback cannot mask the original error or poison later COUNTs), and returns defaults.

    The caller (:func:`phaze.routers.pipeline._build_dag_context`) coerces ``paused`` to ``int`` ``0``/``1``
    so the canvas's "every dag value is a server-computed int safe to interpolate into ``x-init``"
    invariant holds (Pitfall 3 / T-35-11) — never emit a Python ``bool`` through to the template.
    """
    try:
        rows = (await session.execute(select(PipelineStageControl))).scalars().all()
        out: dict[str, dict[str, int | bool]] = {s: dict(v) for s, v in _DEFAULT_CONTROLS.items()}
        for r in rows:
            if r.stage in out:
                out[r.stage] = {"paused": r.paused, "priority": r.priority}
        return out
    except Exception:
        logger.warning("stage_controls_degraded", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("stage_controls_rollback_failed", exc_info=True)
        return {s: dict(v) for s, v in _DEFAULT_CONTROLS.items()}


# Per-stage in-flight gate (Phase 38 follow-up, t7k FIX2). ``saq_jobs`` has NO ``function`` column;
# the deterministic key is ``<function>:<file_id>`` (Phase 35), so the per-stage in-flight count is
# bucketed by the key's function prefix. Static SQL with NO interpolated operator input — the only
# literals are ``split_part`` and the ``status`` allowlist (T-t7k-01, mirroring the Phase-37
# stage_control discipline). One grouped scan covers all three agent stages.
_STAGE_BUSY_SQL = text("SELECT split_part(key, ':', 1) AS fn, COUNT(*) AS n FROM saq_jobs WHERE status IN ('queued', 'active') GROUP BY fn")

# Registered-function-name -> stage label (the inverse of STAGE_TO_FUNCTION), built locally so the
# bucket loop maps each saq_jobs key prefix back to its agent stage; non-stage functions
# (generate_proposals, scan_directory, ...) are absent here and therefore ignored.
_BUSY_FUNCTION_TO_STAGE: dict[str, str] = {fn: stage for stage, fn in STAGE_TO_FUNCTION.items()}


async def get_stage_busy_counts(session: AsyncSession) -> dict[str, int]:
    """Return the per-agent-stage in-flight job count ``{metadata, analyze, fingerprint}``.

    Counts ``saq_jobs`` rows with ``status IN ('queued', 'active')`` whose deterministic key prefix
    maps to one of the three agent stages. This REPLACES the single global ``agentBusy`` gate
    (queued+active summed across ALL agent queues) that locked all three agent stages together --
    each stage now gates on ITS OWN in-flight count, so Metadata, Analyze and Fingerprint run in
    parallel (running one no longer blocks the other two).

    A paused stage's parked rows (status still ``queued``, ``scheduled = SENTINEL``) DO count as busy
    -- an accepted, documented behavior consistent with the prior global ``agentBusy`` meaning of
    "has a backlog" (the enqueue button stays blocked while a backlog exists).

    Failure isolation (T-t7k-02): the ``saq_jobs`` read runs inside a SAVEPOINT
    (``session.begin_nested()``). On ANY DB error (a missing ``saq_jobs`` table in a pre-migration
    env, a DB hiccup) the nested scope is rolled back ALONE -- recovering the aborted Postgres
    transaction WITHOUT expiring the dashboard's already-loaded ORM objects (a plain
    ``session.rollback()`` would expire ``agents`` / ``recent_scans`` and 500 the page on the next
    lazy load) and WITHOUT poisoning later queries. The function then logs a warning and returns
    all-zeros -- it NEVER raises into the hot 5s /pipeline/stats poll.
    """
    out: dict[str, int] = {"metadata": 0, "analyze": 0, "fingerprint": 0}
    try:
        async with session.begin_nested():
            rows = (await session.execute(_STAGE_BUSY_SQL)).all()
    except Exception:
        logger.warning("stage_busy_degraded", exc_info=True)
        return out
    for row in rows:
        stage = _BUSY_FUNCTION_TO_STAGE.get(row[0])
        if stage is not None:
            out[stage] = int(row[1])
    return out


# Live-broker key set (Phase 45). ``saq_jobs`` is SAQ-owned -- this is a READ-ONLY probe of the
# live broker, never an Alembic-managed table (mirrors the _STAGE_BUSY_SQL / _INFLIGHT_COUNT_SQL
# discipline). Recovery subtracts this set from the scheduling-ledger rows to find work that was
# scheduled then lost; parked/paused jobs keep status='queued' and so correctly stay IN this live
# set (out of the orphan set). Static SQL with NO interpolated operator input -- the only literals
# are the column name and the status allowlist (T-45 read-only probe).
_LIVE_KEYS_SQL = text("SELECT key FROM saq_jobs WHERE status IN ('queued', 'active')")


async def get_live_job_keys(session: AsyncSession) -> set[str]:
    """Return the set of ``saq_jobs`` keys currently ``queued`` or ``active``. Degrade-safe.

    The recovery exclusion set: ``ledger - live keys`` is exactly the previously-scheduled work
    that is no longer live (lost). ``queued``/``active`` are the only LIVE statuses; SAQ sweeps
    terminal (COMPLETE/FAILED/ABORTED) rows ~10 min after they end, so a terminal row is NOT a
    durable signal -- the ledger owns its own durable clear.

    Failure isolation: the read runs inside a SAVEPOINT (``session.begin_nested()``). On ANY DB
    error (a missing ``saq_jobs`` table in a pre-migration env, a DB hiccup) the nested scope is
    rolled back ALONE and the function returns an EMPTY set -- it never raises into the recovery
    producer (clones the get_stage_busy_counts isolation verbatim).
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(_LIVE_KEYS_SQL)).all()
    except Exception:
        logger.warning("live_job_keys_degraded", exc_info=True)
        return set()
    return {row[0] for row in rows}


async def get_stage_orphan_counts(session: AsyncSession) -> dict[str, int]:
    """Return the per-enrich-stage orphaned/stuck (recovery-candidate) count, degrade-safe (Phase 87, UI-05/D-05).

    orphan(stage) = the number of ``scheduling_ledger`` rows for the stage's function that are NEITHER
    live (a queued/active ``saq_jobs`` key) NOR domain-completed NOR owned by an in-flight ``cloud_job``
    -- i.e. EXACTLY the set :func:`phaze.tasks.reenqueue.recover_orphaned_work` would re-enqueue for
    that stage. Parity with recovery is DEFINITIONAL (T-87-31 / OQ-2): this reuses recovery's OWN
    classification predicate (``is_domain_completed`` + the per-stage done-set derivation
    ``_build_done_sets`` + the in-flight cloud exclusion ``_in_flight_cloud_job_ids``) rather than
    re-deriving the done clauses here, so the amber rail badge can never drift from what recovery does.

    Returns ``{metadata, analyze, fingerprint}`` -> int (the three :data:`STAGE_TO_FUNCTION` enrich
    functions ``extract_file_metadata`` / ``process_file`` / ``fingerprint_file``); ``push_file`` /
    ``scan_live_set`` / the controller functions are NOT part of the per-enrich badge.

    No staleness threshold is used, so the naive-``enqueued_at`` footgun (Pitfall 4, project memory)
    never bites here -- the only naive/aware comparison is the D-10 metadata cell inside
    ``is_domain_completed``, which already coerces the naive ledger stamp to UTC-aware (CR-02).

    Failure isolation (T-87-28): the whole derivation runs inside a SAVEPOINT
    (``session.begin_nested()``); on ANY DB error the nested scope is rolled back ALONE -- recovering
    the aborted Postgres transaction WITHOUT expiring the dashboard's already-loaded ORM objects (a
    plain ``session.rollback()`` would 500 the page on the next lazy load) -- and the all-zero default
    is returned. It NEVER raises into the hot 5s /pipeline/stats poll. The ``reenqueue`` import is
    FUNCTION-LOCAL: ``reenqueue`` imports :func:`get_live_job_keys` FROM this module, so a top-level
    import would be circular; deferring it also keeps the agent-worker import boundary intact
    (``reenqueue`` is control-only and must never be loaded merely by importing ``services.pipeline``).
    """
    out: dict[str, int] = {"metadata": 0, "analyze": 0, "fingerprint": 0}
    try:
        async with session.begin_nested():
            # Function-local import (see docstring): break the reenqueue<->pipeline import cycle and
            # preserve the control-only boundary (tests/test_task_split.py).
            from phaze.services.scheduling_ledger import get_ledger_rows  # noqa: PLC0415 -- deferred: keeps the reenqueue<->pipeline cycle broken
            from phaze.tasks.reenqueue import (  # noqa: PLC0415 -- deferred: reenqueue is control-only + imports FROM this module (cycle)
                _build_done_sets,
                _in_flight_cloud_job_ids,
                _ledger_fids,
                _natural_id,
                is_domain_completed,
            )

            rows = await get_ledger_rows(session)
            live = await get_live_job_keys(session)
            done_sets = await _build_done_sets(session, _ledger_fids(rows))
            in_flight = await _in_flight_cloud_job_ids(session)
            for row in rows:
                stage = _BUSY_FUNCTION_TO_STAGE.get(row.function)
                if stage is None:
                    continue  # push_file / scan_live_set / controller rows are not enrich badges
                if row.key in live or is_domain_completed(row, done_sets) or _natural_id(row) in in_flight:
                    continue
                out[stage] += 1
    except Exception:
        logger.warning("stage_orphan_counts_degraded", exc_info=True)
        return {"metadata": 0, "analyze": 0, "fingerprint": 0}
    return out


# Search-tracklist in-flight gate (Phase 39, REQ-39-3). search_tracklist is a CONTROLLER task --
# NOT one of the three agent stages -- so it is deliberately ABSENT from get_stage_busy_counts's
# {metadata,analyze,fingerprint} contract (that function + its tests stay untouched). The
# deterministic key is "search_tracklist:<file_id>" (Phase 35), so the in-flight count is the
# bucket whose key prefix == "search_tracklist". Reuses the SAME static _STAGE_BUSY_SQL grouped
# scan (no operator input is interpolated -- the only literals are split_part, the status
# allowlist, and the function-name constant below; T-39-01, mirroring the Phase-37/t7k discipline).
_SEARCH_BUSY_FUNCTION = "search_tracklist"


async def get_search_busy_count(session: AsyncSession) -> int:
    """Return the in-flight ``search_tracklist`` job count (``queued`` + ``active``), degrade-safe.

    Counts the ``saq_jobs`` rows whose deterministic key prefix is ``search_tracklist`` (status
    ``IN ('queued', 'active')``). This drives the DAG Search node's "Search busy" gate so a second
    bulk search cannot be launched while one batch is in flight. A paused/parked search job (status
    still ``queued``) counts as busy -- the same accepted semantics as :func:`get_stage_busy_counts`.

    Failure isolation (T-39-03): the read runs inside a SAVEPOINT (``session.begin_nested()``). On
    ANY DB error (a missing ``saq_jobs`` table in a pre-migration env, a DB hiccup) the nested scope
    is rolled back ALONE -- recovering the aborted Postgres transaction WITHOUT expiring the
    dashboard's already-loaded ORM objects (a plain ``session.rollback()`` would 500 the page on the
    next lazy load) and WITHOUT poisoning later queries. The function logs a warning and returns 0 --
    it NEVER raises into the hot 5s /pipeline/stats poll.
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(_STAGE_BUSY_SQL)).all()
    except Exception:
        logger.warning("search_busy_degraded", exc_info=True)
        return 0
    for row in rows:
        if row[0] == _SEARCH_BUSY_FUNCTION:
            return int(row[1])
    return 0


# Fingerprint-scan in-flight gate (Phase 40, REQ-40-3). scan_live_set is a PER-AGENT task --
# NOT one of the three agent stages tracked by get_stage_busy_counts (that function + its tests
# stay untouched) -- but its jobs live in the SAME saq_jobs table (Postgres backend), so the same
# key-prefix scan works. The deterministic key is "scan_live_set:<file_id>" (Phase 35), so the
# in-flight count is the bucket whose key prefix == "scan_live_set". Reuses the SAME static
# _STAGE_BUSY_SQL grouped scan (no operator input is interpolated -- the only literals are
# split_part, the status allowlist, and the function-name constant below; T-40-01, mirroring the
# Phase-37/t7k/Phase-39 static-SQL discipline).
_SCAN_BUSY_FUNCTION = "scan_live_set"


async def get_scan_busy_count(session: AsyncSession) -> int:
    """Return the in-flight ``scan_live_set`` job count (``queued`` + ``active``), degrade-safe.

    Counts the ``saq_jobs`` rows whose deterministic key prefix is ``scan_live_set`` (status
    ``IN ('queued', 'active')``). This drives the DAG Fingerprint-Scan node's "Scan busy" gate so a
    second bulk scan cannot be launched while one batch is in flight. A paused/parked scan job
    (status still ``queued``) counts as busy -- the same accepted semantics as
    :func:`get_search_busy_count`.

    Failure isolation (T-40-03): the read runs inside a SAVEPOINT (``session.begin_nested()``). On
    ANY DB error (a missing ``saq_jobs`` table in a pre-migration env, a DB hiccup) the nested scope
    is rolled back ALONE -- recovering the aborted Postgres transaction WITHOUT expiring the
    dashboard's already-loaded ORM objects (a plain ``session.rollback()`` would 500 the page on the
    next lazy load) and WITHOUT poisoning later queries. The function logs a warning and returns 0 --
    it NEVER raises into the hot 5s /pipeline/stats poll.
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(_STAGE_BUSY_SQL)).all()
    except Exception:
        logger.warning("scan_busy_degraded", exc_info=True)
        return 0
    for row in rows:
        if row[0] == _SCAN_BUSY_FUNCTION:
            return int(row[1])
    return 0


# Bulk scrape/match in-flight gates (Phase 41, REQ-41-3). Both scrape_and_store_tracklist and
# match_tracklist_to_discogs are CONTROLLER tasks -- NOT one of the three agent stages tracked by
# get_stage_busy_counts (that function + its tests stay untouched) -- but their jobs live in the SAME
# saq_jobs table, so the same key-prefix scan works. The deterministic keys are
# "scrape_and_store_tracklist:<tracklist_id>" / "match_tracklist_to_discogs:<tracklist_id>" (Phase 35),
# so each in-flight count is the bucket whose key prefix == the function-name constant. Reuses the SAME
# static _STAGE_BUSY_SQL grouped scan (no operator input is interpolated -- the only literals are
# split_part, the status allowlist, and the function-name constants below; T-41-01, mirroring the
# Phase-37/39/40 static-SQL discipline).
_SCRAPE_BUSY_FUNCTION = "scrape_and_store_tracklist"
_MATCH_BUSY_FUNCTION = "match_tracklist_to_discogs"


async def get_scrape_busy_count(session: AsyncSession) -> int:
    """Return the in-flight ``scrape_and_store_tracklist`` job count (``queued`` + ``active``), degrade-safe.

    Counts the ``saq_jobs`` rows whose deterministic key prefix is ``scrape_and_store_tracklist``
    (status ``IN ('queued', 'active')``). This drives the DAG Scrape node's "Scraping…" gate so a
    second bulk scrape cannot be launched while one batch is in flight. A paused/parked scrape job
    (status still ``queued``) counts as busy -- the same accepted semantics as
    :func:`get_search_busy_count`.

    Failure isolation (T-41-03): the read runs inside a SAVEPOINT (``session.begin_nested()``). On
    ANY DB error (a missing ``saq_jobs`` table in a pre-migration env, a DB hiccup) the nested scope
    is rolled back ALONE -- recovering the aborted Postgres transaction WITHOUT expiring the
    dashboard's already-loaded ORM objects (a plain ``session.rollback()`` would 500 the page on the
    next lazy load) and WITHOUT poisoning later queries. The function logs a warning and returns 0 --
    it NEVER raises into the hot 5s /pipeline/stats poll.
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(_STAGE_BUSY_SQL)).all()
    except Exception:
        logger.warning("scrape_busy_degraded", exc_info=True)
        return 0
    for row in rows:
        if row[0] == _SCRAPE_BUSY_FUNCTION:
            return int(row[1])
    return 0


async def get_match_busy_count(session: AsyncSession) -> int:
    """Return the in-flight ``match_tracklist_to_discogs`` job count (``queued`` + ``active``), degrade-safe.

    Counts the ``saq_jobs`` rows whose deterministic key prefix is ``match_tracklist_to_discogs``
    (status ``IN ('queued', 'active')``). This drives the DAG Match node's "Matching…" gate so a
    second bulk match cannot be launched while one batch is in flight. A paused/parked match job
    (status still ``queued``) counts as busy -- the same accepted semantics as
    :func:`get_search_busy_count`.

    Failure isolation (T-41-03): the read runs inside a SAVEPOINT (``session.begin_nested()``). On
    ANY DB error the nested scope is rolled back ALONE -- recovering the aborted Postgres transaction
    WITHOUT expiring the dashboard's already-loaded ORM objects and WITHOUT poisoning later queries.
    The function logs a warning and returns 0 -- it NEVER raises into the hot 5s /pipeline/stats poll.
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(_STAGE_BUSY_SQL)).all()
    except Exception:
        logger.warning("match_busy_degraded", exc_info=True)
        return 0
    for row in rows:
        if row[0] == _MATCH_BUSY_FUNCTION:
            return int(row[1])
    return 0


async def get_scrape_pending_tracklists(session: AsyncSession) -> list[Tracklist]:
    """Return the Tracklist rows with NO ``tracklist_versions`` row (the complement of scrape.done).

    The EXACT complement of :func:`get_stage_progress`'s ``scrape.done``
    (``COUNT(DISTINCT TracklistVersion.tracklist_id)``): a tracklist that already has any scraped
    version is excluded, so a bulk scrape over this set skips already-done rows (idempotent re-runs;
    the deterministic ``tracklist_id`` key additionally dedups in-flight replays). Pure ORM
    ``~exists(...)`` with NO interpolated operator input (T-41-01).
    """
    stmt = select(Tracklist).where(~exists(select(TracklistVersion.id).where(TracklistVersion.tracklist_id == Tracklist.id)))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_match_pending_tracklists(session: AsyncSession) -> list[Tracklist]:
    """Return the Tracklist rows NOT reachable from ``discogs_links`` (the complement of match.done).

    The EXACT complement of :func:`get_stage_progress`'s ``match.done`` (DISTINCT tracklist_id walked
    ``discogs_links -> tracklist_tracks -> tracklist_versions``): a tracklist whose version→track→link
    chain exists is excluded. A tracklist with a scraped version but no discogs link is still
    match-pending (scrape and match are independent stages). Pure ORM ``.not_in(subquery)`` with NO
    interpolated operator input (T-41-01).
    """
    matched_subq = (
        select(TracklistVersion.tracklist_id)
        .select_from(DiscogsLink)
        .join(TracklistTrack, DiscogsLink.track_id == TracklistTrack.id)
        .join(TracklistVersion, TracklistTrack.version_id == TracklistVersion.id)
    )
    stmt = select(Tracklist).where(Tracklist.id.not_in(matched_subq))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_active_agents(session: AsyncSession, kind: str | None = None) -> int:
    """Return the number of online agents (``revoked_at IS NULL`` AND ``last_seen_at IS NOT NULL``).

    Counts agents matching :func:`phaze.services.enqueue_router.select_active_agent`'s EXACT
    liveness definition (CONTEXT decision 2 -- do NOT invent a new liveness rule): a revoked agent
    (``revoked_at`` set) and a never-seen agent (``last_seen_at`` None) are both excluded. This drives
    the DAG Fingerprint-Scan node's "Needs agent" gate -- ``scan_live_set`` is a per-agent task and
    raises ``NoActiveAgentError`` when no agent is online, so the button must stay disabled until one
    is.

    Phase 58 (58-04, WORK-03): when ``kind`` is given (``"compute"`` / ``"fileserver"``) the count is
    scoped to agents of that ``Agent.kind`` -- the SAME liveness predicate, restricted to the kind.
    This mirrors :func:`phaze.services.enqueue_router.select_active_agent`'s ``kind`` arg (the canonical
    compute-online seam -- do NOT invent a second rule) and drives the Analyze A1 lane's ``computeOnline``
    capacity numeral. ``kind=None`` preserves the original any-kind behavior, so every existing caller is
    unchanged.

    Failure isolation (T-40-05): the read runs inside a SAVEPOINT (``session.begin_nested()``) so a
    DB hiccup on the hot 5s poll does NOT expire the dashboard's loaded ORM objects. On ANY exception
    it logs ``active_agent_count_degraded`` and returns 0. That degrade default is FAIL-SAFE:
    ``agentOnline == 0`` leaves the new node blocked "Needs agent", so a liveness-read failure can
    never let a scan launch with no agent online. It NEVER raises into the 5s /pipeline/stats poll.
    """
    try:
        async with session.begin_nested():
            stmt = select(func.count(Agent.id)).where(Agent.revoked_at.is_(None), Agent.last_seen_at.is_not(None))
            if kind is not None:
                stmt = stmt.where(Agent.kind == kind)
            count = (await session.execute(stmt)).scalar()
    except Exception:
        logger.warning("active_agent_count_degraded", exc_info=True)
        return 0
    return int(count or 0)


async def get_files_by_state(session: AsyncSession, state: FileState) -> list[FileRecord]:
    """Get all files in a given pipeline state.

    Args:
        session: Async database session.
        state: The FileState to filter by.

    Returns:
        List of FileRecord objects in the given state.
    """
    stmt = select(FileRecord).where(FileRecord.state == state)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# --- Phase 58 (58-04, WORK-04 / D-03) all-in-stage Analyze file table read ----------------
#
# The states surfaced in the D-03 "one table of ALL in-stage Analyze files" table: the done
# bucket (ANALYZED), the three cloud lanes (AWAITING_CLOUD / PUSHING / PUSHED), and the
# terminal-failure bucket (ANALYSIS_FAILED). In-flight files (a partial 57.1 analysis row,
# not yet ANALYZED) are captured separately by the analysis-row-presence predicate below, so
# a running file appears even while still in its pre-analyze state.
_ANALYZE_STAGE_STATES = [
    FileState.ANALYZED,
    FileState.AWAITING_CLOUD,
    FileState.PUSHING,
    FileState.PUSHED,
    FileState.ANALYSIS_FAILED,
]


async def get_analyze_stage_files(session: AsyncSession) -> list[dict[str, Any]]:
    """Return the all-in-stage Analyze file rows for the workspace table (D-03), degrade-safe.

    ONE read-only multi-state SELECT (a pure read -- NO behavior change) over FileRecord, LEFT
    JOINing the per-file ``cloud_job`` sidecar (lane derivation), the 1:1 ``analysis`` aggregate
    (windowed coverage / the 57.1 mid-flight signal), and ``metadata`` (duration). Returns every
    file in the Analyze stage: queued/running (in-flight analysis row), awaiting-cloud, and done.

    Per-file lane is DERIVED (RESEARCH A1, confirmed against
    :func:`phaze.services.cloud_staging.stage_file_to_s3` -- the ONLY ``cloud_job`` writer, reached
    only on a cloud route): no ``cloud_job`` row -> ``local``; ``cloud_job`` with ``cloud_phase IS
    NULL`` -> ``a1``; ``cloud_job`` with ``cloud_phase`` set -> ``k8s``. A local-routed file never
    enters ``stage_file_to_s3``, so it never carries a ``cloud_job`` row and cannot be mislabeled.

    Window coverage reads ``analysis.fine_windows_analyzed`` / ``fine_windows_total``: a completed
    (ANALYZED) row shows full coverage from the aggregate; an in-flight row shows the merged 57.1
    mid-flight ``N/M`` signal (``fine_windows_analyzed < fine_windows_total``). Phase 58 only READS
    this signal (D-04) -- no schema/query-semantics change.

    Degrade-safe via a SAVEPOINT returning ``[]`` on any error (mirrors :func:`count_active_agents`):
    this read rides the hot dashboard context and must NEVER 500 the page / poll.
    """
    try:
        async with session.begin_nested():
            stmt = (
                select(
                    FileRecord.id,
                    FileRecord.original_filename,
                    FileRecord.original_path,
                    FileRecord.state,
                    CloudJob.id,
                    CloudJob.cloud_phase,
                    AnalysisResult.fine_windows_analyzed,
                    AnalysisResult.fine_windows_total,
                    FileMetadata.duration,
                )
                .select_from(FileRecord)
                .outerjoin(CloudJob, CloudJob.file_id == FileRecord.id)
                .outerjoin(AnalysisResult, AnalysisResult.file_id == FileRecord.id)
                .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
                .where(or_(FileRecord.state.in_(_ANALYZE_STAGE_STATES), AnalysisResult.id.is_not(None)))
                .order_by(FileRecord.created_at.desc())
            )
            rows = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("analyze_stage_files_degraded", exc_info=True)
        return []

    files: list[dict[str, Any]] = []
    for file_id, filename, path, state, cloud_job_id, cloud_phase, fine_done, fine_total, duration in rows:
        if cloud_job_id is None:
            lane = "local"
        elif cloud_phase is None:
            lane = "a1"
        else:
            lane = "k8s"
        files.append(
            {
                # Phase 61 (RECORD-01): the row->record slide-in opener keys on this file_id
                # (hx-get="/record/{file_id}"); str() so the template renders the UUID inline.
                "file_id": str(file_id),
                "filename": filename,
                "path": path,
                "state": state,
                "lane": lane,
                "fine_done": fine_done,
                "fine_total": fine_total,
                "duration": duration,
                "completed": state == FileState.ANALYZED,
            }
        )
    return files


# --- Phase 59 (59-01, IDENT-01/IDENT-02) Identify-workspace read-only row assembly ----------
#
# The two genuinely-new pieces of Phase 59 (RESEARCH "Don't Hand-Roll" key insight): per-row
# presentation data for the Track-ID combined table and the Tracklist per-set table. Both are
# PURE READS over existing, already-populated tables (no enqueue, no commit, no schema change) and
# both degrade to ``[]`` inside a SAVEPOINT on any error, mirroring :func:`get_analyze_stage_files`
# -- they ride the hot render/poll path and must NEVER 500 the page.

# The PERSISTED lowercase engine vocab (RESEARCH Pitfall 1, traced adapter -> API -> DB write):
# AudfprintAdapter.name / PanakoAdapter.name. The UI label is "Panako" but the stored value is
# lowercase "panako". Used as the per-engine join keys for the Track-ID badges.
_TRACKID_ENGINE_AUDFPRINT = "audfprint"
_TRACKID_ENGINE_PANAKO = "panako"


def _trackid_engine_badge(status: str | None) -> str:
    """Map a persisted ``FingerprintResult.status`` to the D-01 per-engine badge word.

    done <=> ``status == "success"`` (the value the engine adapters actually write via
    ``put_fingerprint``; ``"completed"`` is tolerated defensively but is NEVER written by that
    path -- RESEARCH Pitfall 1); failed <=> ``"failed"``; pending <=> no row for ``(file, engine)``
    (a missing join -> ``status is None`` -- RESEARCH Pitfall 2).
    """
    if status in ("success", "completed"):
        return "done"
    if status == "failed":
        return "failed"
    return "pending"


async def get_trackid_stage_files(session: AsyncSession) -> list[dict[str, Any]]:
    """Return the per-file Track-ID identity-signal rows for the combined table (IDENT-01), degrade-safe.

    ONE read-only SELECT (a pure read -- NO behavior change) over the signal-bearing set: music/video
    files that carry at least one ``FingerprintResult`` row OR a linked ``Tracklist`` (RESEARCH
    Open-Q2). Each row carries the per-engine fingerprint badge words (audfprint / panako, D-01) and
    the tracklist match-state + confidence (D-04).

    Per-engine badge (D-01, Pitfall 1/2): two aliased LEFT joins keyed on the lowercase persisted
    ``engine`` values map ``status == "success"`` -> ``"done"``, ``"failed"`` -> ``"failed"``, and a
    missing row -> ``"pending"`` (see :func:`_trackid_engine_badge`).

    Tracklist match-state (D-04): a tracklist LINKED to this file (``Tracklist.file_id == files.id``)
    -> ``"matched"`` + that linked tracklist's ``match_confidence`` (best via
    ``match_confidence desc nulls_last``); else, if any unlinked candidate tracklist exists in the
    system -> ``"candidate"`` + the global best candidate ``match_confidence`` (the
    ``match_confidence.desc().nulls_last()`` ordering ``list_tracklists`` already uses); else
    ``"no match"`` with confidence ``None``. NOTE: with the current schema a candidate
    (``file_id IS NULL``) is not tied to a specific file, so the candidate fallback surfaces the
    system-wide best candidate -- the literal D-04 reading; Plan 59-02 renders it and may refine if
    UI-SPEC requires per-file candidates.

    Degrade-safe via a SAVEPOINT returning ``[]`` on any error (mirrors :func:`get_analyze_stage_files`).
    """
    try:
        async with session.begin_nested():
            # D-04 fallback: the system-wide best unlinked candidate (highest match_confidence).
            best_candidate = (
                await session.execute(
                    select(Tracklist.match_confidence)
                    .where(Tracklist.file_id.is_(None))
                    .order_by(Tracklist.match_confidence.desc().nulls_last())
                    .limit(1)
                )
            ).scalar_one_or_none()
            has_candidate = bool((await session.execute(select(exists(select(Tracklist.id).where(Tracklist.file_id.is_(None)))))).scalar())

            # Per-file best LINKED tracklist confidence (D-04 "matched" branch).
            linked_conf_subq = (
                select(
                    Tracklist.file_id.label("file_id"),
                    func.max(Tracklist.match_confidence).label("conf"),
                )
                .where(Tracklist.file_id.is_not(None))
                .group_by(Tracklist.file_id)
                .subquery()
            )

            audfprint = aliased(FingerprintResult)
            panako = aliased(FingerprintResult)
            stmt = (
                select(
                    FileRecord.original_filename,
                    FileRecord.original_path,
                    audfprint.status,
                    panako.status,
                    linked_conf_subq.c.file_id,
                    linked_conf_subq.c.conf,
                )
                .select_from(FileRecord)
                .outerjoin(audfprint, and_(audfprint.file_id == FileRecord.id, audfprint.engine == _TRACKID_ENGINE_AUDFPRINT))
                .outerjoin(panako, and_(panako.file_id == FileRecord.id, panako.engine == _TRACKID_ENGINE_PANAKO))
                .outerjoin(linked_conf_subq, linked_conf_subq.c.file_id == FileRecord.id)
                .where(
                    FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
                    or_(
                        exists(select(FingerprintResult.id).where(FingerprintResult.file_id == FileRecord.id)),
                        exists(select(Tracklist.id).where(Tracklist.file_id == FileRecord.id)),
                    ),
                )
                .order_by(FileRecord.created_at.desc())
            )
            rows = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("trackid_stage_files_degraded", exc_info=True)
        return []

    files: list[dict[str, Any]] = []
    for filename, path, af_status, pk_status, linked_file_id, linked_conf in rows:
        if linked_file_id is not None:
            tracklist_state = "matched"
            confidence = linked_conf
        elif has_candidate:
            tracklist_state = "candidate"
            confidence = best_candidate
        else:
            tracklist_state = "no match"
            confidence = None
        files.append(
            {
                "filename": filename,
                "path": path,
                "audfprint_status": _trackid_engine_badge(af_status),
                "panako_status": _trackid_engine_badge(pk_status),
                "tracklist_state": tracklist_state,
                "confidence": confidence,
            }
        )
    return files


async def get_tracklist_set_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Return the per-set Tracklist rows for the per-set coverage table (IDENT-02 / D-07/D-08), degrade-safe.

    ONE read-only SELECT (a pure read -- NO behavior change), one row per ``Tracklist`` (a "set").
    Each row carries the set name + path, the match-state + ``matched_to_file`` flag, and the D-07
    per-set track coverage: ``tracks_confident`` of ``tracks_total`` derived from
    ``TracklistTrack.confidence`` over the tracklist's versioned tracks (``COUNT(confidence)`` counts
    only non-NULL confidences -> the confident N; ``COUNT(id)`` -> the total M).

    A tracklist LINKED to a file (``file_id IS NOT NULL``) -> ``"matched"`` + the file's name/path;
    an unlinked tracklist -> ``"candidate"`` (set name falls back to artist / event / external_id,
    path ``None``). The track counts are scoped to the tracklist's ``latest_version_id`` only (the
    same convention the tracklists router uses) -- a re-scraped tracklist with multiple versions must
    NOT sum coverage across versions, which would inflate the D-07 N/M. A tracklist whose
    ``latest_version_id`` is NULL reports 0/0.

    Degrade-safe via a SAVEPOINT returning ``[]`` on any error (mirrors :func:`get_analyze_stage_files`).
    """
    try:
        async with session.begin_nested():
            track_counts_subq = (
                select(
                    TracklistTrack.version_id.label("version_id"),
                    func.count(TracklistTrack.id).label("total"),
                    func.count(TracklistTrack.confidence).label("confident"),
                )
                .group_by(TracklistTrack.version_id)
                .subquery()
            )
            stmt = (
                select(
                    Tracklist.external_id,
                    Tracklist.artist,
                    Tracklist.event,
                    Tracklist.file_id,
                    FileRecord.original_filename,
                    FileRecord.original_path,
                    track_counts_subq.c.total,
                    track_counts_subq.c.confident,
                )
                .select_from(Tracklist)
                .outerjoin(FileRecord, FileRecord.id == Tracklist.file_id)
                .outerjoin(track_counts_subq, track_counts_subq.c.version_id == Tracklist.latest_version_id)
                .order_by(Tracklist.created_at.desc())
            )
            rows = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("tracklist_set_rows_degraded", exc_info=True)
        return []

    sets: list[dict[str, Any]] = []
    for external_id, artist, event, file_id, filename, path, total, confident in rows:
        matched = file_id is not None
        set_name = filename if matched else (artist or event or external_id)
        sets.append(
            {
                "set_name": set_name,
                "path": path,
                "tracklist_state": "matched" if matched else "candidate",
                "tracks_confident": int(confident or 0),
                "tracks_total": int(total or 0),
                "matched_to_file": matched,
            }
        )
    return sets


# --- ANALYSIS_FAILED bucket (Phase 44, D-02) --------------------------------------------
#
# The files that GAVE UP -- terminal windowed-analysis failure (Phase 43 sets
# FileState.ANALYSIS_FAILED). This is its OWN bucket, intentionally ABSENT from
# PIPELINE_STAGES (lines 40-49): adding it there would double-count failed files in the
# linear stat bar. Surfaced on the dashboard alongside the STRAGGLER bucket (still grinding)
# as two distinct outcomes of the 4h-timeout incident. Reads the indexed files.state
# (ix_files_state, models/file.py:74) -- NOT saq_jobs (a failed file has no live job).


async def get_analysis_failed_files(session: AsyncSession) -> list[FileRecord]:
    """Return the FileRecords in ``FileState.ANALYSIS_FAILED`` (the analysis-gave-up bucket).

    A one-liner reuse of :func:`get_files_by_state` (D-02): the failed list reads the indexed
    ``files.state = 'analysis_failed'`` directly. Distinct from the STRAGGLER bucket
    (:func:`get_straggler_count`, still-running jobs from ``saq_jobs``) -- these files have
    terminally failed and carry no live job.
    """
    return await get_files_by_state(session, FileState.ANALYSIS_FAILED)


async def get_analysis_failed_count(session: AsyncSession) -> int:
    """Return COUNT of files in ``FileState.ANALYSIS_FAILED``, degrading to 0 on any DB error.

    Poll-safe via :func:`_safe_count` (the standard stage-count degrade discipline): a DB hiccup
    degrades this node to 0 and rolls back the aborted transaction rather than 500ing the hot 5s
    /pipeline/stats poll. ``ANALYSIS_FAILED`` is its
    own bucket and is deliberately NOT added to ``PIPELINE_STAGES`` (D-02 -- it would double-count
    in the linear bar).
    """
    return await _safe_count(
        session,
        # Phase 90 (PR-A, D-09): DERIVED from the analyze-failure marker (analysis.failed_at NOT NULL)
        # via the LOCKED ``failed_clause`` builder -- no longer the ``files.state`` column. Composes the
        # clause verbatim (never re-spells the inner exists) so the DERIV-04 equivalence guarantee holds.
        select(func.count(FileRecord.id)).where(failed_clause(Stage.ANALYZE)),
        node="analysis_failed",
    )


# --- Phase 49 duration-routing read helpers (D-05, D-09/D-10) ---------------------------
#
# The primitives the per-file router (Plan 02), backfill (Plan 03), and release cron
# (Plan 04) compose against. All three JOIN files -> metadata on FileMetadata.duration:
# FileRecord.file_metadata is lazy="noload" (models/file.py), so duration MUST be captured
# in-memory via an explicit SELECT before any background task reads it (a later lazy access
# off-session would raise). The backfill predicate filters ANALYSIS_FAILED *AND*
# duration >= threshold -- it deliberately does NOT reuse get_analysis_failed_count, which
# over-counts short/null-duration failures and would re-trigger the over-enqueue class.


async def get_discovered_files_with_duration(session: AsyncSession) -> list[tuple[FileRecord, float | None]]:
    """Return ``(FileRecord, duration)`` for every analyze-pending music/video file (LEFT OUTER JOIN metadata).

    READ-01 cutover: the analyze pending set is now DERIVED, not gated on ``FileRecord.state ==
    DISCOVERED``. A file is analyze-pending iff it is a music/video type, is ``eligible_clause(ANALYZE)``
    (``~inflight ∧ ~done ∧ ~failed`` -- ELIG-03 keeps a FAILED analyze terminal, the 44.5K over-enqueue
    guard), is NOT dedup-resolved, and is NOT being handled by the cloud path (T-82-A1). This dissolves
    the cross-stage deadlock the old state gate created -- a file whose ``state`` advanced past
    ``DISCOVERED`` (e.g. to ``METADATA_EXTRACTED``) but was never analyzed re-surfaces here correctly.

    The ``file_type.in_(MUSIC_VIDEO_TYPES)`` scope is NEWLY required: the old state-gated query was
    file-type-agnostic, so without it a non-music DISCOVERED file would leak into the analyze set
    (Pitfall 1). The ``~exists(cloud_job in ACTIVE statuses)`` conjunct is the explicit A1 double-dispatch
    guard -- see ``_ACTIVE_CLOUD_STATUSES``: a cloud-held/pushing file carries NO ``process_file`` ledger
    row, so ``eligible_clause``'s ``~inflight`` alone would re-admit it to the local analyze set.

    The duration is the joined ``FileMetadata.duration`` (or ``None`` when no metadata row exists yet).
    The LEFT OUTER JOIN is PRESERVED (the per-file cloud duration-router reads ``FileMetadata.duration``);
    it is captured into the in-memory list here because ``FileRecord.file_metadata`` is ``lazy="noload"``
    -- a later access in a background task would NOT lazy-load it.
    """
    stmt = (
        select(FileRecord, FileMetadata.duration)
        .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .where(
            FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
            eligible_clause(Stage.ANALYZE),
            ~dedup_resolved_clause(),
            ~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status.in_(_ACTIVE_CLOUD_STATUSES))),
        )
    )
    result = await session.execute(stmt)
    return [(record, duration) for record, duration in result.all()]


async def get_awaiting_cloud_count(session: AsyncSession) -> int:
    """Return COUNT of genuinely-parked awaiting cloud_job rows, degrading to 0 on any DB error (Phase 83, D-15).

    Drives the dashboard "Awaiting cloud" card. Re-anchored off the retired
    ``FileRecord.state == AWAITING_CLOUD`` display read onto the SAME clause the drain
    (:func:`get_cloud_staging_candidates`) uses -- ``COUNT(cloud_job) WHERE status='awaiting' AND
    ~inflight_clause(ANALYZE) AND ~domain_completed_clause(ANALYZE)`` -- so the card counts exactly the
    rows the drain would pick and the two can NEVER disagree. A LOCAL_ANALYZING long file that still
    carries its inert awaiting row (D-13 keeps the flip; D-14 reaps the row at the analyze-terminal seam)
    is excluded from BOTH by ``~inflight_clause``, so it never inflates the card. Composes the LOCKED
    clause builders verbatim (DERIV-04). Poll-safe via :func:`_safe_count` (mirrors
    :func:`get_analysis_failed_count`): a DB hiccup degrades this node to 0 and rolls back the aborted
    transaction rather than 500ing the hot 5s /pipeline/stats poll.
    """
    return await _safe_count(
        session,
        # INNER-join FileRecord so the correlated ``~exists(... file_id == FileRecord.id)`` clause builders
        # resolve (they reference FileRecord.id); cloud_job.file_id is unique, so the join is 1:1 and the
        # COUNT matches the drain's candidate set exactly.
        select(func.count(CloudJob.id)).select_from(CloudJob).join(FileRecord, FileRecord.id == CloudJob.file_id).where(awaiting_candidate_clause()),
        node="awaiting_cloud",
    )


async def get_inadmissible_count(session: AsyncSession) -> int:
    """Return COUNT of ``cloud_job`` rows flagged ``inadmissible``, degrading to 0 on any DB error.

    Drives the dashboard Inadmissible operator alert (D-06, KSUBMIT-04): a non-zero count means
    one or more Kueue Workloads are Inadmissible (a misconfigured LocalQueue/ClusterQueue), which
    the reconcile cron (Plan 06) stamps onto the row. A healthy quota wait (``Pending``) never
    sets the flag, so this count stays 0 and the alert stays silent. Poll-safe via
    :func:`_safe_count` (mirrors :func:`get_awaiting_cloud_count`): a DB hiccup degrades this node
    to 0 and rolls back the aborted transaction rather than 500ing the hot 5s /pipeline/stats poll
    (T-54-10).
    """
    return await _safe_count(
        session,
        # CR-01: scope to in-flight rows so a terminal row that was transiently Inadmissible (and whose
        # flag the reconcile cron clears anyway) can never inflate the alert -- belt-and-suspenders.
        select(func.count(CloudJob.id)).where(
            CloudJob.inadmissible.is_(True),
            CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
        ),
        node="inadmissible",
    )


async def get_localqueue_unreachable(redis: Any) -> bool:
    """Return True when the controller flagged the Kueue LocalQueue unreachable, degrading to False.

    Drives the dashboard amber "K8s LocalQueue unreachable" alert (D-05, KDEPLOY-04). The WRITER is
    the ``controller.startup`` probe (D-06): on a reachability failure it sets the cross-process Redis
    key ``phaze:k8s:localqueue_unreachable``; on success it deletes it. This is the degrade-safe READER
    the api process consumes -- it returns False when ``redis`` is None (the test client skips the
    lifespan so ``app.state.redis`` is absent) AND on ANY Redis error, logging a warning but NEVER
    propagating. The hot 5s ``/pipeline/stats`` poll must never 500 on a Redis hiccup (T-54-10); the
    alert simply stays silent (reachable) instead.
    """
    if redis is None:
        return False
    try:
        return bool(await redis.exists("phaze:k8s:localqueue_unreachable"))
    except Exception:
        logger.warning("localqueue_unreachable_read_degraded", exc_info=True)
        return False


async def get_cloud_phase_counts(session: AsyncSession) -> dict[str, int]:
    """Return per-``cloud_phase`` counts for the dashboard admission-state card, each degrading to 0.

    Drives the KROUTE-06 admission-state card (D-04): four COUNT(cloud_job) reads grouped by the
    Kueue admission progression (``queued_behind_quota`` -> ``admitted`` -> ``running`` ->
    ``finished``). Each count is an independent :func:`_safe_count`-backed read with a distinct
    ``node=`` tag, mirroring :func:`get_inadmissible_count`: a DB hiccup degrades THAT phase to 0
    (and rolls back the aborted transaction) rather than 500ing the hot 5s ``/pipeline/stats`` poll
    (T-55-CARD-01). The card then renders the quiet empty carrier.

    ``cloud_phase`` is NULL for a1/local rows (admission is a k8s-only concept), so those rows count
    toward NONE of the four phases — all-zero leaves the card a quiet empty carrier on non-k8s deploys.
    """
    return {
        "queued_behind_quota": await _safe_count(
            session,
            select(func.count(CloudJob.id)).where(CloudJob.cloud_phase == CloudPhase.QUEUED_BEHIND_QUOTA.value),
            node="cloud_phase_queued_behind_quota",
        ),
        "admitted": await _safe_count(
            session,
            select(func.count(CloudJob.id)).where(CloudJob.cloud_phase == CloudPhase.ADMITTED.value),
            node="cloud_phase_admitted",
        ),
        "running": await _safe_count(
            session,
            select(func.count(CloudJob.id)).where(CloudJob.cloud_phase == CloudPhase.RUNNING.value),
            node="cloud_phase_running",
        ),
        "finished": await _safe_count(
            session,
            select(func.count(CloudJob.id)).where(CloudJob.cloud_phase == CloudPhase.FINISHED.value),
            node="cloud_phase_finished",
        ),
    }


async def get_pushing_count(session: AsyncSession) -> int:
    """Return COUNT of the "pushing" half of the bounded cloud window, degrading to 0 (D-09/D-12).

    Phase 90 (PR-A): DERIVED from ``cloud_job.status IN ('uploading','submitted')`` -- no longer the
    retired ``files.state == PUSHING`` column. Drives the dashboard "Staged (pushing)" card -- the left
    half of the bounded cloud window (files mid-upload / just handed to remote submit). Poll-safe via
    :func:`_safe_count`
    (mirrors :func:`get_awaiting_cloud_count`): a DB hiccup degrades this node to 0 and rolls back
    the aborted transaction rather than 500ing the hot 5s /pipeline/stats poll. This is the
    OBSERVATIONAL per-card count -- the load-bearing backpressure is now per-backend
    ``Backend.in_flight_count`` (Phase 69, D-05), which the drain reads once per tick and which is
    intentionally NOT degrade-safe so the drain never over-dispatches on a transient error.
    """
    return await _safe_count(
        session,
        # Phase 90 (PR-A, D-12): DERIVED from the ``cloud_job`` sidecar -- the "pushing" half of the
        # bounded cloud window is a cloud_job mid-upload (``uploading``) or handed to the remote submit
        # but not yet landed (``submitted``). Mirrors :func:`get_inadmissible_count`'s cloud_job read;
        # no longer the ``files.state == PUSHING`` column.
        select(func.count(CloudJob.id)).where(CloudJob.status.in_([CloudJobStatus.UPLOADING.value, CloudJobStatus.SUBMITTED.value])),
        node="pushing",
    )


async def get_pushed_count(session: AsyncSession) -> int:
    """Return COUNT of the "pushed / analyzing" half of the bounded cloud window, degrading to 0 (D-09/D-12).

    Phase 90 (PR-A): DERIVED from ``cloud_job.status IN ('uploaded','running')`` -- no longer the retired
    ``files.state == PUSHED`` column. Drives the dashboard "Analyzing (cloud)" card -- the right half of
    the bounded cloud window (files that finished upload and are awaiting/within remote analysis). Poll-safe via
    :func:`_safe_count`, exactly like :func:`get_pushing_count`. Observational only; the per-backend
    cap itself is enforced by ``Backend.in_flight_count`` (Phase 69, D-05) from committed cloud_job rows.
    """
    return await _safe_count(
        session,
        # Phase 90 (PR-A, D-12): DERIVED from the ``cloud_job`` sidecar -- the "pushed / analyzing"
        # half of the bounded cloud window is a cloud_job that finished upload (``uploaded``) or is
        # actively analyzing on the remote (``running``). Mirrors :func:`get_pushing_count`; no longer
        # the ``files.state == PUSHED`` column.
        select(func.count(CloudJob.id)).where(CloudJob.status.in_([CloudJobStatus.UPLOADED.value, CloudJobStatus.RUNNING.value])),
        node="analyzing_cloud",
    )


# --- Phase 50 bounded cloud-window helpers (D-03/D-08, CLOUDPIPE-01) ---------------------
#
# Phase 69 (D-05, SCHED-02) retired the global FileState-window count in favor of per-backend
# ``Backend.in_flight_count`` (a ``cloud_job``-derived COUNT scoped by ``backend_id``). The
# ``stage_cloud_window`` drain now snapshots each backend's free capacity once per tick and SELECTs
# candidates via ``get_cloud_staging_candidates`` below -- still ``FOR UPDATE SKIP LOCKED`` in ONE
# transaction so a concurrent tick cannot double-stage the same row (T-50-scratch-dos).


async def get_cloud_staging_candidates(session: AsyncSession, limit: int) -> list[tuple[FileRecord, datetime]]:
    """Return up to ``limit`` oldest genuinely-parked cloud candidates + each row's staleness clock (Phase 83, D-05/D-06/D-07).

    Cut over from the retired ``FileRecord.state == AWAITING_CLOUD`` read (SC#1) to the ``cloud_job``
    sidecar + the derived ``in_flight(analyze)`` layer. A candidate is a file that:

    * carries a ``cloud_job(status='awaiting')`` sidecar row (INNER join -- D-05 conjunct 1), AND
    * is NOT analyze-in-flight (``~inflight_clause(ANALYZE)`` -- D-05 conjunct 2). A locally-dispatched
      file whose ``process_file`` ledger row is committed is excluded, and that exclusion SURVIVES a
      whole-tick rollback because the ledger row was committed by the ``before_enqueue`` hook's OWN
      session -- the exact reason D-05 chose a predicate conjunct over deleting the awaiting row (a
      deleted row restored on the rollback would re-pick the file and could cloud-dispatch it, the
      double-dispatch SC#3 forbids). AND
    * has NOT domain-completed its analyze (``~domain_completed_clause(ANALYZE)`` -- D-05 conjunct 3):
      ``FAILURE_IS_TERMINAL[analyze]`` is True, so a terminally-failed local analyze is domain-complete
      and never re-driven (the Phase-81 twin the ROADMAP dep-note names).

    Composes the LOCKED ``inflight_clause`` / ``domain_completed_clause`` builders VERBATIM -- re-spelling
    either breaks the DERIV-04 equivalence test (``tests/integration/test_stage_status_equivalence.py``).

    FIFO stays on the immutable ``FileRecord.created_at`` (D-07 -- byte-identical discovery order to the
    pre-cutover query; a file discovered months ago but held today still sorts to the front). The per-row
    ``cloud_job.updated_at`` is surfaced alongside each candidate as the lane-entry staleness clock the
    caller passes into ``select_backend`` (D-07): it lives on the awaiting row rather than
    ``file.updated_at`` so Phase 90's removal of the dual-written ``file.state`` cannot silently break the
    ``cloud_route_max_wait_sec`` spill clock.

    D-06: the lock moves to the candidacy table -- ``with_for_update(of=CloudJob, skip_locked=True)`` over
    the INNER join so Postgres re-evaluates ``cloud_job``'s ``WHERE`` after acquiring the lock (EvalPlanQual);
    locking only ``files`` would read the deciding ``cloud_job.status`` column stale against the concurrent
    callback routers / reconcile cron the tick's advisory lock does not cover. INNER (not outer) join is
    required -- Postgres rejects ``FOR UPDATE`` on the nullable side of an outer join. ``limit`` is the
    free-slot count the caller computed as ``sum(remaining)`` across available backends; the caller must
    guarantee ``limit > 0`` (a ``LIMIT 0`` would be a pointless round-trip).
    """
    stmt = (
        select(FileRecord, CloudJob.updated_at)
        .join(CloudJob, CloudJob.file_id == FileRecord.id)
        .where(awaiting_candidate_clause())
        .order_by(FileRecord.created_at.asc())
        .limit(limit)
        .with_for_update(of=CloudJob, skip_locked=True)
    )
    return [(file, updated_at) for file, updated_at in (await session.execute(stmt)).all()]


def _backfill_candidates_stmt(threshold_sec: int) -> Select[Any]:
    """Build the ANALYSIS_FAILED + ``duration >= threshold_sec`` + ledger-scoped candidate predicate.

    INNER JOIN ``FileMetadata`` so a null-duration ANALYSIS_FAILED file is structurally
    excluded; the ``duration >= threshold_sec`` filter then drops short failures. ``threshold_sec``
    is a bound int parameter (T-49-02) -- never interpolated SQL.

    Phase 55 (L4 / D-03 / KROUTE-05): an ``EXISTS`` predicate against ``scheduling_ledger`` keyed
    ``'process_file:' || file.id`` scopes candidates to **previously-scheduled work only**. A SAQ
    timeout abandons a long ``process_file`` job WITHOUT firing ``report_analysis_failed`` (which
    clears the row), so the orphaned ledger row persists into ``ANALYSIS_FAILED`` -- exactly the
    timed-out set this backfill re-drives. A never-scheduled (or cleanly report-failed, row-cleared)
    failure has NO ledger row and is excluded, preventing the v4.0.6 / v5.0 whole-backlog
    over-enqueue class. ORM / bound params only -- the key is concatenated via ``cast`` + a bound
    literal, never f-string SQL (T-49-02 / T-55-BF-04).
    """
    return (
        select(FileRecord, FileMetadata.duration)
        .join(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .where(
            FileRecord.state == FileState.ANALYSIS_FAILED,
            FileMetadata.duration >= threshold_sec,
            exists(select(SchedulingLedger.key).where(SchedulingLedger.key == "process_file:" + cast(FileRecord.id, String))),
        )
    )


async def count_backfill_candidates(session: AsyncSession, threshold_sec: int) -> int:
    """Return COUNT of ANALYSIS_FAILED files whose joined duration >= ``threshold_sec``.

    This is the explicit filter that closes the over-enqueue class (D-09/D-10): it is NOT
    :func:`get_analysis_failed_count` (which counts ALL ANALYSIS_FAILED, including short and
    null-duration failures that must never be cloud-routed). Poll-safe via :func:`_safe_count`.
    """
    return await _safe_count(
        session,
        select(func.count()).select_from(_backfill_candidates_stmt(threshold_sec).subquery()),
        node="backfill_candidates",
    )


async def get_backfill_candidates(session: AsyncSession, threshold_sec: int) -> list[tuple[FileRecord, float | None]]:
    """Return ``(FileRecord, duration)`` for the same ANALYSIS_FAILED + duration>=threshold set.

    The list form the backfill producer (Plan 03) iterates to re-route long failed files to a
    cloud compute agent. duration is captured in-memory (FileRecord.file_metadata is
    ``lazy="noload"``) so a downstream background task never triggers a lazy load.
    """
    result = await session.execute(_backfill_candidates_stmt(threshold_sec))
    return [(record, duration) for record, duration in result.all()]


# --- Shared pending-set helpers (Phase 42, D-03 anti-drift) -----------------------------
#
# ONE definition of "pending" per stage, consumed by BOTH the Phase 39-41 manual DAG
# triggers (routers/pipeline.py) AND the Phase-42 recovery producer
# (tasks/reenqueue.recover_orphaned_work). Recovery and the manual triggers MUST read the
# SAME query so the two paths cannot drift apart (D-03): an identical pending set funnelled
# through the IDENTICAL keyed producer yields the IDENTICAL deterministic key, so a recovery
# re-enqueue dedups cleanly against any surviving in-flight job (no doubling, Phase-32 class).
# All queries are pure ORM / bound params -- NO f-string SQL (T-42-03).


async def get_metadata_pending_files(session: AsyncSession) -> list[FileRecord]:
    """Return the DERIVED metadata-extraction pending set -- music/video files eligible for metadata (READ-01).

    The EXACT set the manual metadata triggers (``trigger_metadata_extraction`` /
    ``trigger_extraction_ui``) and the Phase-42 recovery producer enqueue. READ-01 cutover: DERIVED from
    ``eligible_clause(METADATA)`` (``~inflight ∧ ~done`` -- ``ELIGIBLE_AFTER_FAILURE[METADATA]`` is True,
    so a FAILED metadata row stays eligible for the ELIG-04 auto-retry) instead of the prior
    state-agnostic "every music/video file", and excludes dedup-resolved files. A file whose metadata is
    genuinely done (a row present with ``failed_at`` NULL) drops out; a not-started or failed one stays.
    Pure ORM / bound params, NO interpolated operator input (T-42-03).
    """
    stmt = select(FileRecord).where(
        FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
        eligible_clause(Stage.METADATA),
        ~dedup_resolved_clause(),
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_metadata_failed_files(session: AsyncSession) -> list[FileRecord]:
    """Return every FileRecord carrying a terminal metadata failure row (FAIL-03 retry set).

    A metadata failure is persisted by the 81-03 writer as a ``metadata`` row with
    ``failed_at`` set and the payload columns NULL, so ``done(metadata)`` derives FAILED rather
    than DONE. This reuses the ``failed_clause(Stage.METADATA)`` shape (services/stage_status.py)
    -- a correlated ``exists(select(FileMetadata.id).where(file_id == FileRecord.id,
    FileMetadata.failed_at IS NOT NULL))`` -- so the operator bulk-retry endpoint re-enqueues
    EXACTLY the set the derivation reports as terminally failed. Pure ORM / bound params, NO
    f-string SQL (T-42-03).

    D-11: this returns the files; the retry LEAVES the failure row in place and re-enqueues --
    ``put_metadata``'s clear-on-success (81-03) wipes ``failed_at`` only when real metadata lands.
    """
    stmt = select(FileRecord).where(exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id, FileMetadata.failed_at.isnot(None))))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_fingerprint_pending_files(session: AsyncSession) -> list[FileRecord]:
    """Return the DERIVED fingerprint pending set -- music/video files eligible for fingerprinting (READ-01).

    The EXACT set the manual ``trigger_fingerprint`` / ``trigger_fingerprint_ui`` endpoints and the
    Phase-42 recovery producer enqueue. READ-01 cutover: DERIVED from ``eligible_clause(FINGERPRINT)`` in
    a SINGLE ``.where(...)`` -- the prior ``get_files_by_state(METADATA_EXTRACTED)`` UNION with the
    failed-retry sub-select AND the manual de-dup-by-id loop are COLLAPSED. This loses no coverage:
    ``ELIGIBLE_AFTER_FAILURE[FINGERPRINT]`` is True, so ``eligible_clause`` is ``~inflight ∧ ~done``
    (it drops the ``~failed`` conjunct), which subsumes the old failed-retry set -- a failed-only
    fingerprint (DERIV-05: no engine ``success``/``completed``) is NOT ``done`` and therefore stays
    eligible (ELIG-04 auto-retry). A single ``.where`` cannot emit a duplicate row, so the de-dup loop is
    unnecessary. Dedup-resolved files are excluded. Pure ORM / bound params, NO interpolated operator
    input (T-42-03).
    """
    stmt = select(FileRecord).where(
        FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
        eligible_clause(Stage.FINGERPRINT),
        ~dedup_resolved_clause(),
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_untracked_files(session: AsyncSession) -> list[FileRecord]:
    """Return music/video FileRecords with NO ``Tracklist`` row -- the search/scan pending set.

    The EXACT set BOTH the Phase-39 name-search trigger (``trigger_search_ui``) and the
    Phase-40 fingerprint-scan trigger (``trigger_scan_live_sets_ui``) enqueue: a music/video
    file that does not yet have a ``Tracklist`` (already-matched files are skipped so re-runs
    are cheap and idempotent). Pure ORM ``~exists(...)`` with NO interpolated operator input
    (T-42-03).
    """
    stmt = select(FileRecord).where(
        FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
        ~exists(select(Tracklist.id).where(Tracklist.file_id == FileRecord.id)),
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_proposal_pending_batches(session: AsyncSession, batch_size: int) -> list[list[str]]:
    """Return the ``generate_proposals`` pending set as deterministic, sorted file-id batches.

    Runs the convergence query (files in ``{ANALYZED, METADATA_EXTRACTED}`` with BOTH a
    ``FileMetadata`` AND an ``AnalysisResult`` row -- the EXACT set the manual proposals
    triggers use), then SORTS the file-id strings before chunking into ``batch_size`` groups.

    Sorting BEFORE chunking is load-bearing (D-04, 42-RESEARCH Pitfall 2): ``generate_proposals``
    is keyed on ``generate_proposals:<sha256(sorted file_ids)>`` (an order-independent SET hash),
    so the manual trigger and recovery MUST produce the IDENTICAL batch MEMBERSHIP to land on the
    IDENTICAL key and dedup against an in-flight batch. Both paths call THIS helper, so their
    batches -- and therefore their set-hash keys -- are guaranteed to match. Pure ORM / bound
    params, NO f-string SQL (T-42-03).
    """
    stmt = (
        select(FileRecord)
        .where(FileRecord.state.in_([FileState.ANALYZED, FileState.METADATA_EXTRACTED]))
        .where(exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id)))
        # Phase 57.1 (D-03 KEY RISK): require the COMPLETION discriminator, not bare row-existence.
        # D-03 upserts a partial `analysis` row at analysis START (NULL aggregates, completed_at NULL)
        # while the file is still METADATA_EXTRACTED -- bare `exists(AnalysisResult)` would batch that
        # partial row into generate_proposals with NULL bpm/key/mood. `analysis_completed_at IS NOT
        # NULL` (stamped only in the put_analysis completion branch) gates it out; in-flight rows have
        # completed_at NULL.
        .where(
            exists(
                select(AnalysisResult.id).where(
                    AnalysisResult.file_id == FileRecord.id,
                    AnalysisResult.analysis_completed_at.isnot(None),
                )
            )
        )
    )
    result = await session.execute(stmt)
    file_ids = sorted(str(f.id) for f in result.scalars().all())
    return [file_ids[i : i + batch_size] for i in range(0, len(file_ids), batch_size)]


# --- Queue-loss detector (Phase 42, REQ-42-2) -------------------------------------------
#
# Static SQL counting saq_jobs rows in flight. After Phase 36 the SAQ broker is Postgres
# (saq_jobs), so queued/active jobs SURVIVE a controller restart -- a normal reboot loses
# nothing. A genuine queue-loss is the rare asymmetry "saq_jobs has zero queued/active rows
# while the domain DB still shows pending work" (truncate / restore-from-backup / fresh
# migration). This COUNT is the cheap loss signal. Parked/paused jobs use scheduled=SENTINEL
# but are STILL status='queued', so they ARE counted -- a paused-but-present queue is correctly
# NOT misread as lost (42-RESEARCH Open Q4). Static literals only -- the only interpolation-free
# operands are the status allowlist (T-42-03, mirroring the _STAGE_BUSY_SQL discipline).
_INFLIGHT_COUNT_SQL = text("SELECT COUNT(*) FROM saq_jobs WHERE status IN ('queued', 'active')")


async def count_inflight_jobs(session: AsyncSession) -> int:
    """Return COUNT(*) of ``saq_jobs`` rows with ``status IN ('queued', 'active')``, degrade-safe.

    The queue-loss detector for :func:`phaze.tasks.reenqueue.recover_orphaned_work`: a return of
    ``0`` while the domain DB shows pending work signals a genuine broker wipe (Phase-36 durability
    reframe). Parked/paused jobs (status still ``queued``) ARE counted, so a paused queue reads as
    present, not lost (42-RESEARCH Open Q4).

    Failure isolation (T-42-04): the read runs inside a SAVEPOINT (``session.begin_nested()``). On
    ANY DB error (a missing ``saq_jobs`` table in a pre-migration env, a DB hiccup) the nested scope
    is rolled back ALONE -- recovering the aborted Postgres transaction WITHOUT poisoning the outer
    session's later pending-set queries. It logs a warning and DEGRADES TO 0, never raising into the
    controller boot path. A degrade-to-0 false positive is backstopped by the deterministic-key
    dedup: a reconcile that fires on a non-empty queue collapses every live item to a skipped no-op,
    so it can never double the queue (T-42-05, accepted).
    """
    try:
        async with session.begin_nested():
            count = (await session.execute(_INFLIGHT_COUNT_SQL)).scalar()
    except Exception:
        logger.warning("inflight_count_degraded", exc_info=True)
        return 0
    return int(count or 0)


# --- Straggler detector (Phase 44, D-01) ------------------------------------------------
#
# A STRAGGLER is a `process_file` analyze job that is STILL RUNNING (status='active') but has
# been running longer than the configured threshold -- the "still grinding" complement of the
# ANALYSIS_FAILED bucket (gave up). saq_jobs has NO `started`/`touched` SQL column (PATTERNS.md
# banner / saq/queue/postgres_migrations.py): SAQ stores `started` (epoch MILLISECONDS,
# saq.utils.now()) INSIDE the serialized `job` BYTEA blob (saq/job.py:132). So the age predicate
# CANNOT be a `WHERE now() - started > threshold` SQL filter against a non-existent column.
# Instead this:
#   (1) selects ONLY the BYTEA blob for the SMALL active process_file set (static SQL, Shared
#       Pattern B -- the only literals are split_part, the 'active' status, and the
#       'process_file' prefix; no operator/threshold input is interpolated; T-44-05),
#   (2) deserializes each blob in Python the SAME way SAQ does on the default json serializer
#       (the project passes no custom dump/load to build_pipeline_queue, so the blob is a JSON
#       object with a top-level `started` int) and reads `started`,
#   (3) counts jobs whose started is set AND (now_ms - started)/1000 > threshold_sec; a
#       missing/None/0 started (not yet dequeued) is treated as not-yet-old and NOT counted.
# scheduled BIGINT is intentionally NOT used as the age source: it is reset to dequeue/now on the
# active transition but does NOT equal `started` after a retry, so it is not a reliable
# running-age signal (PATTERNS.md banner).
_STRAGGLER_ACTIVE_SQL = text("SELECT job FROM saq_jobs WHERE status = 'active' AND split_part(key, ':', 1) = 'process_file'")


def _job_started_ms(blob: object) -> int | None:
    """Read the SAQ `started` epoch-ms from a serialized job BYTEA blob, or None if unreadable.

    The default SAQ serializer is ``json.dumps`` (the project sets no custom dump/load on
    ``build_pipeline_queue``), so the blob is a JSON object carrying a top-level ``started`` int
    (epoch milliseconds, ``saq.utils.now()``; ``saq/job.py:132``). We parse the dict directly
    rather than constructing a ``saq.Job`` -- ``Queue.deserialize`` would require the live queue
    object and would raise on a queue-name mismatch; all we need is the one ``started`` field.
    A blob that is not JSON, not a dict, or lacks a positive ``started`` returns None (treated as
    not-yet-old by the caller, never counted).
    """
    try:
        data = json.loads(blob) if isinstance(blob, (str, bytes, bytearray)) else blob
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    started = data.get("started")
    if isinstance(started, int) and started > 0:
        return started
    return None


async def get_straggler_count(session: AsyncSession, threshold_sec: int) -> int:
    """Return the count of active ``process_file`` jobs running longer than ``threshold_sec``, degrade-safe.

    A straggler is an analyze job that is STILL RUNNING (``status='active'``) but whose
    running-age exceeds ``threshold_sec`` -- "still grinding", the complement of the
    ANALYSIS_FAILED bucket (gave up). Age is computed in PYTHON from the deserialized job blob's
    ``started`` (epoch ms), because ``saq_jobs`` has no queryable ``started`` column
    (PATTERNS.md banner / D-01). Only the bounded active ``process_file`` set is deserialized
    (T-44-06), not the full backlog.

    ``threshold_sec`` is the caller's ``settings.straggler_threshold_sec`` -- a plain Python int
    compared post-deserialize, NEVER interpolated into SQL (T-44-05). A job with a missing / None /
    non-positive ``started`` (dequeued-but-not-yet-stamped) is treated as not-yet-old and is NOT
    counted.

    Failure isolation (T-44-04): the ``saq_jobs`` read runs inside a SAVEPOINT
    (``session.begin_nested()``). On ANY DB error (a missing ``saq_jobs`` table in a pre-migration
    env, a DB hiccup) the nested scope is rolled back ALONE -- recovering the aborted Postgres
    transaction WITHOUT expiring the dashboard's already-loaded ORM objects (a plain
    ``session.rollback()`` would 500 the page on the next lazy load) and WITHOUT poisoning later
    queries. The function logs ``straggler_degraded`` and returns 0 -- it NEVER raises into the
    hot 5s /pipeline/stats poll.
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(_STRAGGLER_ACTIVE_SQL)).all()
    except Exception:
        logger.warning("straggler_degraded", exc_info=True)
        return 0
    now_ms = saq_now()
    count = 0
    for row in rows:
        started_ms = _job_started_ms(row[0])
        if started_ms is not None and (now_ms - started_ms) / 1000 > threshold_sec:
            count += 1
    return count


# --------------------------------------------------------------------------------------------------
# Phase 87 (87-04, UI-01 / D-02 / PERF-01): the scannable, per-row-derived files page.
#
# The operator's "where's this file at?" overview. Two anti-features are forbidden by the phase's
# anti-feature table and BOTH are honoured here: (1) "rendering raw internal status strings" -- every
# per-stage cell is the DERIVED stage_status_case bucket, never FileRecord.state; (2) "a stats poll
# that scans the whole corpus" -- the query is LIMIT-bounded, keyset/offset-paginated, and NEVER emits
# an unbounded whole-corpus COUNT (the +1 sentinel below computes has_next instead). The six correlated
# stage_status_case CASE columns evaluate for the N page rows ONLY (they correlate to FileRecord), so
# the per-page derivation cost is O(page_size), never O(corpus) -- the T-87-11 DoS mitigation.
# --------------------------------------------------------------------------------------------------

# The six pills the UI shows, in matrix order. The 7-stage -> 6-pill remap LANDMINE lives HERE and in
# _stage_matrix.html: tracklist is omitted; Appr = REVIEW, Exec = APPLY. `.value` keys the row dict so
# the template reads buckets.review for the Appr pill and buckets.apply for the Exec pill.
_FILES_PAGE_STAGES: tuple[Stage, ...] = (
    Stage.METADATA,
    Stage.FINGERPRINT,
    Stage.ANALYZE,
    Stage.PROPOSE,
    Stage.REVIEW,
    Stage.APPLY,
)


@dataclass
class FilesPageRow:
    """One rendered file row: the ORM record + its six DERIVED per-stage buckets (keyed by Stage value)."""

    file: FileRecord
    buckets: dict[str, str]


@dataclass
class FilesPage:
    """A bounded, derive-per-row page of files. ``has_next`` comes from a +1 sentinel row -- never a COUNT."""

    rows: list[FilesPageRow] = field(default_factory=list)
    page: int = 1
    page_size: int = 25
    has_next: bool = False


def _files_page_stmt(*, page: int, page_size: int, stage: Stage | None, bucket: str | None) -> Select[Any]:
    """Build the bounded per-page derivation SELECT (extracted so the EXPLAIN test can probe it directly).

    ``select(FileRecord, stage_status_case(METADATA), ... , stage_status_case(APPLY))`` ordered by the
    ``FileRecord.id`` PK index and LIMITed to ``page_size + 1`` (the sentinel that yields ``has_next``
    with NO COUNT). Each ``stage_status_case`` is a correlated CASE over the Phase-77 partial indexes
    (``ix_metadata_failed`` / ``ix_analysis_completed`` / ``ix_analysis_failed`` / ``ix_fprint_success``),
    so the derivation touches only the page rows. The optional ``stage``+``bucket`` filter is applied as
    ``stage_status_case(stage) == bucket`` -- a pure ORM bound-param comparison (never f-string SQL,
    T-87-14); the caller validates ``stage``/``bucket`` against the ``Stage``/``Status`` allowlists.
    """
    offset = (page - 1) * page_size
    cols = [stage_status_case(s) for s in _FILES_PAGE_STAGES]
    stmt = select(FileRecord, *cols).order_by(FileRecord.id)
    if stage is not None and bucket is not None:
        stmt = stmt.where(stage_status_case(stage) == bucket)
    # +1 sentinel -> has_next WITHOUT a whole-corpus COUNT (the T-87-11 DoS mitigation).
    return stmt.offset(offset).limit(page_size + 1)


async def get_files_page(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 25,
    stage: Stage | None = None,
    bucket: str | None = None,
) -> FilesPage:
    """Return one bounded, per-row-derived page of files -- SAVEPOINT degrade-safe, never a whole-corpus scan.

    Clamps ``page`` (>=1) and ``page_size`` (10..100), builds the bounded :func:`_files_page_stmt`, and
    runs it inside a ``begin_nested()`` SAVEPOINT so ANY error (a DB hiccup, an aborted transaction, a
    build-time raise) rolls back the nested scope ALONE, logs a warning, and returns a safe EMPTY page --
    it NEVER 500s the poll (INFLIGHT-02 / D-00c / T-87-12). ``has_next`` is derived from the LIMIT+1
    sentinel row, so pagination costs no COUNT. The six correlated ``stage_status_case`` columns are read
    back into each row's ``buckets`` dict keyed by ``Stage`` value (metadata/fingerprint/analyze/propose/
    review/apply) -- the derived buckets the ``_stage_pill`` cells render (never ``FileRecord.state``).

    ``stage``+``bucket`` are accepted NOW (plumbed straight through to the filter) so Plan 05 -- which
    wires the status filter bar -- is templates-only. Passing only one of the pair is a no-op filter.
    """
    page = max(page, 1)
    page_size = min(max(page_size, 10), 100)
    try:
        async with session.begin_nested():
            stmt = _files_page_stmt(page=page, page_size=page_size, stage=stage, bucket=bucket)
            result = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("files_page_degraded", page=page, page_size=page_size, exc_info=True)
        return FilesPage(rows=[], page=page, page_size=page_size, has_next=False)
    has_next = len(result) > page_size
    rows = [
        FilesPageRow(
            file=row[0],
            buckets={stage_member.value: row[idx + 1] for idx, stage_member in enumerate(_FILES_PAGE_STAGES)},
        )
        for row in result[:page_size]
    ]
    return FilesPage(rows=rows, page=page, page_size=page_size, has_next=has_next)
