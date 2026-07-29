"""Control-side SAQ cron: retro-heal stage backlog rows stranded ``SENTINEL``-parked (phaze-uqyn).

CONTROL-ONLY (Phase 26 D-03 / control-vs-agent DB boundary). Like :mod:`phaze.tasks.aborting_reaper`
this needs PostgreSQL via ``ctx["async_session"]`` and MUST NEVER be imported or registered by
``phaze.tasks.agent_worker`` or anything under ``phaze.tasks._shared`` (the agent path is
deliberately Postgres-free -- ``tests/shared/core/test_task_split.py`` enforces this).

WHY THIS EXISTS
---------------
``resume_stage`` (``phaze.services.stage_control``) is a single, one-shot ``UPDATE saq_jobs``
that un-parks every row CURRENTLY ``scheduled = SENTINEL`` for a stage. It commits atomically
with the durable ``pipeline_stage_control.paused = false`` flip, but it has no way to reach
into every OTHER process's in-memory pause cache. Both park writers --
``apply_stage_control`` (the before-enqueue hook, runs in the API/controller process for a
manual-trigger enqueue) and the ``enforce_stage_pause_on_process`` / ``repark_if_stage_paused``
worker-side pair -- read ``paused`` through a per-process 5-second TTL cache
(``phaze.tasks._shared.stage_control._read_stage_control``) that is dropped only on window
expiry, never on a control change. A stage job enqueued or dequeued in ANY process whose cache
still holds ``paused=True`` within that up-to-5s window AFTER the resume commit is parked at
``SENTINEL`` -- after the one-shot un-park already ran -- and nothing else in that process's
lifetime un-parks it again. The consequences compound: the row stays ``status='queued'``, so
:func:`phaze.services.pipeline.get_live_job_keys` classifies it as live and
:func:`phaze.tasks.reenqueue.recover_orphaned_work` permanently excludes it from replay;
:func:`phaze.services.pipeline.get_stage_busy_counts` counts it as busy, so the stage's DAG
trigger button stays disabled; and its ``scheduling_ledger`` row keeps the file ``in_flight``
forever (D-01), so the file never re-enters any pending set. The only remedy without this sweep
is an undiscoverable second pause+resume cycle.

The API-process pause/resume endpoints ALSO call
:func:`phaze.tasks._shared.stage_control.clear_stage_control_cache` right after their commit,
closing the SAME-process window immediately. That leaves every OTHER process (a worker, the
fingerprint-requeue CLI, ...) still relying on its own cache expiring naturally -- this sweep is
the cross-process, retro-active backstop: it does not need to know which process stamped a
stray SENTINEL row, only that the row's *stage* durably reads unpaused right now.

WHAT IT DOES
------------
For every stage whose durable ``pipeline_stage_control`` row currently reads ``paused=false``
(read FRESH via the ORM -- never the TTL cache), re-runs the SAME sentinel-guarded
:func:`phaze.services.stage_control.resume_stage` un-park used by the resume endpoint. A stage
that is genuinely paused is skipped entirely -- its parked backlog is left untouched. Because
the un-park is sentinel-guarded (``scheduled = SENTINEL`` in the WHERE clause) it can never
disturb a genuine retry backoff (``scheduled = now + delay``), and re-running it against an
already-healthy backlog matches zero rows -- a no-op, safe to run every tick indefinitely.

Degrade-safe: each stage's read+un-park runs inside its own try/except; one stage's failure
(a pre-migration/missing control row, a transient DB hiccup) is logged and skipped rather than
aborting the sweep for the OTHER stages, and never aborts a controller cron tick.
"""

from __future__ import annotations

from typing import Any

import structlog

from phaze.models.pipeline_stage_control import PipelineStageControl
from phaze.services.stage_control import resume_stage
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


logger = structlog.get_logger(__name__)


async def reconcile_stale_stage_parks(ctx: dict[str, Any]) -> dict[str, int]:
    """Re-run the sentinel-guarded un-park for every currently-unpaused stage (phaze-uqyn).

    Returns ``{<stage>: rows_unparked}`` for every stage this tick attempted (a stage skipped
    because it reads paused, or because its own read/un-park failed, is simply absent from the
    mapping). A non-zero count for a stage names an actual retro-heal: rows that a stale
    per-process pause cache re-parked after the last real resume ran.
    """
    healed: dict[str, int] = {}
    async with ctx["async_session"]() as session:
        for stage in STAGE_TO_FUNCTION:
            try:
                # A SAVEPOINT per stage (mirrors backfill_ledger_from_saq_jobs / aborting_reaper):
                # one stage's failure rolls back ONLY its own nested scope, so a pre-migration or
                # transiently-broken stage never poisons the whole session for the OTHER stages.
                async with session.begin_nested():
                    row = await session.get(PipelineStageControl, stage)
                    if row is None or row.paused:
                        continue
                    count = await resume_stage(session, stage)
            except Exception:
                logger.warning("stage_park_reconcile_degraded: read/un-park failed for stage", stage=stage, exc_info=True)
                continue
            if count:
                healed[stage] = count
        await session.commit()

    if healed:
        # Loud: a non-zero count here means a job was silently wedged at SENTINEL by the exact
        # TOCTOU this sweep exists to close -- worth an operator's attention, not just a debug line.
        logger.warning("stage_park_reconcile: retro-healed stale SENTINEL-parked rows", healed=healed)

    return healed
