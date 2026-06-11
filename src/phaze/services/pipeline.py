"""Pipeline orchestration service -- stage counts and file queries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
import structlog

from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


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
