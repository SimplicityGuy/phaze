"""Durable operator ARM/DISARM flag for the continuous 1001Tracklists drain (phaze-6nrrf).

WHY THIS TABLE EXISTS
----------------------
``tasks.tracklist_drain.drain_tracklists`` still dispenses exactly one bounded (<=100 lookup)
slice per enqueue -- that framing is UNCHANGED (see that module's docstring). What changes here is
who re-enqueues the NEXT slice: previously only an operator click; now, optionally, the
``continue_armed_tracklist_drain`` CronJob (``tasks.tracklist_drain_control``), gated on this row.

A single row (the ``id`` CHECK enforces it) rather than a bare boolean setting because arming is
an OPERATOR ACTION with a timestamp and a reason for how it ended, not just a toggle -- the
tracklist workspace renders "armed since <armed_at>" / "disarmed: <disarmed_reason>", and the
continuous-drain cron needs ``in_flight`` / ``slice_enqueued_at`` / ``next_eligible_at`` to pace
itself without a second source of truth (no in-memory cache, no cursor file -- the drain's own
"resumption is a property of the data" discipline, see ``services.tracklist_drain``).

THE SAFETY INVARIANT THIS ROW CARRIES
--------------------------------------
``armed`` starts (and, after a fresh migration, MUST start) ``false``. Nothing in this codebase
ever sets it ``true`` except the operator's explicit "Arm" click
(``services.tracklist_drain_arm.arm_drain``). A controller restart while armed reads this row and
RESUMES the already-armed pass (the durable flag IS the operator's consent for that); a controller
booting with no prior consent stays disarmed, exactly like ``drain_tracklists`` itself has never
had a CronJob. See the epic's ethics bound (``tasks.tracklist_drain`` module docstring) for why
that boundary matters: a residential IP, a headful browser, and a shared host's published budget.

FIELDS
------
* ``armed`` / ``armed_at`` -- the operator's live consent and when it was given.
* ``disarmed_at`` / ``disarmed_reason`` -- how the LAST arming ended: ``"operator"`` (explicit
  disarm click), ``"queue_empty"`` (the drain finished the corpus), or ``"failures"`` (three
  consecutive slice failures, phaze-6nrrf's auto-disarm rule). ``None`` before the first
  arm/disarm cycle.
* ``consecutive_failures`` -- resets to 0 on any slice that completes without raising; counts
  toward the 3-failure auto-disarm otherwise. Scoped to CRON-DRIVEN slices only (``in_flight``
  gates it) -- a manual "Run tracklist lookups" click failing does not burn this budget, since it
  is not part of an armed pass at all (it may run while disarmed).
* ``in_flight`` / ``slice_enqueued_at`` -- true for the window between the cron enqueueing a slice
  and that slice's SAQ ``after_process`` hook observing its terminal outcome. Prevents the cron
  from stacking a second slice on top of one still running. ``slice_enqueued_at`` also backs a
  self-heal: a slice whose ``after_process`` never ran (a killed worker process, which skips even
  a ``finally`` block) would otherwise wedge ``in_flight`` forever.
* ``next_eligible_at`` -- the cooldown boundary (``ControlSettings.tracklist_drain_cooldown_sec``,
  default 600s/10min, operator-tunable) the cron compares ``now()`` against before enqueueing the
  next slice. ``NULL`` means eligible immediately (the state just after an ``arm_drain`` call).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 -- SQLAlchemy resolves Mapped[] annotations at runtime

from sqlalchemy import Boolean, CheckConstraint, DateTime, SmallInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


SINGLETON_ID = "tracklist_drain"
"""The one and only row id this table ever holds -- enforced by the CHECK below, not just convention."""


class TracklistDrainArmState(TimestampMixin, Base):
    """The single durable ARM/DISARM row for the continuous 1001Tracklists drain."""

    __tablename__ = "tracklist_drain_arm_state"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    armed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    armed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disarmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disarmed_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    in_flight: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    slice_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Bare constraint name (no `ck_` / table prefix) -- SQLAlchemy's naming convention
    # (models/base.py) supplies both, and a pre-prefixed name here would double-prefix
    # (phaze-x8tof) rather than error.
    __table_args__ = (CheckConstraint(f"id = '{SINGLETON_ID}'", name="singleton"),)
