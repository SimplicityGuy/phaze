"""Pipeline orchestration service -- stage counts and file queries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any, cast as type_cast

from saq.utils import now as saq_now
from sqlalchemy import String, and_, cast, distinct, exists, func, or_, select, text
from sqlalchemy.orm import aliased
import structlog

from phaze.config import get_settings
from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.enums.stage import Stage, Status
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.pipeline_stage_control import PipelineStageControl
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.agent_liveness import non_local_backend_kinds
from phaze.services.enqueue_router import LANES
from phaze.services.pagination import DEFAULT_PAGE_SIZE, Page, clamp_page, clamp_page_size, paged_stmt, split_sentinel
from phaze.services.stage_status import (
    awaiting_candidate_clause,
    dedup_resolved_clause,
    done_clause,
    eligible_clause,
    failed_clause,
    inflight_clause,
    stage_status_case,
)
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

    from phaze.config import ControlSettings
    from phaze.routers.column_sort import SortState


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


# NOTE (Phase 82, D-05/READ-02): ``get_pipeline_stats`` -- the linear per-``FileRecord.state`` grouped
# counter -- was REMOVED here. The stats path no longer groups by (or reads) ``FileRecord.state``: its
# three former callers
# (``routers/pipeline.py`` ``_build_dag_context`` / ``build_dashboard_context`` /
# ``pipeline_stats_partial``) now derive the seven consumed keys from :func:`get_stage_progress`'s
# output-table counts (``discovered→discovery.done``, ``metadata_extracted→metadata.done``,
# ``fingerprinted→fingerprint.done``, ``analyzed→analyze.done``, ``proposal_generated→proposals.done``,
# ``approved→execute.total``, ``executed→execute.done``). Phase 90 (MIG-04) then removed the
# ``FileState`` enum + ``files.state`` column entirely, so the former linear ``PIPELINE_STAGES`` list
# (which enumerated the enum members) is gone -- stage membership derives from the output tables.


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

    Failure isolation is split per-source AND per-agent, and the function never raises: a
    Redis hiccup or a missing ``app.state`` attribute (the test ``client`` skips the
    lifespan, so the queue handles are absent) must degrade only the affected reader to 0,
    never 500 the 5s dashboard poll. The agent and controller reads use independent ``try``
    blocks so one dead source does not zero the other; within the agent source each agent is
    read in its own ``try`` (and each lane queue ``connect()``-ed first, idempotently, so an
    agent registered after startup does not raise ``PoolClosed`` on an unopened pool) so one
    dead agent queue does not zero the rest.

    Returns a dict with keys ``agent_queued``, ``agent_active``, ``controller_queued``,
    ``controller_active``, ``agent_busy`` (= queued + active), ``controller_busy``.
    """
    agent_queued = agent_active = controller_queued = controller_active = 0

    try:
        agents_stmt = select(Agent).where(Agent.revoked_at.is_(None))
        agents = (await session.execute(agents_stmt)).scalars().all()
    except Exception:
        # The agents query itself failing (missing app.state/session in the test lifespan-skip,
        # or a DB hiccup) degrades the whole agent source to 0 -- never 500 the 5s dashboard poll.
        agents = []
        logger.warning("queue_activity_degraded", source="agent", exc_info=True)

    for agent in agents:
        # Per-agent isolation + connect-before-count (#217). main.py's lifespan opens the
        # PostgresQueue psycopg pool only for agents present at boot; an agent registered at
        # runtime (``phaze agents add`` -- e.g. a compute burst agent) otherwise raises
        # PoolClosed on count() until the api restarts, and a single such raise used to zero
        # EVERY agent's live depth. connect() is idempotent (SAQ guards on ``self._connected``),
        # mirroring the producer path (enqueue_for_agent). Wrapping each agent independently
        # means one dead or unconnectable queue degrades only itself, not the rest.
        #
        # quick-260707-dh1: sum queued+active across ALL FOUR lane queues (the authoritative
        # all-lane agent depth -- the heartbeat's queue_depth is analyze-lane-only by design)
        # PLUS the legacy base queue so the migration drain window stays visible.
        try:
            a_queued = a_active = 0
            for q in (*app_state.task_router.all_lane_queues(agent.id), app_state.task_router.legacy_base_queue(agent.id)):
                await q.connect()
                a_queued += await q.count("queued")
                a_active += await q.count("active")
            agent_queued += a_queued
            agent_active += a_active
        except Exception:
            logger.warning("queue_activity_degraded", source="agent", agent_id=agent.id, exc_info=True)

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

    ``created_at`` carries no uniqueness constraint, so two scan batches for the same agent can share a
    value; with a partial ORDER BY, rows tied at the ``LIMIT`` boundary would come back in ANY order
    (heap order, which shifts with page layout, vacuum, and plan choice), letting a batch flap in/out
    between polls. Appending the unique ``ScanBatch.id`` makes the order TOTAL, so the LIMIT boundary is
    deterministic. Same rationale as the paging contract's mandatory unique tiebreaker (rule 4, see
    :mod:`phaze.services.pagination`).
    """
    try:
        async with session.begin_nested():
            stmt = select(ScanBatch).where(ScanBatch.agent_id == agent_id).order_by(ScanBatch.created_at.desc(), ScanBatch.id.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
        return list(rows)
    except Exception:
        logger.warning("agent_recent_scans_degraded", agent_id=agent_id, exc_info=True)
        return []


# Bounded fan-out for the get_stage_progress reads (CLEAN-01 / D-01/D-02/D-03). A single
# AsyncSession (one asyncpg connection) CANNOT run concurrent statements -- SQLAlchemy 2.0 raises
# IllegalStateChangeError ("another operation is in progress") -- so each concurrent read runs in
# its OWN session. The semaphore caps the extra concurrent pool checkouts per 5s poll: cap 4 admits
# all three heavy enrich-bucket reads at once (the serial-cost dominators) while leaving >=6 of the
# deliberately-lean 10-conn/worker pool (pool_size=5 + max_overflow=5, post-PgBouncer-exhaustion
# incident) for the request's own session + other request traffic + the orphan refresher (RESEARCH
# Pool Headroom, T-92-02-DoS).
#
# WHY NOT a module-level pre-constructed ``asyncio.Semaphore(4)``: an asyncio primitive binds to the
# event loop of its FIRST use, so a module-singleton raises "bound to a different event loop" under
# pytest's per-test loops and degrades every read (a real bug, not just a test artifact). Instead
# :func:`_stats_fanout` builds a FRESH ``asyncio.Semaphore(4)`` per poll, bound to the running loop
# (RESEARCH's cap is explicitly PER-POLL) -- unless the ``_STATS_FANOUT`` override below is set.
#
# PATCHABLE SEAM -- ``_STATS_FANOUT`` is the override 92-03 Task 2 sets (per-test, in the test loop)
# to ``asyncio.Semaphore(1)`` so the fan-out SERIALIZES onto the single shared per-test connection
# (concurrent reads on one connection would raise IllegalStateChangeError); it also monkeypatches
# ``phaze.database.async_session`` to route the fan-out through that connection. Both are resolved at
# CALL time (the deferred import + this module attribute) so the routing takes effect.
_STATS_FANOUT: asyncio.Semaphore | None = None


def _stats_fanout() -> asyncio.Semaphore:
    """Return the fan-out bound to the CURRENT loop: the ``_STATS_FANOUT`` test override, else a fresh cap-4."""
    return _STATS_FANOUT if _STATS_FANOUT is not None else asyncio.Semaphore(4)


async def _read_in_own_session[T](fanout: asyncio.Semaphore, fn: Callable[[AsyncSession], Awaitable[T]], default: T) -> T:
    """Run one degrade-safe read in its OWN :class:`AsyncSession`, bounded by the shared ``fanout``.

    ``fanout`` is the ONE semaphore from :func:`_stats_fanout` shared across all the poll's reads (so
    the cap bounds them collectively). Resolves ``async_session`` at CALL time via a DEFERRED
    ``from phaze.database import async_session`` (re-read every call, matching the agent-worker
    import-boundary convention used by :func:`refresh_stage_orphan_counts`) so the single patchable
    seam is the SOURCE module attribute ``phaze.database.async_session`` -- 92-03 Task 2 monkeypatches
    it onto the per-test connection so seed-then-read tests see their rows.

    Degrade discipline end-to-end (RESEARCH Pitfall 2 / T-92-02-DoS): the passed ``fn`` already wraps
    :func:`_safe_count` / :func:`_safe_bucket_counts` (which never raise), but the session ACQUISITION
    itself (``async with async_session()``) can raise ``TimeoutError`` after ``pool_timeout=10s`` on a
    saturated pool -- a raise that happens OUTSIDE ``fn``'s try/except and, under a default
    ``asyncio.gather``, would cancel/propagate and 500 the hot 5s poll. Catching it HERE returns the
    node's ``default`` (0 for a count, the all-zero bucket dict for an enrich node) so a pool timeout
    degrades that single node rather than aborting the whole fan-out.
    """
    from phaze.database import async_session  # noqa: PLC0415 -- deferred: keeps the agent-worker import boundary intact

    try:
        async with fanout, async_session() as s:
            return await fn(s)
    except Exception:
        logger.warning("stage_progress_acquire_degraded", exc_info=True)
        return default


async def get_stage_progress(session: AsyncSession) -> dict[str, dict[str, int | None]]:  # noqa: ARG001
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

    CLEAN-01 (D-01/D-02/D-03): every independent read now runs CONCURRENTLY via
    :func:`asyncio.gather`, each in its OWN :class:`AsyncSession` from :func:`_read_in_own_session`
    (bounded by :data:`_STATS_FANOUT`), collapsing the ~13 serial awaits into roughly the slowest
    single read. The incoming ``session`` parameter is KEPT for signature stability (callers still
    pass their request session) but is UNUSED-BY-DESIGN -- the reads run in their own sessions
    (Open Question 2). Because each read has its own transaction/snapshot, the returned dict is
    byte-identical on a QUIESCENT DB; under concurrent writes two nodes may reflect MVCC snapshots
    microseconds apart -- acceptable for a 5s poll (RESEARCH Pitfall 1), NOT strict identity under
    live writes.
    """
    # Pre-build the count statements so each gather task closes over a distinct, already-constructed
    # Select. Statement construction is pure (no I/O) -- only the execute() inside each own session
    # touches the pool.
    mv_total_stmt = select(func.count(FileRecord.id)).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))
    tracklist_total_stmt = select(func.count(Tracklist.id))
    discovery_stmt = select(func.count(FileRecord.id))
    # Proposals denominator: the convergence-gate set -- files with BOTH metadata AND analysis
    # (mirrors routers/pipeline.py:116-128, the generate_proposals ready-set).
    convergence_stmt = (
        select(func.count(FileRecord.id))
        .where(exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id)))
        .where(exists(select(AnalysisResult.id).where(AnalysisResult.file_id == FileRecord.id)))
    )
    scan_search_stmt = select(func.count(distinct(Tracklist.file_id)))
    scrape_stmt = select(func.count(distinct(TracklistVersion.tracklist_id)))
    proposals_stmt = select(func.count(distinct(RenameProposal.file_id)))
    execute_total_stmt = select(func.count(distinct(RenameProposal.file_id))).where(RenameProposal.status == ProposalStatus.APPROVED)

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

    # The all-zero enrich-bucket default returned when a bucket read's session acquisition times out
    # (never mutated -- only spread via {**bucket, "total": ...}).
    bucket_default: dict[str, int] = {s.value: 0 for s in Status}

    # ONE semaphore shared across every read in THIS poll so the cap bounds them collectively (fresh
    # per poll, bound to the running loop -- see _stats_fanout).
    fanout = _stats_fanout()

    # Fan out every independent read concurrently, each in its own session (D-01/D-02/D-03). The
    # _safe_count / _safe_bucket_counts wrappers stay VERBATIM (D-04) -- reused as the per-read body
    # -- and _read_in_own_session adds the acquisition-degrade belt (Pitfall 2). Assemble the SAME
    # 9-key dict in the SAME key order from the gathered values (byte-identical on a quiescent DB).
    (
        music_video_total,
        tracklist_total,
        discovery_done,
        convergence_total,
        metadata_b,
        fingerprint_b,
        analyze_b,
        scan_search_done,
        scrape_done,
        match_done,
        proposals_done,
        execute_done,
        execute_total,
        # asyncio.gather with >6 awaitables of mixed return types collapses to list[object] under mypy,
        # so pin the exact per-node tuple shape with a single cast (int counts + 3 enrich-bucket dicts).
        # NOTE: typing.cast is aliased type_cast -- the bare `cast` name is sqlalchemy's SQL cast (used
        # elsewhere in this module).
    ) = type_cast(
        "tuple[int, int, int, int, dict[str, int], dict[str, int], dict[str, int], int, int, int, int, int, int]",
        await asyncio.gather(
            _read_in_own_session(fanout, lambda s: _safe_count(s, mv_total_stmt, node="music_video_total"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, tracklist_total_stmt, node="tracklist_total"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, discovery_stmt, node="discovery"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, convergence_stmt, node="proposals_total"), 0),
            _read_in_own_session(fanout, lambda s: _safe_bucket_counts(s, Stage.METADATA), bucket_default),
            _read_in_own_session(fanout, lambda s: _safe_bucket_counts(s, Stage.FINGERPRINT), bucket_default),
            _read_in_own_session(fanout, lambda s: _safe_bucket_counts(s, Stage.ANALYZE), bucket_default),
            _read_in_own_session(fanout, lambda s: _safe_count(s, scan_search_stmt, node="scan_search"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, scrape_stmt, node="scrape"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, match_done_stmt, node="match"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, proposals_stmt, node="proposals"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, execute_done_stmt, node="execute"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, execute_total_stmt, node="execute_total"), 0),
        ),
    )

    return {
        "discovery": {"done": discovery_done, "total": discovery_done},
        # Phase 82 (READ-02, D-04/D-05) + Phase 87 (skipped): the three enrich nodes are FIVE-BUCKET
        # ({not_started, in_flight, done, skipped, failed} + total) via one GROUP BY stage_status_case(stage)
        # each -- so the DAG surfaces a VISIBLE failed count per enrich stage and the five buckets sum
        # to music_video_total on a healthy query. `total` stays music_video_total; `done` (still read
        # by _build_dag_context) is now the derived done-bucket. Degrade-safe (all-zero on any error).
        "metadata": {**metadata_b, "total": music_video_total},
        "fingerprint": {**fingerprint_b, "total": music_video_total},
        "analyze": {**analyze_b, "total": music_video_total},
        "scan_search": {
            "done": scan_search_done,
            "total": None,  # counter-only: no table defines "should get a tracklist" (RESEARCH Q5 / UI-SPEC)
        },
        "scrape": {
            "done": scrape_done,
            "total": tracklist_total,
        },
        "match": {
            "done": match_done,
            "total": tracklist_total,
        },
        "proposals": {
            "done": proposals_done,
            "total": convergence_total,
        },
        "execute": {
            "done": execute_done,
            "total": execute_total,
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

    This is the DEGRADE-SAFE public wrapper (HYG-01 / D-05): it is retained UNCHANGED as the parity
    anchor + tested public surface -- delegating to the RAISING :func:`_compute_stage_orphan_counts`
    core and swallowing any error into the all-zero default. The parity guard
    (``test_orphan_count.py::test_orphan_count_matches_recovery_candidate_set``) targets this contract.
    """
    try:
        return await _compute_stage_orphan_counts(session)
    except Exception:
        logger.warning("stage_orphan_counts_degraded", exc_info=True)
        return {"metadata": 0, "analyze": 0, "fingerprint": 0}


async def _compute_stage_orphan_counts(session: AsyncSession) -> dict[str, int]:
    """Raising core of :func:`get_stage_orphan_counts` (HYG-01, D-03/D-05).

    Returns the same ``{metadata, analyze, fingerprint}`` dict the wrapper returns on success, but
    RAISES on ANY DB error (it does NOT swallow) -- so the off-request refresher
    (:func:`refresh_stage_orphan_counts`) can distinguish a real success from a degrade and thereby
    keep the last-good cache value on failure instead of poisoning it with all-zeros (D-03).

    The classification predicate is REUSED verbatim from recovery
    (:func:`phaze.tasks.reenqueue.is_domain_completed` + ``_build_done_sets`` + ``_in_flight_cloud_job_ids``);
    parity with ``recover_orphaned_work`` is DEFINITIONAL and mutation-tested (D-05). The ``reenqueue``
    / ``scheduling_ledger`` imports stay FUNCTION-LOCAL to break the reenqueue<->pipeline cycle and
    preserve the control-only agent-worker boundary (``tests/shared/core/test_task_split.py``); do NOT hoist.
    """
    out: dict[str, int] = {"metadata": 0, "analyze": 0, "fingerprint": 0}
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
    return out


# HYG-01 / WR-02 orphan-count cache (Phase 91). The amber /pipeline/stats badge polls every 5s; the
# full derivation above materializes the whole ``scheduling_ledger`` (~44.5K rows in the 2026-06-18
# incident) + the per-stage done-sets, which must NEVER run inline on that hot request path (D-01/D-02).
# A process/module-scope cache (NOT request-scoped -- D-04) is refreshed off-request by the FastAPI
# lifespan's ``_orphan_refresh_loop`` on a short TTL; the request-scoped /pipeline/stats read is O(1).
# NO ``asyncio.Lock`` is needed: a single event loop runs the refresher and the readers, and a whole-
# dict rebind (``_orphan_cache = ...``) between awaits is atomic -- readers see either the old dict or
# the new one, never a torn partial (per RESEARCH "Don't Hand-Roll" -- no manual locking).
_ORPHAN_TTL_SECONDS: float = 4.0  # D-01 discretion: < the 5s poll so the cache is at most one tick stale
_orphan_cache: dict[str, int] = {"metadata": 0, "analyze": 0, "fingerprint": 0}  # seeded safe until first success
_orphan_cache_expires_at: float = 0.0


def get_cached_stage_orphan_counts() -> dict[str, int]:
    """Return an O(1) COPY of the module-scope orphan-count cache (D-04). No session, no DB.

    Returns a distinct ``dict`` so a caller mutating the return can never corrupt the module cache.
    This is the hot-path reader the /pipeline/stats poll uses instead of the full derivation.
    """
    return dict(_orphan_cache)


async def refresh_stage_orphan_counts() -> dict[str, int]:
    """Recompute the orphan counts off-request and rebind the module cache on SUCCESS ONLY (D-03).

    Opens its OWN ``async_session`` (independent of any request session), runs the RAISING
    :func:`_compute_stage_orphan_counts`, and rebinds ``_orphan_cache`` (+ its TTL stamp) only when
    the compute succeeds. On ANY exception it propagates the error -- the background
    ``_orphan_refresh_loop`` swallows + logs it -- leaving the prior known-good value intact so a
    transient DB hiccup never poisons the badge to all-zeros (D-03).
    """
    global _orphan_cache, _orphan_cache_expires_at
    from phaze.database import async_session  # noqa: PLC0415 -- deferred: keeps the agent-worker import boundary intact

    async with async_session() as session:
        computed = await _compute_stage_orphan_counts(session)
    # Whole-dict rebind is atomic between awaits (no Lock needed -- see module comment above).
    _orphan_cache = computed
    _orphan_cache_expires_at = time.monotonic() + _ORPHAN_TTL_SECONDS
    return computed


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


# --- Phase 58 (58-04, WORK-04 / D-03) all-in-stage Analyze file table read ----------------
#
# The rows surfaced in the D-03 "one table of ALL in-stage Analyze files" table are now DERIVED
# (Phase 90 PR-A, no ``files.state`` read): any analysis row (``AnalysisResult.id IS NOT NULL`` --
# which by the builders' definitions covers the done + failed + partial-57.1 buckets), the analyze
# in-flight ledger (``inflight_clause(ANALYZE)``), and the active ``cloud_job`` lanes
# (awaiting / pushing / pushed, D-12), so a running or cloud-held file appears even before it has an
# analysis row.


# Phase 95 (phaze-zqvh.2, CONSOLE-04): the per-file table is BOUNDED at the source. The
# Phase-58 ``get_analyze_stage_files`` returned the ENTIRE analyze-stage membership -- which as the
# archive converges monotonically approaches the whole corpus (92,335 rows / ~105MB HTML at the seeded
# 200K scale, phaze-zqvh.1 baseline). It is SPLIT here into two bounded reads that share ONE row
# projection (identical per-row dict shape, so ``analyze_workspace.html`` row-building is unchanged):
#
#   * :func:`get_analyze_working_set` -- the DEFAULT view: the active-first working set (in-flight,
#     awaiting-cloud, failed -- everything that is NOT a finished completion, naturally bounded by lane
#     concurrency / the failure backlog) PLUS a LIMIT-ed recent-completions window. The dominant,
#     monotonically-growing completed set is windowed, not rendered whole.
#   * :func:`get_analyze_files_page` -- the full corpus, reachable via the status-filter bar, served as
#     bounded OFFSET pages with a ``page_size + 1`` sentinel for ``has_next`` (never a whole-corpus
#     COUNT -- the same T-87-11 DoS mitigation ``get_files_page`` uses).
#
# The membership semantics are UNCHANGED from Phase 90 (PR-A): DERIVED, never ``files.state``. A file is
# in the Analyze stage iff it carries ANY analysis row (``AnalysisResult.id IS NOT NULL`` -- SUPERSETS
# done_clause + failed_clause + any partial 57.1 row) OR its analyze is in-flight (``inflight_clause``
# over ``scheduling_ledger``) OR it carries an ACTIVE ``cloud_job`` sidecar. The correlated builders are
# NOT composed against the OUTER-JOINED columns (SQLAlchemy would auto-correlate them out of the inner
# ``exists(...)`` -- the Phase 90 blocking-fix); membership is spelled against the joined columns using
# the builders' EXACT semantics while ``inflight_clause`` (over the un-joined ledger) is composed verbatim.

# Bounded recent-completions window on the DEFAULT view (phaze-zqvh.2). Small enough that the operator
# sees "what just finished" without the whole (corpus-scale) completed set landing in the DOM.
_ANALYZE_COMPLETIONS_WINDOW = 50

# The ACTIVE cloud statuses that place a file in the Analyze working set -- the SAME five the Phase-58
# membership listed (awaiting/uploading/submitted/uploaded/running; NOT the terminal ``succeeded``,
# which the completed-window / paged listing covers instead).
_ANALYZE_ACTIVE_CLOUD_STATUSES: tuple[str, ...] = (
    CloudJobStatus.AWAITING.value,
    CloudJobStatus.UPLOADING.value,
    CloudJobStatus.SUBMITTED.value,
    CloudJobStatus.UPLOADED.value,
    CloudJobStatus.RUNNING.value,
)

# The status-filter allowlist for the paged full listing. Validated as a SET (T-87-14 / T-57-01: a
# filter value is NEVER spliced into SQL or a template path -- an unknown value degrades to the
# unfiltered "all" membership, never a 422 into the render). ``None`` (no filter) => the DEFAULT
# working-set view; ``"all"`` => the full analyze-stage membership, paged.
ANALYZE_FILTER_ALL = "all"
ANALYZE_FILTER_IN_FLIGHT = "in_flight"
ANALYZE_FILTER_AWAITING = "awaiting_cloud"
ANALYZE_FILTER_FAILED = "failed"
ANALYZE_FILTER_COMPLETED = "completed"
ANALYZE_FILTERS: frozenset[str] = frozenset(
    {
        ANALYZE_FILTER_ALL,
        ANALYZE_FILTER_IN_FLIGHT,
        ANALYZE_FILTER_AWAITING,
        ANALYZE_FILTER_FAILED,
        ANALYZE_FILTER_COMPLETED,
    }
)


@dataclass
class AnalyzeFilesPage:
    """A bounded, projected page of analyze-stage files. ``has_next`` rides a +1 sentinel -- never a COUNT."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    page: int = 1
    page_size: int = 50
    has_next: bool = False
    status: str | None = None


def _analyze_files_select() -> Select[Any]:
    """The shared analyze-file SELECT: the 11 display columns + the three degrade-safe LEFT joins.

    Extracted so the working-set, completions-window, and paged reads all project the IDENTICAL row
    shape (:func:`_project_analyze_rows`), keeping ``analyze_workspace.html`` row-building unchanged.
    LEFT JOINs the per-file ``cloud_job`` sidecar (lane derivation), the 1:1 ``analysis`` aggregate
    (windowed coverage / the 57.1 mid-flight signal + the done/failed markers), and ``metadata``
    (duration). No WHERE / ORDER here -- each caller composes its own bounded predicate + order.
    """
    return (
        select(
            FileRecord.id,
            FileRecord.original_filename,
            FileRecord.original_path,
            CloudJob.id,
            CloudJob.status,
            CloudJob.backend_id,
            AnalysisResult.fine_windows_analyzed,
            AnalysisResult.fine_windows_total,
            AnalysisResult.analysis_completed_at,
            AnalysisResult.failed_at,
            FileMetadata.duration,
        )
        .select_from(FileRecord)
        .outerjoin(CloudJob, CloudJob.file_id == FileRecord.id)
        .outerjoin(AnalysisResult, AnalysisResult.file_id == FileRecord.id)
        .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
    )


def _analyze_active_where() -> Any:
    """The DEFAULT working-set predicate: analyze-stage membership MINUS finished completions.

    In-flight (a partial analysis row -- ``analysis`` row present with NO ``analysis_completed_at``,
    which also covers a ``failed_at`` row -- OR the ledger ``inflight_clause``) plus awaiting-cloud
    (an active ``cloud_job``). A completed file (``analysis_completed_at`` set) is EXCLUDED here and
    surfaced via the bounded completions window instead -- so this set never grows with the corpus.
    """
    return or_(
        and_(AnalysisResult.id.is_not(None), AnalysisResult.analysis_completed_at.is_(None)),
        inflight_clause(Stage.ANALYZE),
        CloudJob.status.in_(_ANALYZE_ACTIVE_CLOUD_STATUSES),
    )


def _analyze_status_where(status: str | None) -> Any:
    """Map a validated status filter to its WHERE predicate (the paged full-listing lens).

    ``None`` / unknown -> the full analyze-stage membership ("all", unfiltered). Each branch is a pure
    ORM bound-param comparison over the already-joined columns (never f-string SQL, never a request
    value in a path -- T-87-14 / T-57-01); the router validates ``status`` against :data:`ANALYZE_FILTERS`.
    """
    if status == ANALYZE_FILTER_IN_FLIGHT:
        return or_(
            and_(
                AnalysisResult.id.is_not(None),
                AnalysisResult.analysis_completed_at.is_(None),
                AnalysisResult.failed_at.is_(None),
            ),
            inflight_clause(Stage.ANALYZE),
        )
    if status == ANALYZE_FILTER_AWAITING:
        return CloudJob.status.in_(_ANALYZE_ACTIVE_CLOUD_STATUSES)
    if status == ANALYZE_FILTER_FAILED:
        return AnalysisResult.failed_at.is_not(None)
    if status == ANALYZE_FILTER_COMPLETED:
        return AnalysisResult.analysis_completed_at.is_not(None)
    # ANALYZE_FILTER_ALL / None / unknown -> the full analyze-stage membership (the Phase-90 predicate).
    return or_(
        AnalysisResult.id.is_not(None),
        inflight_clause(Stage.ANALYZE),
        CloudJob.status.in_(_ANALYZE_ACTIVE_CLOUD_STATUSES),
    )


def _project_analyze_rows(rows: Sequence[Any], kinds: dict[str, str]) -> list[dict[str, Any]]:
    """Project raw :func:`_analyze_files_select` rows into the per-file dict the template renders.

    The IDENTICAL shape the Phase-58 ``get_analyze_stage_files`` produced (so ``analyze_workspace.html``
    row-building is unchanged): the RECORD-01 ``file_id`` opener key, the DERIVED boolean flags
    (``awaiting_cloud`` / ``analysis_failed`` / ``completed`` -- never a raw ``files.state``), the
    COMPUTE-03 lane derivation off the stamped ``CloudJob.backend_id`` (no ``cloud_job`` -> local; a
    stamped ``backend_id`` -> the id + its registry ``lane_kind`` via ``non_local_backend_kinds``,
    falling back to ``"cloud"`` for a deregistered cluster; a NULL ``backend_id`` -> the truthful
    unattributed ``"cloud"`` fallback, NEVER the stale ``"a1"`` heuristic), and the 57.1 windowed
    coverage. ``kinds`` is the once-per-call registry projection (never a per-row lookup).
    """
    files: list[dict[str, Any]] = []
    for file_id, filename, path, cloud_job_id, cloud_status, backend_id, fine_done, fine_total, completed_at, failed_at, duration in rows:
        if cloud_job_id is None:
            lane, lane_kind = "local", "local"
        elif backend_id is not None:
            lane, lane_kind = backend_id, kinds.get(backend_id, "cloud")
        else:
            # Stamped cloud_job with no backend_id yet (not attributed to a registry cluster) --
            # the truthful "cloud, unattributed" fallback. NEVER the stale "a1" heuristic label.
            lane, lane_kind = "cloud", "cloud"
        files.append(
            {
                # Phase 61 (RECORD-01): the row->record slide-in opener keys on this file_id
                # (hx-get="/record/{file_id}"); str() so the template renders the UUID inline.
                "file_id": str(file_id),
                "filename": filename,
                "path": path,
                # Phase 90 (PR-A): derived boolean flags REPLACE the raw ``state`` key -- the template
                # renders off these, never a FileState string.
                "awaiting_cloud": cloud_status == CloudJobStatus.AWAITING.value,
                "analysis_failed": failed_at is not None,
                "lane": lane,
                "lane_kind": lane_kind,
                "fine_done": fine_done,
                "fine_total": fine_total,
                "duration": duration,
                # completed derives from the joined analysis_completed_at (done_clause(ANALYZE)), not state==ANALYZED.
                "completed": completed_at is not None,
            }
        )
    return files


async def get_analyze_working_set(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    completions_limit: int = _ANALYZE_COMPLETIONS_WINDOW,
) -> AnalyzeFilesPage:
    """Return ONE BOUNDED page of the default Analyze view: the active-first working set, then a completions window.

    phaze-5462 -- THIS READ USED TO BE UNBOUNDED, and its docstring said the opposite. The retired
    text claimed the working set was "Naturally bounded (lane concurrency + the failure backlog);
    NEVER the whole corpus". That was FALSE in production and is the entire bug: a file joins the
    working set merely by having a ``scheduling_ledger`` row OR a partial/failed ``analysis`` row, and
    ORPHANED work never leaves it on its own. With a large stuck backlog the branch rendered 10,132
    rows / 12.7 MB inline -- ~180x the sibling metadata/fingerprint tabs. The prior fix (phaze-zqvh)
    bounded only the completions window and trusted this assertion for the other half. An assumption
    is not a bound; the LIMIT below is.

    Both reads follow the paging contract in :mod:`phaze.services.pagination` -- OFFSET paging, the
    shared :data:`~phaze.services.pagination.DEFAULT_PAGE_SIZE`, a ``page_size + 1`` sentinel for
    ``has_next`` (NEVER a whole-corpus COUNT), and the MANDATORY unique ``FileRecord.id`` tiebreaker
    (``created_at`` alone ties -- Postgres timestamp defaults are transaction-time constant -- so
    without it OFFSET paging would silently skip and duplicate rows across pages).

      1. The active working set (:func:`_analyze_active_where`) -- in-flight / awaiting-cloud /
         failed, newest-first, PAGED.
      2. The recent-completions window, appended ONLY on the final page (``has_next`` False) so the
         "active work first, then what just finished" reading survives while every page stays
         bounded. For a working set that fits one page this is byte-identical to the prior behaviour.

    Degrade-safe under ONE SAVEPOINT: any error rolls back the nested scope alone, logs, and returns
    an EMPTY page -- this rides the hot workspace render and must NEVER 500 the page.
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    completions_limit = min(max(completions_limit, 0), 500)
    try:
        async with session.begin_nested():
            active_raw = (
                await session.execute(
                    paged_stmt(
                        _analyze_files_select().where(_analyze_active_where()),
                        page=page,
                        page_size=page_size,
                        order_by=(FileRecord.created_at.desc(),),
                        tiebreaker=(FileRecord.id.desc(),),
                    )
                )
            ).all()
            active_rows, has_next = split_sentinel(active_raw, page_size)
            # The completions window is a TAIL garnish, not part of the paged set -- read it only when
            # there is no further active page to show.
            window_rows = (
                (
                    await session.execute(
                        _analyze_files_select()
                        .where(AnalysisResult.analysis_completed_at.is_not(None))
                        .order_by(AnalysisResult.analysis_completed_at.desc(), FileRecord.id.desc())
                        .limit(completions_limit)
                    )
                ).all()
                if not has_next
                else []
            )
    except Exception:
        logger.warning("analyze_working_set_degraded", page=page, page_size=page_size, exc_info=True)
        return AnalyzeFilesPage(rows=[], page=page, page_size=page_size, has_next=False, status=None)

    # COMPUTE-03: the registry projection is looked up ONCE per call (not per row).
    kinds = non_local_backend_kinds(type_cast("ControlSettings", get_settings()))
    active = _project_analyze_rows(active_rows, kinds)
    seen = {row["file_id"] for row in active}
    window = [row for row in _project_analyze_rows(window_rows, kinds) if row["file_id"] not in seen]
    return AnalyzeFilesPage(rows=active + window, page=page, page_size=page_size, has_next=has_next, status=None)


async def get_analyze_files_page(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    status: str | None = None,
) -> AnalyzeFilesPage:
    """Return ONE bounded page of the full analyze-stage listing under a validated status filter.

    Follows the paging contract in :mod:`phaze.services.pagination`: OFFSET paging, the shared
    clamps, a ``page_size + 1`` sentinel for ``has_next`` (NEVER a whole-corpus COUNT -- T-87-11), and
    the MANDATORY unique ``FileRecord.id`` tiebreaker after the non-unique ``created_at`` display
    order. ``status`` is validated against :data:`ANALYZE_FILTERS` (unknown -> the unfiltered "all"
    membership, never a 422 into the render). SAVEPOINT degrade-safe: ANY error rolls back the nested
    scope alone, logs a warning, and returns a safe EMPTY page. Rows are the SAME projected shape as
    :func:`get_analyze_working_set`, so the template renders both identically.
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    status = status if status in ANALYZE_FILTERS else None
    try:
        async with session.begin_nested():
            stmt = paged_stmt(
                _analyze_files_select().where(_analyze_status_where(status)),
                page=page,
                page_size=page_size,
                order_by=(FileRecord.created_at.desc(),),
                tiebreaker=(FileRecord.id.desc(),),
            )
            raw = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("analyze_files_page_degraded", page=page, page_size=page_size, exc_info=True)
        return AnalyzeFilesPage(rows=[], page=page, page_size=page_size, has_next=False, status=status)
    rows, has_next = split_sentinel(raw, page_size)
    kinds = non_local_backend_kinds(type_cast("ControlSettings", get_settings()))
    return AnalyzeFilesPage(rows=_project_analyze_rows(rows, kinds), page=page, page_size=page_size, has_next=has_next, status=status)


def analyze_lanes_content_hash(lanes: list[dict[str, Any]], selected_lane: str | None) -> str:
    """Return a stable content hash of the #analyze-lanes grid's render inputs (phaze-zqvh.3).

    A deterministic digest over the lane snapshot + the selected-lane highlight -- the ONLY inputs that
    change what ``_analyze_lanes.html`` renders. Emitted as ``data-lanes-hash`` on the grid so a client
    ``htmx:oobBeforeSwap`` hook can SKIP the 5s OOB grid swap when the incoming state is byte-identical to
    what is already mounted -- bounding per-tick destroy-and-recreate churn (+ the Alpine re-init it
    triggers) on a long-lived, mostly-idle tab, WITHOUT a second poll loop or any change to the OOB
    store-seed fan-out (phaze-zqvh.3). Pure + degrade-safe: any serialization error collapses to ``""``
    (an empty hash never matches, so the swap always proceeds -- the fail-safe default is "always swap").
    """
    try:
        payload = json.dumps({"lanes": lanes, "selected": selected_lane}, sort_keys=True, default=str)
    except Exception:
        logger.warning("analyze_lanes_hash_degraded", exc_info=True)
        return ""
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# --- Phase 59 (59-01, IDENT-01/IDENT-02) Identify-workspace read-only row assembly ----------
#
# The two genuinely-new pieces of Phase 59 (RESEARCH "Don't Hand-Roll" key insight): per-row
# presentation data for the Track-ID combined table and the Tracklist per-set table. Both are
# PURE READS over existing, already-populated tables (no enqueue, no commit, no schema change) and
# both degrade to ``[]`` inside a SAVEPOINT on any error, mirroring :func:`get_analyze_working_set`
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


# phaze-1wvb: BOTH Identify reads below are BOUNDED at the source, on the paging contract
# (:mod:`phaze.services.pagination`). As authored in Phase 59 they were whole-corpus reads: the
# Track-ID read selected EVERY music/video file carrying any ``FingerprintResult`` OR a linked
# ``Tracklist``, and the Tracklist read one row per ``Tracklist`` -- neither with a LIMIT, both
# materialised with ``.all()`` and server-rendered inline into one HTML table by
# ``shell._render_stage``. That is the identical cliff phaze-5462 fixed on the Analyze tab (10,132
# rows / 12.7 MB, and 92,335 rows / ~105 MB HTML at the seeded 200K scale). As the archive converges
# -- most music/video files fingerprinted, many tracklists -- the Track-ID predicate approaches the
# WHOLE corpus, so "the signal-bearing subset" was never a bound. An assumption is not a bound.
#
# RULE 7 DETERMINATION (paging contract rule 7 -- do this BEFORE bounding anything): both readers are
# RENDER-ONLY. Verified by call graph -- their ONLY callers were ``shell._render_stage``
# (``trackid_files`` / ``tracklist_sets``), both flowing straight into ``_file_table.html``; neither
# feeds an enqueue, a trigger, or any bulk action. The Identify workspace's bulk actions read
# DIFFERENT, deliberately UNBOUNDED sets: SEARCH ALL -> :func:`get_untracked_files`, SCRAPE ALL ->
# :func:`get_scrape_pending_tracklists`, MATCH ALL -> :func:`get_match_pending_tracklists`. None of
# those three is touched here, so there is no shared reader to split and no way for this change to
# under-enqueue the backlog: bounding these two bounds ONLY pixels. Do NOT ever point a bulk trigger
# at a ``*_page`` reader.


def _trackid_linked_conf_subq() -> Any:
    """Per-file best LINKED tracklist confidence (the D-04 "matched" branch)."""
    return (
        select(
            Tracklist.file_id.label("file_id"),
            func.max(Tracklist.match_confidence).label("conf"),
        )
        .where(Tracklist.file_id.is_not(None))
        .group_by(Tracklist.file_id)
        .subquery()
    )


def _trackid_files_select(linked_conf_subq: Any) -> Select[Any]:
    """The Track-ID row SELECT: the display columns + the per-engine and linked-tracklist LEFT joins.

    No ORDER BY / LIMIT here -- :func:`_trackid_page_stmt` composes those through :func:`paged_stmt`
    so the bound and the mandatory unique tiebreaker live in exactly one place.
    """
    audfprint = aliased(FingerprintResult)
    panako = aliased(FingerprintResult)
    return (
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
    )


def _trackid_page_stmt(linked_conf_subq: Any, *, page: int, page_size: int, sort: SortState | None = None) -> Select[Any]:
    """Build the BOUNDED Track-ID page SELECT (phaze-1wvb).

    Extracted (like :func:`_pending_page_stmt`) so the bound is assertable at the SQL level: a test
    can compile this and check it carries a ``LIMIT``. That matters because :func:`split_sentinel`
    truncates in PYTHON -- a page whose row COUNT looks right can still be sitting on an unbounded
    whole-corpus DB read, which is the memory/DB half of the bug this bead fixes. Asserting only on
    ``len(page.rows)`` does NOT catch a missing LIMIT.

    Newest-first display order with the MANDATORY unique ``FileRecord.id`` tiebreaker (contract
    rule 4 -- ``created_at`` alone ties for every row written in one transaction).
    """
    return paged_stmt(
        _trackid_files_select(linked_conf_subq),
        page=page,
        page_size=page_size,
        # phaze-a6hm.1 sortable-column contract -- see _pending_page_stmt.
        order_by=sort.order_by() if sort is not None else (FileRecord.created_at.desc(),),
        tiebreaker=(FileRecord.id.desc(),),
    )


def _tracklist_sets_page_stmt(*, page: int, page_size: int, sort: SortState | None = None) -> Select[Any]:
    """Build the BOUNDED per-set Tracklist page SELECT (phaze-1wvb).

    Extracted for the same reason as :func:`_trackid_page_stmt`: the LIMIT must be assertable in the
    EMITTED SQL, not merely inferred from the length of the returned list. Newest-first with the
    MANDATORY unique ``Tracklist.id`` tiebreaker (contract rule 4).
    """
    track_counts_subq = (
        select(
            TracklistTrack.version_id.label("version_id"),
            func.count(TracklistTrack.id).label("total"),
            func.count(TracklistTrack.confidence).label("confident"),
        )
        .group_by(TracklistTrack.version_id)
        .subquery()
    )
    return paged_stmt(
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
        .outerjoin(track_counts_subq, track_counts_subq.c.version_id == Tracklist.latest_version_id),
        page=page,
        page_size=page_size,
        # phaze-a6hm.1 sortable-column contract -- see _pending_page_stmt.
        order_by=sort.order_by() if sort is not None else (Tracklist.created_at.desc(),),
        tiebreaker=(Tracklist.id.desc(),),
    )


async def get_trackid_files_page(
    session: AsyncSession, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, sort: SortState | None = None
) -> Page[dict[str, Any]]:
    """Return ONE BOUNDED page of the Track-ID identity-signal rows (IDENT-01), degrade-safe.

    The membership is UNCHANGED from Phase 59 -- music/video files that carry at least one
    ``FingerprintResult`` row OR a linked ``Tracklist`` (RESEARCH Open-Q2) -- and so is the per-row
    dict shape, so the Track-ID table renders exactly as before. What changed (phaze-1wvb) is that
    the read now carries a LIMIT: it is served as bounded OFFSET pages with a ``page_size + 1``
    sentinel for ``has_next``, NEVER a whole-corpus COUNT (paging contract rule 2 / the T-87-11 DoS
    mitigation). ``page`` / ``page_size`` are CLAMPED inside :func:`paged_stmt`, and a page past the
    end is an EMPTY page, not a 422 (rule 5).

    Ordering: newest-first display order with the MANDATORY unique ``FileRecord.id`` tiebreaker
    (rule 4). ``created_at`` ALONE is not a valid tiebreaker in phaze -- Postgres timestamp defaults
    are transaction-time constant, so every file inserted in one transaction ties exactly and OFFSET
    paging could silently skip or duplicate rows between pages.

    RENDER READ ONLY (rule 7): this feeds the Track-ID table and nothing else. The Track-ID workspace
    has NO bulk trigger of its own, and the Tracklist workspace's SEARCH/SCRAPE/MATCH ALL buttons
    enqueue their own UNBOUNDED sets (:func:`get_untracked_files` /
    :func:`get_scrape_pending_tracklists` / :func:`get_match_pending_tracklists`). Never enqueue from
    a page.

    Per-engine badge (D-01, Pitfall 1/2): two aliased LEFT joins keyed on the lowercase persisted
    ``engine`` values map ``status == "success"`` -> ``"done"``, ``"failed"`` -> ``"failed"``, and a
    missing row -> ``"pending"`` (see :func:`_trackid_engine_badge`).

    Tracklist match-state (D-04): a tracklist LINKED to this file (``Tracklist.file_id == files.id``)
    -> ``"matched"`` + that linked tracklist's best ``match_confidence``; else, if any unlinked
    candidate tracklist exists, ``"candidate"`` + the global best candidate ``match_confidence``;
    else ``"no match"`` with confidence ``None``. NOTE: with the current schema a candidate
    (``file_id IS NULL``) is not tied to a specific file, so the candidate fallback surfaces the
    system-wide best candidate -- the literal D-04 reading. Both candidate probes are themselves
    bounded (``LIMIT 1`` / ``EXISTS``), so no part of this read scales with the corpus.

    Degrade-safe via a SAVEPOINT returning an EMPTY :class:`Page` on any error (rule 6): rolling back
    only the nested scope keeps the outer request transaction usable for the rest of the workspace.
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
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
            stmt = _trackid_page_stmt(_trackid_linked_conf_subq(), page=page, page_size=page_size, sort=sort)
            raw = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("trackid_files_page_degraded", page=page, page_size=page_size, exc_info=True)
        return Page(rows=[], page=page, page_size=page_size, has_next=False)

    sentinel_rows, has_next = split_sentinel(raw, page_size)
    files: list[dict[str, Any]] = []
    for filename, path, af_status, pk_status, linked_file_id, linked_conf in sentinel_rows:
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
    return Page(rows=files, page=page, page_size=page_size, has_next=has_next)


async def get_tracklist_sets_page(
    session: AsyncSession, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, sort: SortState | None = None
) -> Page[dict[str, Any]]:
    """Return ONE BOUNDED page of the per-set Tracklist rows (IDENT-02 / D-07/D-08), degrade-safe.

    One row per ``Tracklist`` (a "set"), carrying the set name + path, the match-state +
    ``matched_to_file`` flag, and the D-07 per-set track coverage: ``tracks_confident`` of
    ``tracks_total`` derived from ``TracklistTrack.confidence`` over the tracklist's versioned tracks
    (``COUNT(confidence)`` counts only non-NULL confidences -> the confident N; ``COUNT(id)`` -> the
    total M). Membership and row shape are UNCHANGED from Phase 59; phaze-1wvb only added the bound.

    The track counts stay scoped to the tracklist's ``latest_version_id`` only (the same convention
    the tracklists router uses) -- a re-scraped tracklist with multiple versions must NOT sum coverage
    across versions, which would inflate the D-07 N/M. A tracklist whose ``latest_version_id`` is NULL
    reports 0/0.

    Bounded per the paging contract: OFFSET pages with a ``page_size + 1`` sentinel for ``has_next``
    (never a COUNT -- rule 2), newest-first display order with the MANDATORY unique ``Tracklist.id``
    tiebreaker (rule 4 -- ``created_at`` ties for every row written in one transaction), and clamped
    inputs that yield an empty page rather than an error (rule 5).

    RENDER READ ONLY (rule 7): the SEARCH / SCRAPE / MATCH ALL triggers above this table enqueue
    :func:`get_untracked_files` / :func:`get_scrape_pending_tracklists` /
    :func:`get_match_pending_tracklists`, which are UNBOUNDED BY DESIGN and untouched. Paging THIS
    read cannot under-enqueue anything; paging THOSE would.

    Degrade-safe via a SAVEPOINT returning an EMPTY :class:`Page` on any error (rule 6).
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    try:
        async with session.begin_nested():
            stmt = _tracklist_sets_page_stmt(page=page, page_size=page_size, sort=sort)
            raw = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("tracklist_sets_page_degraded", page=page, page_size=page_size, exc_info=True)
        return Page(rows=[], page=page, page_size=page_size, has_next=False)

    sentinel_rows, has_next = split_sentinel(raw, page_size)
    sets: list[dict[str, Any]] = []
    for external_id, artist, event, file_id, filename, path, total, confident in sentinel_rows:
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
    return Page(rows=sets, page=page, page_size=page_size, has_next=has_next)


# --- ANALYSIS_FAILED bucket (Phase 44, D-02) --------------------------------------------
#
# The files that GAVE UP -- terminal windowed-analysis failure (Phase 43 sets
# FileState.ANALYSIS_FAILED). This is its OWN bucket, intentionally ABSENT from
# PIPELINE_STAGES (lines 40-49): adding it there would double-count failed files in the
# linear stat bar. Surfaced on the dashboard alongside the STRAGGLER bucket (still grinding)
# as two distinct outcomes of the 4h-timeout incident. Reads the indexed files.state
# (ix_files_state, models/file.py:74) -- NOT saq_jobs (a failed file has no live job).


async def get_analysis_failed_files(session: AsyncSession) -> list[FileRecord]:
    """Return the FileRecords with a terminal analyze-failure marker (the analysis-gave-up bucket).

    Phase 90 (PR-A, D-09): DERIVED from ``failed_clause(Stage.ANALYZE)`` (an ``analysis`` row whose
    ``failed_at`` is non-NULL) -- no longer the retired ``files.state = 'analysis_failed'`` column.
    Composes the LOCKED clause verbatim. Distinct from the STRAGGLER bucket
    (:func:`get_straggler_count`, still-running jobs from ``saq_jobs``) -- these files have
    terminally failed and carry no live job.
    """
    result = await session.execute(select(FileRecord).where(failed_clause(Stage.ANALYZE)))
    return list(result.scalars().all())


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

    phaze-l1km: this predicate cannot distinguish an ORPHANED ledger row (a timed-out process_file
    whose SAQ job is gone) from the LIVE-in-flight marker of a still-running deepen job. That live/dead
    split is a READ of the SAQ-owned ``saq_jobs`` broker (absent in some envs), so it is applied by the
    caller (:func:`phaze.routers.pipeline.trigger_backfill_cloud`) via the degrade-safe
    :func:`get_live_job_keys`, NOT baked into this always-on candidate query.
    """
    return (
        select(FileRecord, FileMetadata.duration)
        .join(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .where(
            # Phase 90 (PR-A, D-09): DERIVED terminal analyze-failure via ``failed_clause(ANALYZE)`` (an
            # analysis row with ``failed_at`` set), no longer ``files.state == ANALYSIS_FAILED``.
            failed_clause(Stage.ANALYZE),
            FileMetadata.duration >= threshold_sec,
            exists(select(SchedulingLedger.key).where(SchedulingLedger.key == "process_file:" + cast(FileRecord.id, String))),
            # Phase 90 (PR-A) idempotency guard: exclude a file already routed to the cloud path (it
            # carries an ACTIVE ``cloud_job`` sidecar). The retired ``state == ANALYSIS_FAILED`` gate WAS
            # the double-click guard -- a held file's state flipped ANALYSIS_FAILED -> AWAITING_CLOUD, so
            # a second backfill re-selected nothing. The derived ``failed_clause`` marker does NOT
            # transition when a file is held (the backfill routes to cloud without clearing it), so this
            # ``~exists(active cloud_job)`` conjunct restores the D-10 no-whole-backlog-sweep idempotency,
            # mirroring the identical guard in :func:`get_discovered_files_with_duration`.
            ~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status.in_(_ACTIVE_CLOUD_STATUSES))),
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
    UNBOUNDED BY DESIGN (paging contract rule 7, phaze.services.pagination). This is the ENQUEUE set
    -- the exact membership the bulk trigger and the recovery producer must schedule -- so it must
    NEVER be paged or LIMITed; doing so would silently under-enqueue the backlog. The WORKSPACE
    renders the bounded :func:`get_pending_files_page` instead. Keep the two readers separate.
    """
    stmt = select(FileRecord).where(
        FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
        eligible_clause(Stage.METADATA),
        ~dedup_resolved_clause(),
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _pending_page_stmt(stage: Stage, *, page: int, page_size: int, sort: SortState | None = None) -> Select[Any]:
    """Build the bounded pending-set page SELECT shared by the metadata and fingerprint workspaces.

    The SAME membership predicate the unbounded enqueue readers use
    (:func:`get_metadata_pending_files` / :func:`get_fingerprint_pending_files`), wrapped in the
    :mod:`phaze.services.pagination` contract: newest-first display order with the MANDATORY unique
    ``FileRecord.id`` tiebreaker (``created_at`` ties -- Postgres timestamp defaults are
    transaction-time constant), OFFSET paging, and a ``page_size + 1`` sentinel instead of a COUNT.
    """
    return paged_stmt(
        select(FileRecord).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES), eligible_clause(stage), ~dedup_resolved_clause()),
        page=page,
        page_size=page_size,
        # phaze-a6hm.1: the operator's whitelisted column when they picked one, else the newest-first
        # default. `sort` is a RESOLVED SortState, so this can only ever be an enumerated expression.
        order_by=sort.order_by() if sort is not None else (FileRecord.created_at.desc(),),
        tiebreaker=(FileRecord.id.desc(),),
    )


async def get_pending_files_page(
    session: AsyncSession, stage: Stage, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, sort: SortState | None = None
) -> Page[FileRecord]:
    """Return ONE bounded page of ``stage``'s pending set -- the RENDER read for the enrich workspaces.

    phaze-5462: the metadata and fingerprint workspaces used to render
    :func:`get_metadata_pending_files` / :func:`get_fingerprint_pending_files` in full, inline and
    UNBOUNDED -- exactly the cliff phaze-5462 fixed on the Analyze tab. They measured a harmless
    ~70 KB with zero rows only because those backlogs happen to be EMPTY in production today; a
    metadata stall would have reproduced the 12.7 MB Analyze payload verbatim. This is the bounded
    read those two surfaces render instead.

    CRITICAL (paging contract rule 7): this is the RENDER read ONLY. The bulk EXTRACT ALL /
    FINGERPRINT ALL triggers keep calling the UNBOUNDED ``get_*_pending_files`` readers, because
    enqueuing only the first page would silently under-enqueue the backlog -- a far worse bug than a
    long table. Do NOT "unify" these two readers.

    SAVEPOINT degrade-safe: returns an EMPTY page on any error rather than 500ing the workspace.
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    try:
        async with session.begin_nested():
            raw = (await session.execute(_pending_page_stmt(stage, page=page, page_size=page_size, sort=sort))).scalars().all()
    except Exception:
        logger.warning("pending_files_page_degraded", stage=stage.value, page=page, page_size=page_size, exc_info=True)
        return Page(rows=[], page=page, page_size=page_size, has_next=False)
    rows, has_next = split_sentinel(raw, page_size)
    return Page(rows=rows, page=page, page_size=page_size, has_next=has_next)


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
    UNBOUNDED BY DESIGN (paging contract rule 7, phaze.services.pagination). This is the ENQUEUE set
    -- the exact membership the bulk trigger and the recovery producer must schedule -- so it must
    NEVER be paged or LIMITed; doing so would silently under-enqueue the backlog. The WORKSPACE
    renders the bounded :func:`get_pending_files_page` instead. Keep the two readers separate.
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

    Runs the convergence query (files NOT yet proposed -- ``~done_clause(PROPOSE)`` -- with BOTH a
    ``FileMetadata`` AND a COMPLETED ``AnalysisResult`` row -- the EXACT set the manual proposals
    triggers use), then SORTS the file-id strings before chunking into ``batch_size`` groups.
    Phase 90 (PR-A, Pitfall 4): the propose-exclusion replaces the retired ``files.state`` membership,
    so an already-proposed file is never re-batched.

    Sorting BEFORE chunking is load-bearing (D-04, 42-RESEARCH Pitfall 2): ``generate_proposals``
    is keyed on ``generate_proposals:<sha256(sorted file_ids)>`` (an order-independent SET hash),
    so the manual trigger and recovery MUST produce the IDENTICAL batch MEMBERSHIP to land on the
    IDENTICAL key and dedup against an in-flight batch. Both paths call THIS helper, so their
    batches -- and therefore their set-hash keys -- are guaranteed to match. Pure ORM / bound
    params, NO f-string SQL (T-42-03).
    """
    stmt = (
        select(FileRecord)
        # Phase 90 (PR-A, Pitfall 4): the ``files.state IN (ANALYZED, METADATA_EXTRACTED)`` gate is
        # REPLACED by ``~done_clause(Stage.PROPOSE)`` -- a file with an existing proposal is a done
        # PROPOSE and is EXCLUDED, so no already-proposed file is ever re-proposed. The two EXISTS
        # convergence clauses below (metadata present AND a COMPLETED analysis row) still bound the set.
        .where(~done_clause(Stage.PROPOSE))
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
    # Contract rule 3: the page size is owned by phaze.services.pagination, never re-spelled here.
    page_size: int = DEFAULT_PAGE_SIZE
    has_next: bool = False


def _files_page_stmt(*, page: int, page_size: int, stage: Stage | None, bucket: str | None, sort: SortState | None = None) -> Select[Any]:
    """Build the bounded per-page derivation SELECT (extracted so the EXPLAIN test can probe it directly).

    ``select(FileRecord, stage_status_case(METADATA), ... , stage_status_case(APPLY))`` ordered by
    ``sort`` (phaze-a6hm.3) -- or, absent a resolved sort, the ``FileRecord.id`` PK index -- and LIMITed
    to ``page_size + 1`` (the sentinel that yields ``has_next`` with NO COUNT). Each ``stage_status_case``
    is a correlated CASE over the Phase-77 partial indexes (``ix_metadata_failed`` / ``ix_analysis_completed``
    / ``ix_analysis_failed`` / ``ix_fprint_success``), so the derivation touches only the page rows. The
    optional ``stage``+``bucket`` filter is applied as ``stage_status_case(stage) == bucket`` -- a pure
    ORM bound-param comparison (never f-string SQL, T-87-14); the caller validates ``stage``/``bucket``
    against the ``Stage``/``Status`` allowlists.
    """
    cols = [stage_status_case(s) for s in _FILES_PAGE_STAGES]
    stmt = select(FileRecord, *cols)
    if stage is not None and bucket is not None:
        stmt = stmt.where(stage_status_case(stage) == bucket)
    # The paging contract (phaze.services.pagination): OFFSET + a page_size+1 sentinel for has_next
    # (never a whole-corpus COUNT -- T-87-11). FileRecord.id is the mandatory unique tiebreaker
    # (paging contract rule 4) regardless of `sort` -- an operator-chosen column ties far more often
    # than the PK does (column_sort contract, SortState.order_by docstring).
    return paged_stmt(
        stmt,
        page=page,
        page_size=page_size,
        order_by=sort.order_by() if sort is not None else (),
        tiebreaker=(FileRecord.id,),
    )


async def get_files_page(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    stage: Stage | None = None,
    bucket: str | None = None,
    sort: SortState | None = None,
) -> FilesPage:
    """Return one bounded, per-row-derived page of files -- SAVEPOINT degrade-safe, never a whole-corpus scan.

    Clamps ``page``/``page_size`` via the :mod:`phaze.services.pagination` contract, builds the bounded :func:`_files_page_stmt`, and
    runs it inside a ``begin_nested()`` SAVEPOINT so ANY error (a DB hiccup, an aborted transaction, a
    build-time raise) rolls back the nested scope ALONE, logs a warning, and returns a safe EMPTY page --
    it NEVER 500s the poll (INFLIGHT-02 / D-00c / T-87-12). ``has_next`` is derived from the LIMIT+1
    sentinel row, so pagination costs no COUNT. The six correlated ``stage_status_case`` columns are read
    back into each row's ``buckets`` dict keyed by ``Stage`` value (metadata/fingerprint/analyze/propose/
    review/apply) -- the derived buckets the ``_stage_pill`` cells render (never ``FileRecord.state``).

    ``stage``+``bucket`` are accepted NOW (plumbed straight through to the filter) so Plan 05 -- which
    wires the status filter bar -- is templates-only. Passing only one of the pair is a no-op filter.

    ``sort`` (phaze-a6hm.3) is an already-resolved :class:`~phaze.routers.column_sort.SortState` from
    the router's ``FILES_SORT`` contract -- this layer never sees the raw wire ``sort``/``order``
    strings, only the whitelisted expression :meth:`~phaze.routers.column_sort.SortState.order_by`
    hands back. ``None`` (e.g. a caller that predates phaze-a6hm.3) falls back to the original
    ``FileRecord.id`` order.
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    try:
        async with session.begin_nested():
            stmt = _files_page_stmt(page=page, page_size=page_size, stage=stage, bucket=bucket, sort=sort)
            result = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("files_page_degraded", page=page, page_size=page_size, exc_info=True)
        return FilesPage(rows=[], page=page, page_size=page_size, has_next=False)
    page_rows, has_next = split_sentinel(result, page_size)
    rows = [
        FilesPageRow(
            file=row[0],
            buckets={stage_member.value: row[idx + 1] for idx, stage_member in enumerate(_FILES_PAGE_STAGES)},
        )
        for row in page_rows
    ]
    return FilesPage(rows=rows, page=page, page_size=page_size, has_next=has_next)


async def get_file_stage_buckets(session: AsyncSession, file_id: uuid.UUID) -> dict[str, str]:
    """Return ONE file's six derived per-stage buckets (keyed by ``Stage`` value) — the matrix row, single-file.

    The record slide-in's Stage-Eligibility pills must show the SAME derived status the Files matrix
    renders for that file (CONSOLE-01: one status source, no divergent second derivation), so this is
    the same six correlated ``stage_status_case`` CASE columns as :func:`_files_page_stmt`, scoped to
    a single ``FileRecord.id`` — an O(1) single-row read, never a corpus scan. Degrades to an
    all-``not_started`` mapping on any error (the pane renders, never 500s) — mirroring
    :func:`get_files_page`'s SAVEPOINT degrade posture.
    """
    cols = [stage_status_case(s) for s in _FILES_PAGE_STAGES]
    try:
        async with session.begin_nested():
            row = (await session.execute(select(*cols).where(FileRecord.id == file_id))).one_or_none()
    except Exception:
        logger.warning("file_stage_buckets_degraded", file_id=str(file_id), exc_info=True)
        row = None
    if row is None:
        return dict.fromkeys((s.value for s in _FILES_PAGE_STAGES), "not_started")
    return {stage_member.value: row[idx] for idx, stage_member in enumerate(_FILES_PAGE_STAGES)}
