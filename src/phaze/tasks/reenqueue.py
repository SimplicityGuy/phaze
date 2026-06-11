"""Control-side reboot/queue-loss recovery: re-enqueue DISCOVERED files for analysis.

CONTROL-ONLY (Phase 26 D-03 / control-vs-agent DB boundary). This task needs both
PostgreSQL via ``ctx["async_session"]`` AND the per-agent enqueuer via
``ctx["task_router"]`` -- wired in ``phaze.tasks.controller.startup``. The agent
worker is deliberately Postgres-free (the import-boundary test
``tests/test_task_split.py`` enforces this), so this module MUST NEVER be imported
or registered by ``phaze.tasks.agent_worker`` or anything under
``phaze.tasks._shared``. Register it ONLY in ``phaze.tasks.controller``.

After a reboot or a Redis flush, files stamped ``FileState.DISCOVERED`` are stranded:
their ``process_file`` jobs were lost from the queue but Postgres still records them as
discovered (``process_file`` does not advance a file out of DISCOVERED until a worker
finishes it). This task re-enqueues ``process_file`` for every DISCOVERED file onto the
ACTIVE agent's per-agent queue through the Wave-1 shared helper
(``phaze.services.analysis_enqueue.enqueue_process_file``), so the key / payload / policy
match the dashboard "Run Analysis" path EXACTLY. The shared deterministic key
``process_file:<file_id>`` makes any file already in flight dedup to a clean no-op
(counted as ``skipped``), so the task is safe to run on every boot AND every cron tick
(32-CONTEXT "Trigger" + "Scope"; 32-RESEARCH §Q2/§Q3/§Pattern 2).

Routing: it picks the agent via ``select_active_agent`` and enqueues onto
``ctx["task_router"].queue_for(agent.id)`` -- NEVER the consumer-less controller queue
that ctx also carries (32-RESEARCH Pitfall 1). Zero live agents (common right after a cold
reboot; 32-RESEARCH Pitfall 3) logs a warning and returns a zero count instead of
raising. It reuses the cached ``task_router`` from ctx rather than constructing a new
``AgentTaskRouter`` per call (32-RESEARCH Pitfall 4).
"""

from __future__ import annotations

from typing import Any

import structlog

from phaze.config import get_settings
from phaze.models.file import FileState
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.services.pipeline import get_files_by_state


logger = structlog.get_logger(__name__)


async def reenqueue_discovered(ctx: dict[str, Any]) -> dict[str, int]:
    """Re-enqueue ``process_file`` for every DISCOVERED file onto the active agent's queue.

    Queries Postgres for ``FileState.DISCOVERED`` rows and funnels each through the
    Wave-1 shared ``enqueue_process_file`` helper (complete 5-field payload + deterministic
    ``process_file:<file_id>`` key + ``timeout=14400`` / ``retries=2``). A file whose key is
    still in flight dedups to a no-op (``enqueue_process_file`` returns ``None``) and is
    counted as ``skipped``; the rest are ``reenqueued``.

    Returns ``{"reenqueued": N, "skipped": M}``. Degrades gracefully:

    - No DISCOVERED files -> ``{"reenqueued": 0, "skipped": 0}`` without selecting an agent.
    - No active agent (``NoActiveAgentError``) -> logs a WARNING and returns zeros; never raises.

    One-time post-deploy note (32-RESEARCH Open Question 1): files enqueued before the
    deterministic-key cutover carry a random uuid key, so a single overlapping re-run may
    transiently double-enqueue them. This is harmless -- ``process_file`` is idempotent
    per file, so the redundant run simply re-analyzes and converges.
    """
    cfg = get_settings()

    async with ctx["async_session"]() as session:
        files = await get_files_by_state(session, FileState.DISCOVERED)
        if not files:
            return {"reenqueued": 0, "skipped": 0}

        try:
            agent = await select_active_agent(session)
        except NoActiveAgentError:
            logger.warning("reenqueue skipped: no active agent", discovered=len(files))
            return {"reenqueued": 0, "skipped": 0}

        queue = ctx["task_router"].queue_for(agent.id)

        reenqueued = 0
        skipped = 0
        for file in files:
            job = await enqueue_process_file(queue, file, agent.id, cfg.models_path)
            if job is None:
                skipped += 1
            else:
                reenqueued += 1

    logger.info(
        "reenqueue complete",
        agent=agent.id,
        queue=f"phaze-agent-{agent.id}",
        discovered=len(files),
        reenqueued=reenqueued,
        skipped=skipped,
    )
    return {"reenqueued": reenqueued, "skipped": skipped}
