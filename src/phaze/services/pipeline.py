"""Pipeline orchestration service -- stage counts and file queries."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from saq.utils import now as saq_now
from sqlalchemy import String, cast, distinct, exists, func, select, text
import structlog

from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
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
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select


logger = structlog.get_logger(__name__)


# Music + video file types -- the shared denominator for the per-file parallel
# stages (Metadata/Fingerprint/Analyze). Mirrors the filter the trigger endpoints
# use at routers/pipeline.py:318-319 so the dashboard denominator matches the set
# of files those stages are actually enqueued for.
MUSIC_VIDEO_TYPES = [ext.lstrip(".") for ext, cat in EXTENSION_MAP.items() if cat in (FileCategory.MUSIC, FileCategory.VIDEO)]


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


async def get_pipeline_stats(session: AsyncSession) -> dict[str, int]:
    """Get file counts per pipeline stage.

    Returns dict mapping state name to count, e.g.:
    {"discovered": 42, "analyzed": 10, "proposal_generated": 5, ...}
    """
    stmt = select(FileRecord.state, func.count(FileRecord.id)).group_by(FileRecord.state)
    result = await session.execute(stmt)
    counts: dict[str, int] = {row[0]: row[1] for row in result.all()}
    # Ensure all stages are present (default 0)
    return {stage.value: counts.get(stage.value, 0) for stage in PIPELINE_STAGES}


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
            q = app_state.task_router.queue_for(agent.id)
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


async def get_stage_progress(session: AsyncSession) -> dict[str, dict[str, int | None]]:
    """Authoritative per-DAG-node reconcile source (D-03) -- counts each stage's OUTPUT table.

    Unlike :func:`get_pipeline_stats`, which groups by the LINEAR ``FileRecord.state``
    (a single enum per file) and therefore STRUCTURALLY cannot report parallel-stage
    done-counts, this query counts ``COUNT(DISTINCT file_id / tracklist_id)`` against
    each stage's write target. A file that is both fingerprinted AND analyzed contributes
    to BOTH ``fingerprint.done`` and ``analyze.done`` here -- impossible to express through
    the single-valued state enum (RESEARCH Q5).

    Returns a dict keyed by DAG node, each value ``{"done": int, "total": int | None}``:

    - ``discovery``   -- done = COUNT(files); total = itself (bar is always 100%)
    - ``metadata``    -- done = DISTINCT file_id in ``metadata``; total = music/video file count
    - ``fingerprint`` -- done = DISTINCT file_id in ``fingerprint_results`` (status='completed'); total = music/video count
    - ``analyze``     -- done = DISTINCT file_id in ``analysis``; total = music/video count
    - ``scan_search`` -- done = DISTINCT file_id in ``tracklists``; total = ``None`` (counter-only; the UI
      renders ``done / —``). No DB table defines "should get a tracklist" so NO denominator is fabricated.
    - ``scrape``      -- done = DISTINCT tracklist_id in ``tracklist_versions``; total = COUNT(tracklists)
    - ``match``       -- done = DISTINCT tracklist_id reachable from ``discogs_links``; total = COUNT(tracklists)
    - ``proposals``   -- done = DISTINCT file_id in ``proposals``; total = convergence set (files with BOTH
      ``metadata`` AND ``analysis``, mirroring routers/pipeline.py:116-128)
    - ``execute``     -- done = DISTINCT file_id with a completed ``execution_log`` row; total = approved-proposal count

    Each source is wrapped in :func:`_safe_count` so a single failing stage degrades to
    ``done=0`` (or ``total=0``) and the function never raises into the 5s poll. The linear
    :func:`get_pipeline_stats` is intentionally left untouched -- it remains the truth for the
    strictly-linear Proposals/Approved/Executed tail where ``state`` IS the truth.
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
        "metadata": {
            "done": await _safe_count(session, select(func.count(distinct(FileMetadata.file_id))), node="metadata"),
            "total": music_video_total,
        },
        "fingerprint": {
            "done": await _safe_count(
                session,
                select(func.count(distinct(FingerprintResult.file_id))).where(FingerprintResult.status == "completed"),
                node="fingerprint",
            ),
            "total": music_video_total,
        },
        "analyze": {
            "done": await _safe_count(session, select(func.count(distinct(AnalysisResult.file_id))), node="analyze"),
            "total": music_video_total,
        },
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


async def count_active_agents(session: AsyncSession) -> int:
    """Return the number of online agents (``revoked_at IS NULL`` AND ``last_seen_at IS NOT NULL``).

    Counts agents matching :func:`phaze.services.enqueue_router.select_active_agent`'s EXACT
    liveness definition (CONTEXT decision 2 -- do NOT invent a new liveness rule): a revoked agent
    (``revoked_at`` set) and a never-seen agent (``last_seen_at`` None) are both excluded. This drives
    the DAG Fingerprint-Scan node's "Needs agent" gate -- ``scan_live_set`` is a per-agent task and
    raises ``NoActiveAgentError`` when no agent is online, so the button must stay disabled until one
    is.

    Failure isolation (T-40-05): the read runs inside a SAVEPOINT (``session.begin_nested()``) so a
    DB hiccup on the hot 5s poll does NOT expire the dashboard's loaded ORM objects. On ANY exception
    it logs ``active_agent_count_degraded`` and returns 0. That degrade default is FAIL-SAFE:
    ``agentOnline == 0`` leaves the new node blocked "Needs agent", so a liveness-read failure can
    never let a scan launch with no agent online. It NEVER raises into the 5s /pipeline/stats poll.
    """
    try:
        async with session.begin_nested():
            count = (await session.execute(select(func.count(Agent.id)).where(Agent.revoked_at.is_(None), Agent.last_seen_at.is_not(None)))).scalar()
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

    Poll-safe via :func:`_safe_count` (mirrors the ANALYZED-count precedent in
    :func:`get_pipeline_stats`): a DB hiccup degrades this node to 0 and rolls back the aborted
    transaction rather than 500ing the hot 5s /pipeline/stats poll. ``ANALYSIS_FAILED`` is its
    own bucket and is deliberately NOT added to ``PIPELINE_STAGES`` (D-02 -- it would double-count
    in the linear bar).
    """
    return await _safe_count(
        session,
        select(func.count(FileRecord.id)).where(FileRecord.state == FileState.ANALYSIS_FAILED),
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
    """Return ``(FileRecord, duration)`` for every DISCOVERED file (LEFT OUTER JOIN metadata).

    The duration is the joined ``FileMetadata.duration`` (or ``None`` when no metadata row
    exists yet). Captured into the in-memory list here because ``FileRecord.file_metadata`` is
    ``lazy="noload"`` -- a later access in a background task would NOT lazy-load it, so the
    duration the per-file router (Plan 02) routes on must be read in this query.
    """
    stmt = (
        select(FileRecord, FileMetadata.duration)
        .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .where(FileRecord.state == FileState.DISCOVERED)
    )
    result = await session.execute(stmt)
    return [(record, duration) for record, duration in result.all()]


async def get_awaiting_cloud_count(session: AsyncSession) -> int:
    """Return COUNT of files in ``FileState.AWAITING_CLOUD``, degrading to 0 on any DB error.

    Drives the dashboard "Awaiting cloud" card (D-05). Poll-safe via :func:`_safe_count`
    (mirrors :func:`get_analysis_failed_count`): a DB hiccup degrades this node to 0 and rolls
    back the aborted transaction rather than 500ing the hot 5s /pipeline/stats poll.
    """
    return await _safe_count(
        session,
        select(func.count(FileRecord.id)).where(FileRecord.state == FileState.AWAITING_CLOUD),
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


async def get_pushing_count(session: AsyncSession) -> int:
    """Return COUNT of files in ``FileState.PUSHING`` (staged, rsync in progress), degrading to 0 (D-09).

    Drives the dashboard "Staged (pushing)" card -- the left half of the bounded cloud window
    (files mid-rsync to the compute agent's scratch dir). Poll-safe via :func:`_safe_count`
    (mirrors :func:`get_awaiting_cloud_count`): a DB hiccup degrades this node to 0 and rolls back
    the aborted transaction rather than 500ing the hot 5s /pipeline/stats poll. This is the
    OBSERVATIONAL per-card count -- the load-bearing ≤N backpressure is :func:`get_cloud_window_count`,
    which is intentionally NOT degrade-safe so the cron never over-stages on a transient error.
    """
    return await _safe_count(
        session,
        select(func.count(FileRecord.id)).where(FileRecord.state == FileState.PUSHING),
        node="pushing",
    )


async def get_pushed_count(session: AsyncSession) -> int:
    """Return COUNT of files in ``FileState.PUSHED`` (landed on compute, within analysis), degrading to 0 (D-09).

    Drives the dashboard "Analyzing (cloud)" card -- the right half of the bounded cloud window
    (files that finished rsync and are awaiting/within remote analysis). Poll-safe via
    :func:`_safe_count`, exactly like :func:`get_pushing_count`. Observational only; the window
    cap itself is enforced by :func:`get_cloud_window_count` from committed FileState.
    """
    return await _safe_count(
        session,
        select(func.count(FileRecord.id)).where(FileRecord.state == FileState.PUSHED),
        node="analyzing_cloud",
    )


# --- Phase 50 bounded cloud-window helpers (D-03/D-08, CLOUDPIPE-01) ---------------------
#
# The window is the load-bearing ≤N backpressure: the count of files staged-or-in-flight to the
# single compute agent (FileState IN {PUSHING, PUSHED}) must never exceed cloud_max_in_flight.
# The ``stage_cloud_window`` cron composes these two helpers in ONE transaction -- count the
# window from COMMITTED FileState truth (NOT the SAQ ledger), then SELECT ... FOR UPDATE SKIP
# LOCKED up to the free slots so a concurrent tick cannot double-stage the same row (T-50-scratch-dos).


async def get_cloud_window_count(session: AsyncSession) -> int:
    """Return COUNT of files in the ≤N cloud window: ``state IN {PUSHING, PUSHED}`` (Phase 50, D-03/D-08).

    The window is counted from COMMITTED FileState truth -- a row is in the window from the moment
    the staging cron flips it to ``PUSHING`` (rsync in progress) through ``PUSHED`` (landed on the
    compute scratch dir, within analysis). ``slots = cloud_max_in_flight - window`` is what the cron
    is allowed to newly stage. NOT poll-safe-degraded like the dashboard counters: a real COUNT is
    required so the cron never over-stages on a transient error (a raise holds the window instead).
    """
    return int(
        (await session.execute(select(func.count(FileRecord.id)).where(FileRecord.state.in_([FileState.PUSHING, FileState.PUSHED])))).scalar() or 0
    )


async def get_cloud_staging_candidates(session: AsyncSession, limit: int) -> list[FileRecord]:
    """Return up to ``limit`` oldest ``AWAITING_CLOUD`` files (FIFO by ``created_at``), row-locked (Phase 50, D-03).

    ``ORDER BY created_at ASC`` makes staging FIFO (the longest-held file goes first). ``FOR UPDATE
    SKIP LOCKED`` lets a concurrent staging tick skip rows this transaction already locked instead of
    blocking or double-staging them (T-50-scratch-dos). ``limit`` is the free-slot count the caller
    computed as ``cloud_max_in_flight - window``; the caller must guarantee ``limit > 0`` before
    calling (a ``LIMIT 0`` would be a pointless round-trip).
    """
    stmt = (
        select(FileRecord)
        .where(FileRecord.state == FileState.AWAITING_CLOUD)
        .order_by(FileRecord.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list((await session.execute(stmt)).scalars().all())


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
    """Return all music/video FileRecords -- the metadata-extraction pending set.

    The EXACT set the manual metadata triggers (``trigger_metadata_extraction`` /
    ``trigger_extraction_ui``) enqueue: every music/video file regardless of state (D-04
    backfill -- metadata extraction is idempotent per file and the deterministic
    ``extract_file_metadata:<file_id>`` key dedups an in-flight re-run). Pure ORM
    ``file_type.in_(MUSIC_VIDEO_TYPES)`` with NO interpolated operator input (T-42-03).
    """
    stmt = select(FileRecord).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_fingerprint_pending_files(session: AsyncSession) -> list[FileRecord]:
    """Return METADATA_EXTRACTED files PLUS failed-fingerprint-retry files, de-duplicated by id.

    The EXACT set the manual ``trigger_fingerprint`` API endpoint enqueues: files in
    ``METADATA_EXTRACTED`` state (ready for fingerprinting) UNION files carrying a
    ``FingerprintResult`` with ``status == "failed"`` that are not yet ``FINGERPRINTED``
    (retry per D-16). The two sets are merged and de-duplicated by id (keeping the
    ``FileRecord`` rows so the full ``FingerprintFilePayload`` can be built downstream).

    Phase 42 consistency fix: the HTMX ``trigger_fingerprint_ui`` endpoint previously queried
    ONLY ``METADATA_EXTRACTED`` (no failed-retry scope); routing it through this shared helper
    ALIGNS it with the API endpoint (it GAINS the failed-retry scope) -- an intended fix so the
    manual UI and API triggers and recovery cannot drift. Pure ORM with NO interpolated
    operator input (T-42-03).
    """
    files = await get_files_by_state(session, FileState.METADATA_EXTRACTED)

    failed_stmt = (
        select(FileRecord)
        .join(FingerprintResult, FingerprintResult.file_id == FileRecord.id)
        .where(FingerprintResult.status == "failed")
        .where(FileRecord.state != FileState.FINGERPRINTED)
    )
    failed_files = list((await session.execute(failed_stmt)).scalars().all())

    seen_ids: set[str] = set()
    out: list[FileRecord] = []
    for f in [*files, *failed_files]:
        fid = str(f.id)
        if fid not in seen_ids:
            seen_ids.add(fid)
            out.append(f)
    return out


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
        .where(exists(select(AnalysisResult.id).where(AnalysisResult.file_id == FileRecord.id)))
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
