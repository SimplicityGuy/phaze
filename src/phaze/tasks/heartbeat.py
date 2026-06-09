"""30-second cron handler that POSTs an agent heartbeat (Phase 29 D-07..D-10).

Reads from SAQ ctx (populated by phaze.tasks.agent_worker.startup):
    - ctx["api_client"]: PhazeAgentClient
    - ctx["agent_identity"]: AgentIdentity
    - ctx["worker"]: SAQ Worker (gives .queue for Queue.info())

Failure policy (D-09): catch AgentApiError, log WARNING, return. SAQ retries
on next tick. Mirrors Phase 28 D-16 fire-and-forget posture.

Cron schedule (D-08 + RESEARCH Critical Discovery #2):
    "* * * * * */30"  -- trailing-seconds 6-field form; croniter 6.x default.
    NOT "*/30 * * * * *" (leading-seconds form fires every second).

IMPORT-BOUNDARY INVARIANT (Phase 26 D-25, extended by Phase 29):
this module MUST NOT transitively import phaze.database,
phaze.tasks.session, or sqlalchemy.ext.asyncio. It runs inside the agent
SAQ worker process which has no Postgres reachability. Enforced by
``tests/test_task_split.py`` (subprocess import-boundary tests).
"""

from __future__ import annotations

import importlib.metadata
import os
from typing import Any

import structlog

from phaze.schemas.agent_heartbeat import HeartbeatRequest
from phaze.services.agent_client import AgentApiError


logger = structlog.get_logger(__name__)


async def heartbeat_tick(ctx: dict[str, Any]) -> None:
    """SAQ cron handler. ctx is the worker context dict from startup hook.

    Builds a HeartbeatRequest from the agent's current state and POSTs it via
    the shared PhazeAgentClient. Defensive against:

    * ctx not yet initialised (worker restart race) -> WARNING, return.
    * queue.info() transient failure -> default queue_depth=0, still POST.
    * AgentApiError (any subclass) -> WARNING, return; SAQ retries next tick.
    """
    client = ctx.get("api_client")
    identity = ctx.get("agent_identity")
    if client is None or identity is None:
        logger.warning("heartbeat_tick: ctx not initialized; skipping")
        return

    # Queue depth from SAQ Queue.info()["queued"] via ctx["worker"].queue.
    # Pitfall 8: ctx["queue"] is NOT a valid key -- SAQ exposes the Queue
    # on the Worker instance only.
    queue = ctx["worker"].queue
    try:
        info = await queue.info()
        queue_depth = int(info.get("queued", 0))
    except Exception:
        # Defensive: any queue error (Redis blip, SAQ internal change, etc.)
        # must NOT crash the cron handler. Default to 0; log + still POST.
        logger.warning("heartbeat_tick: queue.info() failed; defaulting to 0", exc_info=True)
        queue_depth = 0

    payload = HeartbeatRequest(
        agent_version=importlib.metadata.version("phaze"),
        worker_pid=os.getpid(),
        queue_depth=queue_depth,
    )
    try:
        await client.heartbeat(payload)
        # DEBUG only by design (PR3): the 30s cron fires constantly, so an INFO here
        # would flood operational logs -- heartbeat liveness lives at DEBUG.
        logger.debug("heartbeat sent", agent=identity.agent_id, queue_depth=queue_depth)
    except AgentApiError as exc:
        logger.warning("heartbeat failed: %s", exc)
