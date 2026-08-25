"""The per-DAG-node stage progress fan-out and the stage pause/priority controls.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr). The bounded fan-out
machinery (:data:`_STATS_FANOUT` / :func:`_stats_fanout` / :func:`_read_in_own_session`) lives HERE,
beside its only consumer :func:`get_stage_progress`, rather than in a shared module: the semaphore
cap's arithmetic is stated entirely in terms of that one function's read count, so separating them
would leave the cap's rationale unreadable from either side.

PATCH TARGET NOTE (phaze-vsqpr): ``_STATS_FANOUT`` is the per-test override seam. It moved with
:func:`_stats_fanout`, so the target is now ``phaze.services.pipeline.stages._STATS_FANOUT`` --
patching the ``phaze.services.pipeline`` package attribute no longer reaches it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast as type_cast
import weakref

from sqlalchemy import distinct, func, select, text
import structlog

from phaze.enums.stage import Stage
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileRecord
from phaze.models.pipeline_stage_control import PipelineStageControl
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.pipeline.buckets import StageBucketSnapshot, _empty_buckets, _safe_bucket_counts, _safe_bucket_snapshot
from phaze.services.pipeline.common import _BUSY_FUNCTION_TO_STAGE, MUSIC_VIDEO_TYPES, _safe_count
from phaze.services.stage_status import (
    done_clause,
)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


# NOTE (Phase 82, D-05/READ-02): ``get_pipeline_stats`` -- the linear per-``FileRecord.state`` grouped
# counter -- was REMOVED here. The stats path no longer groups by (or reads) ``FileRecord.state``: its
# three former callers
# (``routers/pipeline.py`` ``_build_dag_context`` / ``build_dashboard_context`` /
# ``pipeline_stats_partial``) now derive the seven consumed keys from :func:`get_stage_progress`'s
# output-table counts (``discovered→discovery.done``, ``metadata_extracted→metadata.done``,
# ``analyzed→analyze.done``, ``proposal_generated→proposals.done``,
# ``approved→execute.total``, ``executed→execute.done``). Phase 90 (MIG-04) then removed the
# ``FileState`` enum + ``files.state`` column entirely, so the former linear ``PIPELINE_STAGES`` list
# (which enumerated the enum members) is gone -- stage membership derives from the output tables.


# Bounded fan-out for the get_stage_progress reads (CLEAN-01 / D-01/D-02/D-03). A single
# AsyncSession (one asyncpg connection) CANNOT run concurrent statements -- SQLAlchemy 2.0 raises
# IllegalStateChangeError ("another operation is in progress") -- so each concurrent read runs in
# its OWN session. The semaphore caps the extra concurrent pool checkouts: cap 4 admits all three
# heavy enrich-bucket reads at once (the serial-cost dominators) while leaving >=6 of the
# deliberately-lean 10-conn/worker pool (pool_size=5 + max_overflow=5, post-PgBouncer-exhaustion
# incident) for the request's own session + other request traffic + the orphan refresher (RESEARCH
# Pool Headroom, T-92-02-DoS). That headroom invariant holds only if the cap is GLOBAL across
# concurrently in-flight polls, not per-poll -- see phaze-28wi below.
#
# WHY NOT a module-level pre-constructed ``asyncio.Semaphore(4)``: an asyncio primitive binds to the
# event loop of its FIRST use, so a single eager module-singleton raises "bound to a different event
# loop" under pytest's per-test loops and degrades every read (a real bug, not just a test artifact).
#
# phaze-28wi: the ORIGINAL fix here built a FRESH ``asyncio.Semaphore(4)`` per poll to route around
# that loop-binding hazard, but that makes the cap per-poll rather than process-global: two
# concurrently in-flight polls each get their OWN cap-4 budget, so the ">=6 slots stay free"
# invariant above only holds for a single in-flight render -- exactly the mismatch this bead fixes.
# The cap is now bound to the CURRENT event loop lazily in a ``WeakKeyDictionary`` keyed by the
# running loop (populated on first use, one entry reused for every subsequent poll on that loop):
# production runs one loop for the process lifetime, so every poll shares the SAME semaphore and the
# cap is truly global; each pytest loop still gets its own entry (preserving the original
# loop-binding fix), and the weak key lets that entry drop once the loop is garbage-collected instead
# of accumulating one leaked entry per test.
#
# PATCHABLE SEAM -- ``_STATS_FANOUT`` is the override 92-03 Task 2 sets (per-test, in the test loop)
# to ``asyncio.Semaphore(1)`` so the fan-out SERIALIZES onto the single shared per-test connection
# (concurrent reads on one connection would raise IllegalStateChangeError); it also monkeypatches
# ``phaze.database.async_session`` to route the fan-out through that connection. Both are resolved at
# CALL time (the deferred import + this module attribute) so the routing takes effect, and take
# priority over the loop-keyed cache below (checked first).
_STATS_FANOUT: asyncio.Semaphore | None = None

# Cap on concurrent extra pool checkouts a single fan-out read may hold (see the module comment
# above for the arithmetic).
_STATS_FANOUT_CAP = 4

# Loop-keyed cache of the process-global fan-out semaphore (phaze-28wi). A ``WeakKeyDictionary`` so a
# closed event loop's entry is collected instead of leaking one Semaphore per test loop.
_STATS_FANOUT_CACHE: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _stats_fanout() -> asyncio.Semaphore:
    """Return the process-global fan-out cap for the CURRENT loop (phaze-28wi).

    The ``_STATS_FANOUT`` test override wins when set (routes onto the per-test connection, see the
    module comment above). Otherwise returns the SAME cap-4 :class:`asyncio.Semaphore` for every call
    on this event loop -- created lazily on first use and cached in :data:`_STATS_FANOUT_CACHE` -- so
    the cap bounds ALL concurrently in-flight polls collectively, not just the reads within one poll.
    """
    if _STATS_FANOUT is not None:
        return _STATS_FANOUT
    loop = asyncio.get_running_loop()
    fanout = _STATS_FANOUT_CACHE.get(loop)
    if fanout is None:
        fanout = asyncio.Semaphore(_STATS_FANOUT_CAP)
        _STATS_FANOUT_CACHE[loop] = fanout
    return fanout


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
    both metadata-extracted AND analyzed contributes to BOTH ``metadata.done`` and ``analyze.done``
    here -- impossible to express through the single-valued state enum (RESEARCH Q5). Phase 82
    (READ-02, D-05) removed the former state-grouped ``get_pipeline_stats`` entirely; the stats path
    now derives its seven keys from THIS function (no ``FileRecord.state`` read).

    Returns a dict keyed by DAG node. The two ENRICH nodes carry the FIVE-BUCKET shape
    ``{not_started, in_flight, done, skipped, failed, total}`` (Phase 82 + Phase-87 ``skipped``); every OTHER node keeps
    ``{"done": int, "total": int | None}``:

    - ``discovery``   -- done = COUNT(files); total = itself (bar is always 100%)
    - ``metadata``    -- FIVE-BUCKET via ``stage_status_case(METADATA)`` over music/video files
      (:func:`_safe_bucket_counts`); ``done`` = row present + ``failed_at`` NULL; total = music/video count
    - ``analyze``     -- FIVE-BUCKET via ``stage_status_case(ANALYZE)``; ``done`` = ``analysis`` row with
      ``analysis_completed_at`` NOT NULL (a partial in-flight row is ``in_flight``, not done); total = music/video count
    - ``tracklist``   -- done = DISTINCT file_id in ``tracklists``; total = ``None`` (counter-only; the UI
      renders ``done / —``). No DB table defines "should get a tracklist" so NO denominator is fabricated.
      phaze-2akf renamed this node from ``scan_search`` and DELETED the separate ``scrape`` node beside
      it. Both were shaped by the legacy two-step name-search-then-scrape path; the drain collapses
      derive -> search -> score -> render -> parse -> persist into ONE operation, so "searched but not
      yet scraped" is no longer a state a row can be in. ``scrape.done`` (DISTINCT tracklist_id in
      ``tracklist_versions``) over ``scrape.total`` (COUNT(tracklists)) is now a tautology: every row the
      drain writes gets its first version in the same transaction, so the bar reported 100% always and
      measured nothing.
    - ``match``       -- done = DISTINCT tracklist_id reachable from ``discogs_links``; total = COUNT(tracklists)
    - ``proposals``   -- done = DISTINCT file_id in ``proposals``; total = convergence set (metadata
      DONE AND analysis DONE, mirroring ``get_proposal_pending_batches``'s ``_proposal_pending_clauses``
      ready-set gate below -- phaze-nuyn, phaze-rhs6m)
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
    # (mirrors get_proposal_pending_batches's ready-set, ``_proposal_pending_clauses`` below). The
    # metadata conjunct was a bare row-existence check until phaze-rhs6m, matching
    # ``_proposal_pending_clauses`` exactly -- and the note here recorded that as "a separate,
    # adjacent gap, not this one". phaze-rhs6m CLOSED that gap at the ready-set, so this denominator
    # moves WITH it, onto the same ``done_clause(Stage.METADATA)`` builder: leaving it bare would
    # re-open the phaze-nuyn drift on the other conjunct, and would put a proposals denominator on
    # the dashboard that counts metadata-FAILED files GENERATE ALL will never batch -- the dishonest
    # UI ``_proposal_pending_clauses``'s extraction (phaze-37i1.2) exists to prevent. The analysis
    # conjunct uses
    # ``done_clause(Stage.ANALYZE)`` -- the same completion-discriminated predicate
    # ``_proposal_pending_clauses`` hand-rolls (DERIV-03: ``analysis_completed_at IS NOT NULL``) --
    # instead of bare existence, so this no longer counts a mid-flight partial analysis row
    # (upserted at analysis START, NULL aggregates) or a terminally-failed analyze row (``failed_at``
    # set, ``analysis_completed_at`` NULL) neither of which get_proposal_pending_batches will ever
    # batch. Phase 57.1 added that discriminator to the ready-set only; this fixes the drift
    # (phaze-nuyn) by composing from the shared ``done_clause`` builder so the two cannot drift again.
    convergence_stmt = select(func.count(FileRecord.id)).where(done_clause(Stage.METADATA)).where(done_clause(Stage.ANALYZE))
    tracklist_stmt = select(func.count(distinct(Tracklist.file_id)))
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
    bucket_default: dict[str, int] = _empty_buckets()
    metadata_bucket_default = StageBucketSnapshot(counts=_empty_buckets(), available=False)

    # ONE semaphore shared across every read in THIS poll -- and, since phaze-28wi, across every
    # OTHER concurrently in-flight poll on the SAME running loop too, so the cap bounds them all
    # collectively rather than per-poll (see _stats_fanout).
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
        analyze_b,
        tracklist_done,
        match_done,
        proposals_done,
        execute_done,
        execute_total,
        # asyncio.gather with >6 awaitables of mixed return types collapses to list[object] under mypy,
        # so pin the exact per-node tuple shape with a single cast (int counts + 3 enrich-bucket dicts).
        # NOTE: typing.cast is aliased type_cast -- the bare `cast` name is sqlalchemy's SQL cast (used
        # elsewhere in this module).
    ) = type_cast(
        "tuple[int, int, int, int, StageBucketSnapshot, dict[str, int], int, int, int, int, int]",
        await asyncio.gather(
            _read_in_own_session(fanout, lambda s: _safe_count(s, mv_total_stmt, node="music_video_total"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, tracklist_total_stmt, node="tracklist_total"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, discovery_stmt, node="discovery"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, convergence_stmt, node="proposals_total"), 0),
            _read_in_own_session(fanout, lambda s: _safe_bucket_snapshot(s, Stage.METADATA), metadata_bucket_default),
            _read_in_own_session(fanout, lambda s: _safe_bucket_counts(s, Stage.ANALYZE), bucket_default),
            _read_in_own_session(fanout, lambda s: _safe_count(s, tracklist_stmt, node="tracklist"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, match_done_stmt, node="match"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, proposals_stmt, node="proposals"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, execute_done_stmt, node="execute"), 0),
            _read_in_own_session(fanout, lambda s: _safe_count(s, execute_total_stmt, node="execute_total"), 0),
        ),
    )

    return {
        "discovery": {"done": discovery_done, "total": discovery_done},
        # Phase 82 (READ-02, D-04/D-05) + Phase 87 (skipped): the two enrich nodes are FIVE-BUCKET
        # ({not_started, in_flight, done, skipped, failed} + total) via one GROUP BY stage_status_case(stage)
        # each -- so the DAG surfaces a VISIBLE failed count per enrich stage and the five buckets sum
        # to music_video_total on a healthy query. `total` stays music_video_total; `done` (still read
        # by _build_dag_context) is now the derived done-bucket. Degrade-safe (all-zero on any error).
        "metadata": {**metadata_b.counts, "total": music_video_total, "available": int(metadata_b.available)},
        "analyze": {**analyze_b, "total": music_video_total},
        "tracklist": {
            "done": tracklist_done,
            "total": None,  # counter-only: no table defines "should get a tracklist" (RESEARCH Q5 / UI-SPEC)
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
# semantics for the two agent stages: unpaused, mid-range priority 50. Returned verbatim
# whenever the control table is unreadable/absent so the 5s /pipeline/stats poll degrades to a
# sane default instead of 500ing (T-38-DEGRADE — identical discipline to _safe_count above).
_DEFAULT_CONTROLS: dict[str, dict[str, int | bool]] = {s: {"paused": False, "priority": 50} for s in ("metadata", "analyze")}


async def get_stage_controls(session: AsyncSession) -> dict[str, dict[str, int | bool]]:
    """Read the ``pipeline_stage_control`` rows, degrading to defaults so the 5s poll never 500s.

    Returns ``{metadata, analyze}`` each mapping to ``{"paused": bool, "priority": int}``.
    On the happy path each present stage row overlays its ``paused`` / ``priority`` onto a fresh copy
    of :data:`_DEFAULT_CONTROLS`; unknown ``stage`` values are ignored (guarded by ``if r.stage in out``).

    Failure isolation mirrors :func:`_safe_count` / :func:`get_queue_activity`: the
    ``pipeline_stage_control`` table may be absent (pre-migration env) or a DB hiccup may occur, and
    EITHER must degrade to the two-stage defaults rather than raise into the hot 5s poll path
    (T-38-DEGRADE). The read runs inside a SAVEPOINT (``session.begin_nested()``) so ANY exception
    rolls back the NESTED scope ALONE -- recovering the aborted transaction WITHOUT expiring the
    caller's already-loaded ``agents`` / ``recent_scans`` ORM objects (``_build_dag_context`` runs
    after ``build_dashboard_context`` loads them on the same session; a plain ``session.rollback()``
    here would expire them and 500 the render on the next lazy load). This logs a warning and
    returns defaults.

    The caller (:func:`phaze.routers.pipeline._build_dag_context`) coerces ``paused`` to ``int`` ``0``/``1``
    so the canvas's "every dag value is a server-computed int safe to interpolate into ``x-init``"
    invariant holds (Pitfall 3 / T-35-11) — never emit a Python ``bool`` through to the template.
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(select(PipelineStageControl))).scalars().all()
        out: dict[str, dict[str, int | bool]] = {s: dict(v) for s, v in _DEFAULT_CONTROLS.items()}
        for r in rows:
            if r.stage in out:
                out[r.stage] = {"paused": r.paused, "priority": r.priority}
        return out
    except Exception:
        logger.warning("stage_controls_degraded", exc_info=True)
        return {s: dict(v) for s, v in _DEFAULT_CONTROLS.items()}


@dataclass(frozen=True)
class StageActivitySnapshot:
    """SAQ queued/active counts plus whether the broker-table read succeeded."""

    counts: dict[str, dict[str, int]]
    available: bool


_STAGE_ACTIVITY_SQL = text(
    "SELECT split_part(key, ':', 1) AS fn, status, COUNT(*) AS n FROM saq_jobs WHERE status IN ('queued', 'active') GROUP BY fn, status"
)


def _empty_stage_activity() -> dict[str, dict[str, int]]:
    return {"metadata": {"queued": 0, "active": 0}, "analyze": {"queued": 0, "active": 0}}


async def get_stage_activity_counts(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Compatibility projection of queued and active counts; use the snapshot when availability matters."""
    return (await get_stage_activity_snapshot(session)).counts


async def get_stage_activity_snapshot(session: AsyncSession) -> StageActivitySnapshot:
    """Return queued and active SAQ counts without disguising read failure as measured zero."""
    out = _empty_stage_activity()
    try:
        async with session.begin_nested():
            rows = (await session.execute(_STAGE_ACTIVITY_SQL)).all()
    except Exception:
        logger.warning("stage_activity_degraded", exc_info=True)
        return StageActivitySnapshot(counts=out, available=False)
    for function_name, status, count in rows:
        stage = _BUSY_FUNCTION_TO_STAGE.get(function_name)
        if stage is not None:
            out[stage][status] = int(count)
    return StageActivitySnapshot(counts=out, available=True)
