"""SAQ task: associate_agent_companions -- one coalesced, automatic association run for one agent (phaze-spd83).

Requested by the api after every event that can make or change a companion's links, coalesced per
agent into one job per window, never run on a request path: ``services/companion_autolink.py`` holds
the triggers, the coalescing and the reasons. This module is the controller-side entry point:

1. an agent with a RUNNING scan is DEFERRED -- the run is re-requested for a later window and this
   one returns; the scan's terminal PATCH requests the run that counts;
2. a run whose agent lock is held by another run is deferred the same way;
3. otherwise the agent's links are re-derived and its junk-review queue refreshed, with every page
   committed on its own (idempotent: a second run writes nothing).

Registered in ``tasks.controller.settings["functions"]`` and in ``enqueue_router.CONTROLLER_TASKS``;
keyed per ``(agent_id, window)`` in ``tasks._shared.deterministic_key``.
"""

from __future__ import annotations

from typing import Any

import structlog

from phaze.services.companion_autolink import agent_association_lock, enqueue_association, run_agent_association, scan_running


logger = structlog.get_logger(__name__)


async def _defer(ctx: dict[str, Any], agent_id: str, window: int, reason: str) -> dict[str, Any]:
    """Re-request the run for a later window and report why this one did nothing."""
    requeued = await enqueue_association(ctx["queue"], agent_id)
    logger.info("companion association deferred", agent_id=agent_id, window=window, reason=reason, requeued=requeued)
    return {"agent_id": agent_id, "window": window, "status": "deferred", "reason": reason, "requeued": requeued}


async def associate_agent_companions(ctx: dict[str, Any], *, agent_id: str, window: int) -> dict[str, Any]:
    """Re-derive ``agent_id``'s companion links (or defer, see the module docstring); returns a JSON-safe tally.

    Args:
        ctx: SAQ context; ``ctx["async_session"]`` opens the session the run uses and
            ``ctx["queue"]`` (the controller queue) takes a deferral's re-request.
        agent_id: the agent whose companions are decided; every read and write stays on it.
        window: the coalescing window the request named. It is part of the job's key only.
    """
    async with ctx["async_session"]() as session:
        if await scan_running(session, agent_id):
            return await _defer(ctx, agent_id, window, "scan_running")
        async with agent_association_lock(session, agent_id) as acquired:
            if not acquired:
                return await _defer(ctx, agent_id, window, "agent_locked")
            association, detection = await run_agent_association(session, agent_id, apply=True)
    result: dict[str, Any] = {
        "agent_id": agent_id,
        "window": window,
        "status": "associated",
        "links_created": association.links_created,
        "links_removed": association.links_removed,
        "links_kept": association.links_kept,
        "awaiting_features": association.awaiting_features,
        "decided": {str(step): count for step, count in sorted(association.decided.items())},
        "junk_review_created": sum(detection.created.values()) if detection else 0,
        "junk_review_withdrawn": detection.withdrawn if detection else 0,
    }
    logger.info("companion association complete", **result)
    return result
