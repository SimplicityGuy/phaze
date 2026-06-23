"""Agent liveness heartbeat (Phase 29 D-07..D-10; Phase 46 background-task refactor).

The heartbeat POSTs the agent's current state to the control plane so the
liveness classifier keeps the agent ``alive``. It is built around a single
reusable coroutine ``send_heartbeat(ctx)`` driven by an asyncio background loop
``_heartbeat_loop(ctx)``.

Reads from the SAQ ctx (populated by phaze.tasks.agent_worker.startup):
    - ctx["api_client"]: PhazeAgentClient
    - ctx["agent_identity"]: AgentIdentity
    - ctx["worker"]: SAQ Worker (gives .queue for Queue.info())

Phase 46 — why a background task, not a SAQ CronJob:
    The previous ``heartbeat_tick`` ran as a SAQ ``CronJob`` and so competed for
    the same ``worker_max_jobs`` dispatch slots as multi-hour ``process_file``
    analysis jobs. When all slots were saturated, the heartbeat could not get a
    slot for ~50 min, blowing past the 300s DEAD threshold and marking a healthy
    busy agent DEAD. ``process_file`` runs essentia in a pebble ProcessPool
    (Phase 43), so the event loop is free — a plain asyncio background task ticks
    reliably regardless of dispatch saturation. ``send_heartbeat`` is the single
    implementation; ``_heartbeat_loop`` calls it every
    ``AGENT_HEARTBEAT_INTERVAL_SECONDS``; ``heartbeat_tick`` remains a thin shim.

Failure policy (D-09): catch AgentApiError, log WARNING, return. The loop fires
again on the next tick. Mirrors Phase 28 D-16 fire-and-forget posture.

IMPORT-BOUNDARY INVARIANT (Phase 26 D-25, extended by Phase 29):
this module MUST NOT transitively import phaze.database,
phaze.tasks.session, or sqlalchemy.ext.asyncio. It runs inside the agent
SAQ worker process which has no Postgres reachability. Enforced by
``tests/test_task_split.py`` (subprocess import-boundary tests).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
from typing import Any

import structlog

from phaze.constants import AGENT_HEARTBEAT_INTERVAL_SECONDS
from phaze.schemas.agent_heartbeat import HeartbeatRequest
from phaze.services.agent_client import AgentApiError


logger = structlog.get_logger(__name__)


async def send_heartbeat(ctx: dict[str, Any]) -> None:
    """POST one agent heartbeat from the current worker state.

    Builds a HeartbeatRequest and POSTs it via the shared PhazeAgentClient.
    Defensive against:

    * ctx not yet initialised (worker restart race) -> WARNING, return.
    * ctx["worker"] absent OR queue.info() transient failure -> default
      queue_depth=0, still POST.
    * AgentApiError (any subclass) -> WARNING, return; the loop retries next tick.
    """
    client = ctx.get("api_client")
    identity = ctx.get("agent_identity")
    if client is None or identity is None:
        logger.warning("heartbeat_tick: ctx not initialized; skipping")
        return

    # Queue depth from SAQ Queue.info()["queued"] via ctx["worker"].queue.
    # Pitfall 8: ctx["queue"] is NOT a valid key -- SAQ exposes the Queue on the
    # Worker instance only. The Worker may not be attached yet when the loop first
    # ticks, so read ctx["worker"] lazily INSIDE the try and degrade to 0.
    try:
        queue = ctx["worker"].queue
        info = await queue.info()
        queue_depth = int(info.get("queued", 0))
    except Exception:
        # Defensive: any queue error (worker not attached, SAQ internal change,
        # broker blip) must NOT crash the heartbeat. Default to 0; log + still POST.
        logger.warning("heartbeat_tick: queue.info() failed; defaulting to 0", exc_info=True)
        queue_depth = 0

    payload = HeartbeatRequest(
        agent_version=importlib.metadata.version("phaze"),
        worker_pid=os.getpid(),
        queue_depth=queue_depth,
    )
    try:
        await client.heartbeat(payload)
        # DEBUG only by design (PR3): the 30s cadence fires constantly, so an INFO
        # here would flood operational logs -- heartbeat liveness lives at DEBUG.
        logger.debug("heartbeat sent", agent=identity.agent_id, queue_depth=queue_depth)
    except AgentApiError as exc:
        logger.warning("heartbeat failed: %s", exc)


async def _heartbeat_loop(ctx: dict[str, Any]) -> None:
    """Background loop: POST a heartbeat every ``AGENT_HEARTBEAT_INTERVAL_SECONDS``.

    Phase 46: launched as an asyncio task in the agent worker startup hook so the
    heartbeat runs OUTSIDE the SAQ dispatch pool and cannot be starved by saturated
    ``worker_max_jobs`` slots. Each iteration is wrapped so a single failed beat
    never kills the loop (a dead loop = a silently DEAD agent);
    ``asyncio.CancelledError`` is re-raised so shutdown can cancel + await cleanly.
    """
    while True:
        try:
            await send_heartbeat(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad iteration must not kill the loop -- log and keep ticking.
            logger.warning("heartbeat loop iteration failed; continuing", exc_info=True)
        await asyncio.sleep(AGENT_HEARTBEAT_INTERVAL_SECONDS)


async def heartbeat_tick(ctx: dict[str, Any]) -> None:
    """Back-compat shim: a directly-callable wrapper delegating to send_heartbeat.

    Retained for the documented public surface and the existing direct-call tests;
    it is no longer registered as a SAQ function or CronJob (Phase 46).
    """
    await send_heartbeat(ctx)
