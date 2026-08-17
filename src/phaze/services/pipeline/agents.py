"""Per-agent and whole-fleet live activity reads -- queue depth, lane depth, recent scans and
the online-agent count.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr). What unites these is
the SUBSTRATE and the degrade posture: they read the live broker through ``app.state`` handles (not
the domain DB), they ride 5s polls, and a missing ``app.state`` attribute -- which is the normal
shape under a lifespan-skipping test client -- must degrade to zero rather than 500 the poll.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
import structlog

from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch
from phaze.services.enqueue_router import LANES


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


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
    ``reap_stuck_aborting_jobs``) never inflate the counts. The scheduled-inclusive kind is never
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


# Recent-scan-batches cap for the agent-activity pane (D-05 / D-00b). A small fixed LIMIT keeps the
# per-poll read bounded -- the agent pane shows the most recent handful, scroll for depth is unnecessary.
_AGENT_RECENT_SCANS_N = 10


async def get_agent_lane_depths(app_state: Any, agent_id: str) -> dict[str, int]:
    """Return the agent's per-lane in-flight depth ``{analyze, meta, io}``, degrade-safe (D-05 / D-00b).

    Sums ``count("queued") + count("active")`` across each of the agent's three
    :data:`phaze.services.enqueue_router.LANES` queues (the same ``all_lane_queues`` seam
    :func:`get_queue_activity` uses), keyed by lane name so the agent pane can render
    ``analyze N · meta N · io N``. The legacy base queue is deliberately EXCLUDED
    here -- this pane shows the live per-lane split, not the migration-drain total.

    Failure isolation mirrors :func:`get_queue_activity`: a missing ``app.state.task_router`` (the test
    client skips the lifespan, so the queue handles are absent) or a broker hiccup degrades the whole
    dict to all-zero; a single dead lane degrades THAT lane to 0 without zeroing the others. It NEVER
    raises into the 5s ``/admin/agents/{id}/_activity`` poll (D-00b).

    phaze-en7s7: connect-before-count (#217), mirrored from :func:`get_queue_activity` --
    SAQ's ``PostgresQueue`` constructs its psycopg pool with ``open=False`` and only
    ``connect()`` opens it; ``main.py``'s lifespan pre-opens pools only for agents present at
    boot, so a runtime-registered agent's lanes raise ``PoolClosed`` on ``count()`` until
    something else connects them first. Without this, every lane's ``PoolClosed`` was silently
    swallowed by the per-lane ``except`` below and rendered as 0 -- a systematic (every-poll),
    not transient, false "idle" read for a runtime-registered agent's activity pane.
    ``connect()`` is idempotent (SAQ guards on ``self._connected``), so this is a no-op once
    something else (the dashboard poll, an API-side enqueue) has already opened the pool.
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
            await q.connect()
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


async def count_active_agents(session: AsyncSession, kind: str | None = None) -> int:
    """Return the number of online agents (``revoked_at IS NULL`` AND ``last_seen_at IS NOT NULL``).

    Counts agents matching :func:`phaze.services.enqueue_router.select_active_agent`'s EXACT
    liveness definition (CONTEXT decision 2 -- do NOT invent a new liveness rule): a revoked agent
    (``revoked_at`` set) and a never-seen agent (``last_seen_at`` None) are both excluded. This drives
    the DAG nodes' "Needs agent" gates -- a per-agent task raises ``NoActiveAgentError`` when no
    agent is online, so those buttons must stay disabled until one is.

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
