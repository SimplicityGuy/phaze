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
    busy agent DEAD. ``process_file`` runs essentia in a dedicated child process
    (Phase 101's exec'd-child model, ``services.analysis_exec``; formerly a pebble
    ProcessPool prior to Phase 101), so the event loop is free — a plain asyncio
    background task ticks reliably regardless of dispatch saturation. ``send_heartbeat`` is the single
    implementation; ``_heartbeat_loop`` calls it every
    ``AGENT_HEARTBEAT_INTERVAL_SECONDS``; ``heartbeat_tick`` remains a thin shim.

Failure policy (D-09): catch AgentApiError, log WARNING, return. The loop fires
again on the next tick. Mirrors Phase 28 D-16 fire-and-forget posture.

IMPORT-BOUNDARY INVARIANT (Phase 26 D-25, extended by Phase 29):
this module MUST NOT transitively import phaze.database,
phaze.tasks.session, or sqlalchemy.ext.asyncio. It runs inside the agent
SAQ worker process which has no Postgres reachability. Enforced by
``tests/shared/core/test_task_split.py`` (subprocess import-boundary tests).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import signal
from typing import Any

import structlog

from phaze.constants import (
    AGENT_BROKER_EXIT_BEATS,
    AGENT_BROKER_UNHEALTHY_BEATS,
    AGENT_HEARTBEAT_INTERVAL_SECONDS,
)
from phaze.schemas.agent_heartbeat import HeartbeatRequest
from phaze.services.agent_client import AgentApiError


logger = structlog.get_logger(__name__)


BEAT_TIMEOUT_SECONDS = float(AGENT_HEARTBEAT_INTERVAL_SECONDS)
"""Hard deadline on ONE beat (phaze-kaf2).

Deliberately a SEPARATE constant from ``AGENT_HEARTBEAT_INTERVAL_SECONDS`` rather than
reusing it inline, even though it takes the same default. Cadence ("how often do we
beat") and deadline ("how long may one beat take") are different concerns: tuning the
cadence -- or patching it to 0 to make a test fast -- must never silently disable the
hang protection this bead exists to add.
"""

QUEUE_INFO_TIMEOUT_SECONDS = 5.0
"""Hard deadline on ``Queue.info()`` (phaze-kaf2).

The broker read is a NICE-TO-HAVE enrichment of the heartbeat, not its purpose. A
psycopg pool acquire that never returns must degrade ``queue_depth`` to 0, never delay
the POST that keeps the agent alive. Kept far below
``AGENT_HEARTBEAT_INTERVAL_SECONDS`` so a slow broker cannot eat the whole tick.
"""

HEARTBEAT_INFO_LOG_EVERY = 20
"""Emit one INFO every Nth beat (phaze-kaf2).

Success was DEBUG-only, so a frozen loop and a healthy loop produced byte-identical
logs (nothing) -- during the 2026-07-18 nox incident there was no way to tell them
apart. At the 30s cadence this is ~1 line / 10 min: readable, not a flood.
"""


def _terminate_worker_process() -> None:
    """Ask the current process to shut down loudly (phaze-xuec1).

    SIGTERM -- not ``os._exit`` -- so the SAQ worker's own signal handler
    (``saq.worker.Worker.start``, ``loop.add_signal_handler``) drives the EXISTING
    graceful-shutdown path: ``phaze.tasks.agent_worker.shutdown`` still runs (closing the
    api client and cache-redis handle) rather than a hard kill skipping it, and the
    container's restart policy gets a clean non-zero-exit process to replace. A dedicated,
    patchable function so tests can assert the call without terminating the test process.
    """
    os.kill(os.getpid(), signal.SIGTERM)  # pragma: no cover -- exercised via the patched form in tests


async def send_heartbeat(ctx: dict[str, Any]) -> None:
    """POST one agent heartbeat from the current worker state.

    Builds a HeartbeatRequest and POSTs it via the shared PhazeAgentClient.
    Defensive against:

    * ctx not yet initialised (worker restart race) -> WARNING, return.
    * ctx["worker"] absent (startup race, not a broker signal) -> default
      queue_depth=0, still POST.
    * queue.info() transient failure -> default queue_depth=0, still POST --
      UNLESS it has now failed ``AGENT_BROKER_UNHEALTHY_BEATS`` times in a row
      (phaze-xuec1): see below.
    * AgentApiError (any subclass) -> WARNING, return; the loop retries next tick.

    phaze-xuec1 -- liveness must reflect the ability to CONSUME the queue, not just that
    the process can reach the control-plane API. The 2026-08-08 nox incident: the
    heartbeat POST is independent HTTP traffic and kept succeeding every 30s for 1h41m
    while the worker's psycopg3 broker pool was unusable and it dequeued nothing.
    ``queue.info()`` exercises that SAME pool ``_dequeue()`` needs, so this function now
    tracks consecutive probe failures in ``ctx["_broker_probe_failures"]`` (reset to 0 on
    any success) and, once the count reaches ``AGENT_BROKER_UNHEALTHY_BEATS``, WITHHOLDS
    the POST entirely instead of reporting the agent alive -- ``last_seen_at`` goes stale
    and the EXISTING ``phaze.services.agent_liveness.classify`` thresholds degrade the
    agent through stale -> dead exactly as if the process had died. If the pool never
    recovers, ``AGENT_BROKER_EXIT_BEATS`` later the worker terminates itself loudly
    (SIGTERM) so the container's restart policy can replace it, rather than sitting
    wedged until an operator notices and runs ``docker restart`` by hand.
    """
    client = ctx.get("api_client")
    identity = ctx.get("agent_identity")
    if client is None or identity is None:
        logger.warning("heartbeat_tick: ctx not initialized; skipping")
        return

    # Queue depth from SAQ Queue.info()["queued"] via ctx["worker"].queue.
    # Pitfall 8: ctx["queue"] is NOT a valid key -- SAQ exposes the Queue on the
    # Worker instance only. The Worker may not be attached yet when the loop first
    # ticks; that is a startup race, not evidence the broker is unreachable, so it is
    # handled separately from a probe that actually ran and failed (phaze-xuec1).
    worker = ctx.get("worker")
    if worker is None:
        queue_depth = 0
    else:
        try:
            # phaze-kaf2: `queue.info()` is UNBOUNDED without this deadline. A hung psycopg
            # pool acquire/query blocks here forever; the except-Exception below only fires
            # on a RAISE, never on a hang, so the beat silently never completes and the agent
            # is classified DEAD while still processing jobs.
            info = await asyncio.wait_for(worker.queue.info(), timeout=QUEUE_INFO_TIMEOUT_SECONDS)
            queue_depth = int(info.get("queued", 0))
            ctx["_broker_probe_failures"] = 0
        except TimeoutError:
            # Distinct from the generic failure below: a HANG is the phaze-kaf2 signature and
            # an operator needs to see it named, not folded into "queue.info() failed".
            logger.warning("heartbeat: queue.info() timed out after %.1fs; defaulting depth to 0", QUEUE_INFO_TIMEOUT_SECONDS)
            queue_depth = 0
            ctx["_broker_probe_failures"] = ctx.get("_broker_probe_failures", 0) + 1
        except Exception:
            # Defensive: any queue error (SAQ internal change, broker blip) must NOT crash
            # the heartbeat. Default to 0; log + still POST -- unless sustained, see below.
            logger.warning("heartbeat_tick: queue.info() failed; defaulting to 0", exc_info=True)
            queue_depth = 0
            ctx["_broker_probe_failures"] = ctx.get("_broker_probe_failures", 0) + 1

    broker_probe_failures = ctx.get("_broker_probe_failures", 0)
    if broker_probe_failures >= AGENT_BROKER_UNHEALTHY_BEATS:
        logger.error(
            "heartbeat: broker unreachable for %s consecutive probes (~%ss); worker cannot "
            "verify it can dequeue -- withholding this beat so liveness degrades instead of "
            "reporting alive",
            broker_probe_failures,
            broker_probe_failures * AGENT_HEARTBEAT_INTERVAL_SECONDS,
        )
        if broker_probe_failures >= AGENT_BROKER_EXIT_BEATS:
            logger.error(
                "heartbeat: broker unreachable for %s consecutive probes; exiting so the container restart policy can recover a fresh worker",
                broker_probe_failures,
            )
            _terminate_worker_process()
        return

    payload = HeartbeatRequest(
        agent_version=importlib.metadata.version("phaze"),
        worker_pid=os.getpid(),
        queue_depth=queue_depth,
        # phaze-30fo: tag the beat with THIS worker's lane so the control plane can keep a
        # per-lane breakdown and sum an honest all-lane depth. None in all-mode (no split).
        lane=ctx.get("agent_lane"),
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

    phaze-kaf2 -- the try/except above catches RAISES, not HANGS. An ``await`` that never
    returns is not an exception, so before the per-iteration deadline below a single
    wedged await froze liveness permanently with zero log evidence while the worker kept
    processing jobs. ``asyncio.wait_for`` bounds every beat: a hung one is cancelled,
    named in the log, and the NEXT tick still fires.
    """
    beats = 0
    while True:
        try:
            await asyncio.wait_for(send_heartbeat(ctx), timeout=BEAT_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # The phaze-kaf2 failure mode, now survivable and visible. WARNING (not DEBUG):
            # silence is exactly what made the original incident unreadable.
            logger.warning("heartbeat timed out after %ss; cancelled, continuing", BEAT_TIMEOUT_SECONDS)
        except Exception:
            # One bad iteration must not kill the loop -- log and keep ticking.
            logger.warning("heartbeat loop iteration failed; continuing", exc_info=True)

        beats += 1
        if beats % HEARTBEAT_INFO_LOG_EVERY == 0:
            # Proof-of-life an operator can grep. Distinguishes "loop alive" from
            # "loop dead", which DEBUG-only success logging could not.
            logger.info("heartbeat loop alive", beats=beats, interval_seconds=AGENT_HEARTBEAT_INTERVAL_SECONDS)

        await asyncio.sleep(AGENT_HEARTBEAT_INTERVAL_SECONDS)


async def heartbeat_tick(ctx: dict[str, Any]) -> None:
    """Back-compat shim: a directly-callable wrapper delegating to send_heartbeat.

    Retained for the documented public surface and the existing direct-call tests;
    it is no longer registered as a SAQ function or CronJob (Phase 46).
    """
    await send_heartbeat(ctx)
