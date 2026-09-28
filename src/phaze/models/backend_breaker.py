"""BackendBreaker model - the per-backend control-plane-unreachable circuit breaker (phaze-j0ixx).

One row per backend id, written the first time that backend's pods report the control plane
unreachable (``job_runner.EXIT_CONTROL_PLANE_UNREACHABLE``); absent for a backend that never has. The
row is the breaker's durable state, shared by three processes: the reconcile cron trips it, the drain
reads it once per tick to hold the backend, and the API's presign endpoint closes it when a pod on
that backend reaches the control plane again. ``services/backend_breaker.py`` owns every read and
write and documents the thresholds and the probe.

``tripped_at`` NULL means closed. ``reset_at`` is the last close: unreachable exits recorded at or
before it do not count toward the next trip, so a backend that just recovered cannot re-trip on the
failures that opened it. ``created_at`` / ``updated_at`` come from :class:`TimestampMixin`.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class BackendBreaker(TimestampMixin, Base):
    """Circuit-breaker state for one backend (``backend_id`` = the registry entry id)."""

    __tablename__ = "backend_breaker"

    # The registry entry id (``[[backends]] id``) -- the same free-text value ``cloud_job.backend_id``
    # carries. No FK: the registry lives in config, not in a table.
    backend_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    # When the breaker opened; NULL while closed.
    tripped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why it opened, in words an operator can act on (which files, which window). NULL while closed.
    trip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # While open: the earliest time the drain may dispatch ONE probe file to this backend. Pushed forward
    # each time a probe slot is granted and each time another unreachable exit arrives.
    next_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The last close. Unreachable exits at or before it are the failures that opened the breaker and
    # no longer count.
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
