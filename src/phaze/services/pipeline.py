"""Pipeline orchestration service -- stage counts and file queries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import distinct, exists, func, select, text
import structlog

from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.pipeline_stage_control import PipelineStageControl
from phaze.models.proposal import ProposalStatus, RenameProposal
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
