"""The continuous-drain CronJob + its paired ``after_process`` hook (phaze-6nrrf).

WHAT THIS MODULE ADDS, AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------------------
``tasks.tracklist_drain.drain_tracklists`` is UNCHANGED: it still dispenses exactly one bounded
(<=100 lookup) slice per enqueue, and it still has no CronJob of its OWN -- the epic's ethics
bound (residential IP, headful browser, a shared host's published crawl-delay budget) means
nothing may start crawling on container boot. What this module adds is a SEPARATE, narrowly-scoped
CronJob that only ever CONTINUES a pass the operator has already, explicitly consented to via the
durable :class:`~phaze.models.tracklist_drain_arm_state.TracklistDrainArmState` row (armed via the
tracklist workspace's Arm button, ``routers.pipeline.arm_tracklist_drain_ui``).

This is the same shape ``tasks.controller`` warns against elsewhere -- see its ``cron_jobs`` list
comment: "DO NOT re-add a general auto-advance cron here" -- but it is not that pattern. Every
other forbidden auto-advance cron would pick up NEW work on its own; this one is structurally
incapable of that. It:

* never sets ``armed`` (only ``services.tracklist_drain_arm.arm_drain``, called from the operator's
  click, does that) -- a cold boot or a redeploy always reads whatever the row already durably
  says, DEFAULT ``false`` from migration 059;
* auto-DISARMS itself the moment there is nothing left to do (queue empty) or the drain looks
  broken (3 consecutive slice failures) -- it narrows the armed window, it never widens it;
* is a no-op on every tick while disarmed, which is the steady state for every deploy that has
  never used this feature.

THE TWO HALVES
---------------
1. :func:`continue_armed_tracklist_drain` -- the CronJob body (``* * * * *``, matching this
   controller's other reapers). Reads the arm state; if armed, not already running a slice
   (``in_flight``), and past the cooldown (``ControlSettings.tracklist_drain_cooldown_sec``),
   enqueues the next ``drain_tracklists`` slice. Checks the queue depth FIRST via
   ``tasks.tracklist_drain.tracklist_drain_status`` (spends no host request, per that function's
   own docstring) so a fully-drained queue auto-disarms instead of enqueueing an empty slice.
2. :func:`record_drain_slice_completion` -- an ``after_process`` Worker hook (registered
   alongside ``deterministic_key.increment_completed`` in ``tasks.controller.settings``) that
   clears ``in_flight`` and applies the cooldown + failure-streak rule when a CRON-ENQUEUED slice
   reaches a terminal SAQ status. Gated on ``in_flight`` being true, NOT on ``job.function``
   alone: a manual "Run tracklist lookups" / Prioritize / Refresh slice also runs
   ``drain_tracklists`` but is not part of an armed pass, so it must not perturb the cooldown
   clock or the failure streak (see that function's own docstring).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from saq.job import TERMINAL_STATUSES, Status
import structlog

from phaze.config import settings as cfg
from phaze.services.tracklist_drain_arm import (
    clear_stale_in_flight,
    disarm_drain,
    get_arm_state,
    mark_slice_enqueued,
    mark_slice_finished,
)
from phaze.tasks.tracklist_drain import tracklist_drain_status


if TYPE_CHECKING:
    from saq import Job


logger = structlog.get_logger(__name__)

# phaze-6nrrf, operator decision (2026-08-11): auto-disarm after 3 consecutive CRON-ENQUEUED slice
# failures -- the LABEL the operator selected, over "1 failure -- disarm immediately" and "5
# consecutive failures" (session 4b27bb6e, asked 16:13:19Z, answered 16:15:16Z; recovered via
# phaze-d2hgv.4's provenance tool -- the exchange never names this bead, so a --bead search misses
# it and only a --since/--until date-range search finds it). A successful slice
# (services.tracklist_drain_arm.mark_slice_finished) resets this to 0.
MAX_CONSECUTIVE_SLICE_FAILURES = 3

# Extra headroom (seconds) added on top of the role's worst-case retry envelope
# (worker_job_timeout * (worker_max_retries + 1)) before an ``in_flight`` slice is treated as
# abandoned by clear_stale_in_flight below -- covers scheduling/backoff overhead between retries,
# not just the raw per-attempt timeouts.
_STALE_IN_FLIGHT_BUFFER_SECONDS = 300


async def continue_armed_tracklist_drain(ctx: dict[str, Any]) -> None:
    """CronJob body: enqueue the drain's next slice, but ONLY while armed and eligible.

    Every early return below is an ordinary steady state, not an error: disarmed (the common
    case), a slice already running, or still inside the cooldown window all just mean "nothing to
    do this tick" -- the next tick, a minute later, re-evaluates from the durable row with no
    state lost.
    """
    moment = datetime.now(UTC)

    async with ctx["async_session"]() as session:
        state = await get_arm_state(session)
        armed = state.armed
        in_flight = state.in_flight
        slice_enqueued_at = state.slice_enqueued_at
        next_eligible_at = state.next_eligible_at
        await session.commit()  # persists a defensive get-or-create insert, if one just happened

    if not armed:
        return

    if in_flight:
        stale_after_seconds = cfg.worker_job_timeout * (cfg.worker_max_retries + 1) + _STALE_IN_FLIGHT_BUFFER_SECONDS
        if slice_enqueued_at is None or (moment - slice_enqueued_at).total_seconds() < stale_after_seconds:
            # An ordinary in-progress slice -- record_drain_slice_completion will clear this.
            return
        logger.warning(
            "continuous tracklist drain: in-flight slice looks abandoned (worker process killed "
            "before after_process could run) -- self-healing so the armed pass is not wedged",
            slice_enqueued_at=slice_enqueued_at.isoformat(),
            stale_after_seconds=stale_after_seconds,
        )
        async with ctx["async_session"]() as session:
            await clear_stale_in_flight(session, cooldown_seconds=cfg.tracklist_drain_cooldown_sec, now=moment)
            await session.commit()
        return

    if next_eligible_at is not None and moment < next_eligible_at:
        return

    # Spends NO host request (tracklist_drain_status's own contract) -- checked before enqueueing
    # so a fully-drained queue auto-disarms instead of paying for an empty slice.
    status = await tracklist_drain_status(ctx)
    if status["queued"] == 0:
        async with ctx["async_session"]() as session:
            await disarm_drain(session, reason="queue_empty", now=moment)
            await session.commit()
        logger.info("continuous tracklist drain: auto-disarmed, the queue is empty")
        return

    async with ctx["async_session"]() as session:
        await mark_slice_enqueued(session, now=moment)
        await session.commit()

    await ctx["queue"].enqueue("drain_tracklists")
    logger.info("continuous tracklist drain: enqueued the next bounded slice", queued=status["queued"])


async def record_drain_slice_completion(ctx: dict[str, Any]) -> None:
    """``after_process`` Worker hook -- the cron's counterpart to :func:`continue_armed_tracklist_drain`.

    Gated on the ARM ROW's ``in_flight`` bit, not on ``job.function`` alone: every
    ``drain_tracklists`` completion reaches this hook (it is registered unconditionally,
    alongside ``deterministic_key.increment_completed``), but a manual "Run tracklist lookups" /
    Prioritize / Refresh slice never went through ``mark_slice_enqueued`` and so never set
    ``in_flight`` -- it is not part of an armed pass, and must not perturb the cooldown clock or
    the failure streak that pass is tracking.

    Only fires on a TERMINAL status (mirrors ``increment_completed``): ``Status.QUEUED`` means
    SAQ's own retry path re-queued the job for another attempt, and this hook must wait for that
    attempt's own eventual terminal outcome rather than treating a mid-retry event as the slice's
    result.

    ACCEPTED RACE: ``in_flight`` is a boolean, not a specific job identity, so if an operator's
    manual slice happens to be running CONCURRENTLY with a cron-enqueued one, whichever finishes
    first clears it -- the other's completion is then read as "not part of an armed pass" and
    skipped. ``drain_tracklists`` is deliberately UNKEYED for the same underlying reason
    (``tasks._shared.deterministic_key``'s allow-list: consecutive slices are distinct work, and
    an overlapping pair costs at most one duplicated request), so this mirrors an already-accepted
    imprecision rather than introducing a new one; a missed cooldown/failure update self-corrects
    on the very next slice.
    """
    job: Job | None = ctx.get("job")
    if job is None or job.function != "drain_tracklists" or job.status not in TERMINAL_STATUSES:
        return

    success = job.status == Status.COMPLETE
    async with ctx["async_session"]() as session:
        state = await get_arm_state(session)
        if not state.in_flight:
            await session.commit()
            return
        await mark_slice_finished(
            session,
            success=success,
            cooldown_seconds=cfg.tracklist_drain_cooldown_sec,
            max_consecutive_failures=MAX_CONSECUTIVE_SLICE_FAILURES,
            now=datetime.now(UTC),
        )
        await session.commit()

    if not success:
        logger.warning("continuous tracklist drain: a cron-enqueued slice failed", job_key=job.key, status=job.status)
