"""Persistence + state transitions for the drain's durable ARM/DISARM flag (phaze-6nrrf).

Every function here does a bare read-modify-write of the single
:class:`~phaze.models.tracklist_drain_arm_state.TracklistDrainArmState` row and leaves the
COMMIT to its caller -- mirrors ``services.tracklist_priority`` / ``services.stage_control``, the
two other "durable operator intent" services in this codebase. ``with_for_update=True`` is used
on every mutation (mirrors ``routers.pipeline_stages._load_control_row``'s ``lock=True``): the
row is a SINGLE shared resource an operator's click and the continuous-drain cron can both reach
at once, so a read-modify-write that skips the lock can lose one side's write.

STATE MACHINE (see the model's own docstring for the full field list)
-----------------------------------------------------------------------
``get_or_create`` -> ``arm`` + ``mark_slice_enqueued`` IN ONE CALL (via ``arm_if_not_running``,
the "Run tracklist lookups" click -- phaze-5sj7k -- which enqueues that first slice itself; or
``arm_drain`` alone, kept for tests) -> {cron loop: ``mark_slice_enqueued`` -> [the slice runs] ->
``mark_slice_finished``} -> eventually ``disarm`` (operator's "Stop" click, or auto via
``mark_slice_finished``'s failure-streak branch, or via ``disarm`` called directly for the
queue-empty case).

``clear_stale_in_flight`` is the ONLY transition outside that happy path: it exists for a slice
whose SAQ ``after_process`` hook never ran at all (the owning worker PROCESS was killed, which
skips even a ``finally`` block) -- see its own docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from phaze.models.tracklist_drain_arm_state import SINGLETON_ID, TracklistDrainArmState


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _load_or_create(session: AsyncSession, *, lock: bool = False) -> TracklistDrainArmState:
    """Return the singleton row, creating it at its all-disarmed defaults if absent.

    Migration 059 seeds this row, so in production ``session.get`` always finds it. The defensive
    create keeps a fresh/partially-migrated test database from raising on the very first read --
    mirrors ``routers.pipeline_stages._load_control_row``'s identical defensive-create for
    ``PipelineStageControl``.
    """
    row = (
        await session.get(TracklistDrainArmState, SINGLETON_ID, with_for_update=True)
        if lock
        else await session.get(TracklistDrainArmState, SINGLETON_ID)
    )
    if row is None:
        # Explicit Python-side defaults (mirrors PipelineStageControl's identical defensive
        # create): the ``server_default`` values on the model only apply at INSERT, so an
        # unflushed row's Python attributes would otherwise read None until the next flush --
        # exactly the gap that made an un-migrated test database's very first read crash on
        # ``row.consecutive_failures += 1``.
        row = TracklistDrainArmState(id=SINGLETON_ID, armed=False, consecutive_failures=0, in_flight=False)
        session.add(row)
    return row


async def get_arm_state(session: AsyncSession) -> TracklistDrainArmState:
    """Read-only fetch of the current arm state, for the workspace status fragment."""
    return await _load_or_create(session)


def _apply_arm(row: TracklistDrainArmState, moment: datetime) -> None:
    """Shared field-level transition for arming, used by both :func:`arm_drain` and
    :func:`arm_if_not_running` -- the two entry points arm identically, and only differ in
    whether they check ``row.armed`` first."""
    row.armed = True
    row.armed_at = moment
    row.disarmed_at = None
    row.disarmed_reason = None
    row.consecutive_failures = 0
    row.next_eligible_at = None


async def arm_drain(session: AsyncSession, *, now: datetime | None = None) -> TracklistDrainArmState:
    """The operator's ONE consent decision: arm the continuous drain.

    Resets ``consecutive_failures`` and clears any prior ``disarmed_*`` bookkeeping -- a fresh arm
    is a fresh pass, not a continuation of whatever streak ended the last one.
    ``next_eligible_at`` is cleared to ``None`` (eligible immediately) so the very next cron tick
    -- at most a minute away -- can enqueue the first slice without waiting out a stale cooldown
    left over from before this arm.

    Deliberately does NOT touch ``in_flight`` / ``slice_enqueued_at``: a re-arm racing an
    in-flight slice from a PRIOR arm (unusual, but not impossible if the operator disarms and
    immediately re-arms) must not let a second slice enqueue on top of the first -- the cron's
    ``in_flight`` gate stays authoritative regardless of which arm cycle started it.

    The standalone ``POST /pipeline/arm-tracklist-drain`` endpoint that used to call this
    directly is GONE (phaze-5sj7k: no served template linked to it once "Run tracklist lookups"
    started arming the drain itself, which flags as an orphaned route -- see
    ``test_no_orphaned_ui_route``). This function is kept for tests that need an armed row
    without going through a live SAQ enqueue; the operator's own click now goes through
    :func:`arm_if_not_running`.
    """
    moment = now or datetime.now(UTC)
    row = await _load_or_create(session, lock=True)
    _apply_arm(row, moment)
    return row


async def arm_if_not_running(session: AsyncSession, *, now: datetime | None = None) -> bool:
    """Atomically arm the drain UNLESS it is already armed, marking its first slice ``in_flight``
    in the SAME lock/transaction as the arm -- the "Run tracklist lookups" click's consent
    decision (phaze-5sj7k).

    The operator's mental model is "clicking Run makes the lookups run until done", not "one
    click, one slice, arm separately for the rest" -- so this is now the ONE thing "Run tracklist
    lookups" does before its caller enqueues the first slice. Returns ``True`` when this call is
    the one that transitioned the row from disarmed to armed (the caller should then enqueue that
    first slice) and ``False`` when it was already armed (a second, or Nth, click while a pass is
    already running) -- the caller must not enqueue another slice in that case, since the
    already-running pass (via the continuous-drain cron, ``tasks.tracklist_drain_control``) owns
    pacing the rest of it. The check-and-set happens under the same ``with_for_update`` lock every
    other mutation here uses, so two concurrent clicks cannot both observe "not armed" and both
    enqueue a slice.

    REVIEW FIX (still phaze-5sj7k): the first version of this function armed the row but never
    set ``in_flight`` -- so a Run click's own slice was invisible to both
    ``continue_armed_tracklist_drain`` (which enqueued a SECOND, concurrent slice on its very
    next tick, since it saw armed and not in_flight and no cooldown yet -- doubling the spend of
    the public host's request budget ``in_flight`` exists to bound) and to
    ``record_drain_slice_completion`` (which only acts on ``in_flight`` rows, so the Run slice's
    own completion set no cooldown and did no failure accounting). Folding
    ``in_flight``/``slice_enqueued_at`` into THIS call, atomically with the arm, closes both
    holes: the row this transaction commits already reads as "a slice is running" the instant the
    caller's own enqueue follows. If that enqueue subsequently fails, the row is left
    ``in_flight=true`` for a slice that was never actually queued --
    :func:`clear_stale_in_flight` exists for exactly that shape (a slice whose ``after_process``
    hook never ran) and the continuous-drain cron already checks for it every tick, but its
    staleness window is several minutes; a caller that wants the row corrected sooner should call
    :func:`clear_stale_in_flight` itself on the enqueue failure path (see
    ``routers.pipeline.run_tracklist_drain_ui``).
    """
    moment = now or datetime.now(UTC)
    row = await _load_or_create(session, lock=True)
    if row.armed:
        return False
    _apply_arm(row, moment)
    row.in_flight = True
    row.slice_enqueued_at = moment
    return True


async def disarm_drain(session: AsyncSession, *, reason: str, now: datetime | None = None) -> TracklistDrainArmState:
    """Disarm the drain. A no-op (not an error) when already disarmed -- an operator double-click,
    or a queue-empty auto-disarm racing an operator's own click, must not overwrite the reason the
    FIRST disarm already recorded.

    Never touches ``in_flight``: an in-flight slice keeps running to completion regardless of
    when it disarms (the acceptance criterion "disarm stops after the in-flight slice, never
    kills a running slice mid-flight") -- the cron's own ``armed`` check is what stops the NEXT
    one from being enqueued, and ``after_process`` clears ``in_flight`` when this slice finishes.
    """
    moment = now or datetime.now(UTC)
    row = await _load_or_create(session, lock=True)
    if row.armed:
        row.armed = False
        row.disarmed_at = moment
        row.disarmed_reason = reason
    return row


async def mark_slice_enqueued(session: AsyncSession, *, now: datetime | None = None) -> TracklistDrainArmState:
    """Record that the continuous-drain cron just enqueued a slice -- the ``in_flight`` gate that
    stops the NEXT cron tick from stacking a second one on top of it."""
    moment = now or datetime.now(UTC)
    row = await _load_or_create(session, lock=True)
    row.in_flight = True
    row.slice_enqueued_at = moment
    return row


async def mark_slice_finished(
    session: AsyncSession,
    *,
    success: bool,
    cooldown_seconds: int,
    max_consecutive_failures: int,
    now: datetime | None = None,
) -> TracklistDrainArmState:
    """Record a cron-enqueued slice's terminal outcome: clear ``in_flight``, arm the cooldown for
    the next tick, and apply the failure-streak / auto-disarm rule.

    ``success`` resets ``consecutive_failures`` to 0 -- ANY clean slice, not just a run that found
    something, proves the drain infrastructure itself is healthy (a slice over an already-cached
    queue tail legitimately attempts nothing and still completes cleanly). A non-success slice
    increments the streak and, once it reaches ``max_consecutive_failures`` on a currently-armed
    row, auto-disarms with ``disarmed_reason="failures"`` -- the bead's auto-disarm rule.
    """
    moment = now or datetime.now(UTC)
    row = await _load_or_create(session, lock=True)
    row.in_flight = False
    row.slice_enqueued_at = None
    row.next_eligible_at = moment + timedelta(seconds=cooldown_seconds)
    if success:
        row.consecutive_failures = 0
    else:
        row.consecutive_failures += 1
        if row.armed and row.consecutive_failures >= max_consecutive_failures:
            row.armed = False
            row.disarmed_at = moment
            row.disarmed_reason = "failures"
    return row


async def clear_stale_in_flight(session: AsyncSession, *, cooldown_seconds: int, now: datetime | None = None) -> TracklistDrainArmState:
    """Self-heal an ``in_flight`` row whose owning slice's ``after_process`` hook never ran.

    A SAQ Worker's ``after_process`` runs in a ``finally`` on every ORDINARY outcome (success,
    retry, terminal failure) -- but a killed worker PROCESS (OOM-killed, a hard container
    restart) skips even that, because nothing in the process survives to run it. Without this,
    such a slice would leave ``in_flight = true`` forever, and the continuous-drain cron -- which
    treats ``in_flight`` as "a slice is already running, do not enqueue another" -- would stay
    silently wedged even though the operator's ``armed`` flag still reads true.

    Deliberately does NOT touch ``consecutive_failures``: the slice's actual outcome is unknown
    (it may have completed and simply lost its ``after_process`` call, e.g. to a restart between
    the last DB write and that hook running), so counting it as a failure risks a false
    auto-disarm on infrastructure noise rather than a real drain problem. The cooldown still
    applies before the next attempt, so a process that keeps dying immediately after enqueue
    cannot spin the cron in a tight loop.
    """
    moment = now or datetime.now(UTC)
    row = await _load_or_create(session, lock=True)
    row.in_flight = False
    row.slice_enqueued_at = None
    row.next_eligible_at = moment + timedelta(seconds=cooldown_seconds)
    return row
