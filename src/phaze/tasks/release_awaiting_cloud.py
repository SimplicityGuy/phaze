"""Control-side held-file release: drain AWAITING_CLOUD -> the compute queue (Phase 49).

NARROW, RECOVERY-ONLY automation (D-03/D-03a, CLOUDROUTE-02). This is the drain half of the
duration-routing lifecycle: Plan 02's per-file router HOLDS a long file in
``FileState.AWAITING_CLOUD`` (enqueuing NOTHING, D-02) when no compute agent is online; this
producer releases the held set once a compute agent comes online -- automatically, within ~5 min,
via a SINGLE narrow ``CronJob(release_awaiting_cloud, "*/5 * * * *")`` registered on the controller.

THIS IS NOT THE DELETED reenqueue AUTO-ADVANCE CRON. Phase 42 (PR #132) removed the every-5-min
``reenqueue_discovered`` general pipeline auto-advance, and ``controller.py`` carries a "DO NOT
re-add a recover cron" comment for THAT general-advance behavior. This cron is scoped ONLY to the
``AWAITING_CLOUD -> compute`` transition -- it advances no other stage, so it respects the Phase-42
"automation only in recovery" principle. It is ALSO NOT a ledger replay: held files were never
enqueued (D-02), so they carry NO scheduling-ledger row and ``recover_orphaned_work``'s
ledger-driven replay structurally cannot see them. Release MUST therefore be a STATE-driven scan.

CONTROL-ONLY: needs both PostgreSQL (``ctx["async_session"]``) and the per-agent enqueuer
(``ctx["task_router"]``), exactly like ``recover_orphaned_work``. Register ONLY in
``phaze.tasks.controller`` -- never the agent worker (``tests/test_task_split.py`` enforces the
agent role stays Postgres-free). FastAPI-free: imports neither ``fastapi`` nor ``phaze.routers``,
mirroring the ``recover_orphaned_work`` import discipline.
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


async def release_awaiting_cloud(ctx: dict[str, Any]) -> dict[str, int]:
    """Release every held ``AWAITING_CLOUD`` file to the compute queue (gated on a compute agent).

    Flow:

    1. SCAN: ``held = get_files_by_state(session, FileState.AWAITING_CLOUD)``. Empty -> zero no-op.
    2. GATE: resolve a compute agent via ``select_active_agent(session, kind="compute")``. When no
       compute agent is online (``NoActiveAgentError``) this is a clean no-op (D-02): nothing
       enqueued, nothing raised, no state change -- the held files stay AWAITING_CLOUD for a later
       tick. Held files are NEVER routed to a local/fileserver agent.
    3. RELEASE: for each held file, enqueue ``process_file`` onto the compute agent's per-agent queue
       via the shared ``enqueue_process_file`` producer (deterministic key ``process_file:<id>``;
       the ``before_enqueue`` hook writes the ledger row, and an already-live key dedups to ``None``
       -> counted as skipped) AND reset the file to ``FileState.DISCOVERED`` (D-03a: the reset applies
       even on a dedup so the file leaves the scanned set and the dashboard held-count stays honest).
       Commit once.

    Returns ``{"released": N, "skipped": M}`` where ``released`` counts jobs actually enqueued and
    ``skipped`` counts deterministic-key dedup no-ops. Both an empty held set and a missing compute
    agent return ``{"released": 0, "skipped": 0}``.
    """
    models_path = get_settings().models_path

    async with ctx["async_session"]() as session:
        held = await get_files_by_state(session, FileState.AWAITING_CLOUD)
        if not held:
            return {"released": 0, "skipped": 0}

        try:
            agent = await select_active_agent(session, kind="compute")
        except NoActiveAgentError:
            logger.info("release_awaiting_cloud no-op: no compute agent online", held=len(held))
            return {"released": 0, "skipped": 0}

        compute_queue = ctx["task_router"].queue_for(agent.id)
        tally = {"released": 0, "skipped": 0}
        for file in held:
            job = await enqueue_process_file(compute_queue, file, agent.id, models_path)
            if job is None:
                tally["skipped"] += 1
            else:
                tally["released"] += 1
            # D-03a: reset regardless of the dedup outcome so the file leaves the AWAITING_CLOUD scan
            # set and the dashboard held-count stays honest.
            file.state = FileState.DISCOVERED
        await session.commit()

    logger.info("release_awaiting_cloud complete", agent_id=agent.id, released=tally["released"], skipped=tally["skipped"])
    return tally
