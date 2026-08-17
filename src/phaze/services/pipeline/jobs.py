"""Read-only probes of the SAQ-owned ``saq_jobs`` broker table.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr). Every read here is a
static-SQL scan of the live broker with NO interpolated operator input, and every one degrades to a
zero/empty answer inside its own SAVEPOINT. They are grouped by SUBSTRATE rather than by domain
because the four busy-count readers are shape-identical clones over ONE shared statement
(:data:`_STAGE_BUSY_SQL`) -- the same "define it once so the copies cannot drift" argument the
individual docstrings make. ``saq_jobs`` is SAQ-owned and never Alembic-managed, so a missing table
is an expected environment, not a bug.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text
import structlog

from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


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
    """Return the per-agent-stage in-flight job count ``{metadata, analyze}``.

    Counts ``saq_jobs`` rows with ``status IN ('queued', 'active')`` whose deterministic key prefix
    maps to one of the agent stages. This REPLACES the single global ``agentBusy`` gate
    (queued+active summed across ALL agent queues) that locked every agent stage together --
    each stage now gates on ITS OWN in-flight count, so Metadata and Analyze run in parallel
    (running one no longer blocks the other).

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
    out: dict[str, int] = {"metadata": 0, "analyze": 0}
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


# Bulk match in-flight gate (Phase 41, REQ-41-3). match_tracklist_to_discogs is a CONTROLLER task --
# NOT one of the agent stages tracked by get_stage_busy_counts (that function + its tests stay
# untouched) -- but its jobs live in the SAME saq_jobs table, so the same key-prefix scan works. The
# deterministic key is "match_tracklist_to_discogs:<tracklist_id>" (Phase 35), so the in-flight count
# is the bucket whose key prefix == the function-name constant. Reuses the SAME static
# _STAGE_BUSY_SQL grouped scan (no operator input is interpolated -- the only literals are
# split_part, the status allowlist, and the function-name constant below; T-41-01).
#
# phaze-2akf: the sibling _SEARCH_BUSY_FUNCTION / _SCRAPE_BUSY_FUNCTION gates are GONE with the
# legacy scrape path they gated. They counted saq_jobs rows for two functions that no longer exist,
# so they were structurally pinned at 0 -- a "not busy" signal that could never become busy. The
# drain's own progress surface (GET /pipeline/tracklist-drain-status) replaces them.
_MATCH_BUSY_FUNCTION = "match_tracklist_to_discogs"


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


# Bulk proposal in-flight gate (phaze-8qheu). generate_proposals is a CONTROLLER task whose
# dedup key is a SET hash over the pending file-id snapshot (deterministic_key.py's
# ``_hash_ids``), not a per-member key like ``process_file:<file_id>`` -- so, unlike the agent
# stages and match_tracklist_to_discogs, a re-trigger CANNOT rely on SAQ's key dedup to collapse
# onto an in-flight batch (see the CORRECTION note on :func:`get_proposal_pending_batches`).
# Mirrors :func:`get_match_busy_count`'s shape verbatim (same ``_STAGE_BUSY_SQL`` grouped scan,
# same SAVEPOINT degrade-to-0): a second trigger while proposals are queued/active must be
# REFUSED server-side rather than silently re-proposing the still-pending backlog.
_PROPOSALS_BUSY_FUNCTION = "generate_proposals"


async def get_proposal_busy_count(session: AsyncSession) -> int:
    """Return the in-flight ``generate_proposals`` job count (``queued`` + ``active``), degrade-safe.

    Counts ``saq_jobs`` rows whose deterministic key prefix is ``generate_proposals`` (status
    ``IN ('queued', 'active')``). Both proposal trigger routes (``trigger_proposals`` and
    ``trigger_proposals_ui``) gate on this being 0 before recomputing + enqueuing a fresh batch
    set, closing the set-hash dedup gap phaze-8qheu describes: refusing a second trigger while a
    first is live is strictly safer than letting the two batch snapshots race, because their
    set-hash keys are NOT guaranteed to overlap even though they largely overlap in membership.

    Failure isolation mirrors :func:`get_match_busy_count`: the read runs inside a SAVEPOINT
    (``session.begin_nested()``); on ANY DB error the nested scope is rolled back ALONE and this
    degrades to 0 (never raises) -- a degrade-to-0 false negative just re-opens the same window
    this gate closes, it never blocks the operator's first trigger.
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(_STAGE_BUSY_SQL)).all()
    except Exception:
        logger.warning("proposal_busy_degraded", exc_info=True)
        return 0
    for row in rows:
        if row[0] == _PROPOSALS_BUSY_FUNCTION:
            return int(row[1])
    return 0


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
