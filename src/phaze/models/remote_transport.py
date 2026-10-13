"""Durable pacing, budget and stop state for the remote transport effect, one ledger per provider.

Postgres, not Valkey: Valkey is a cache in this stack, and a counter that a cache eviction or restart can
reset is exactly the failure this ledger exists to prevent. ``phaze.services.remote_transport`` owns
every write. The attempt log is the evidence the ledger row is checked against: a ledger whose counter or
last reservation disagrees with its attempts is corrupt and the effect refuses to send rather than
starting the count again.
"""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base


class RemoteTransportLedger(Base):
    """One row per remote provider: the reservation counter, pacing anchor, Retry-After and the stop."""

    __tablename__ = "remote_transport_ledgers"

    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Reservations ever made. Equals the highest attempt ordinal and the attempt count.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Wall time of the latest reservation, from the worker clock; the next one waits a full gap after it.
    last_reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A Retry-After the host sent: nothing is reserved before it.
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A persistent stop (403, 429, a genuine challenge, a robots disallow). Only clear_stop lifts it.
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_code: Mapped[str | None] = mapped_column(String(64))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleared_by: Mapped[str | None] = mapped_column(String(128))
    clear_reason: Mapped[str | None] = mapped_column(String(512))

    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ordinal_nonneg"),
        CheckConstraint("(ordinal = 0) = (last_reserved_at IS NULL)", name="reservation_anchor"),
        CheckConstraint("(stopped_at IS NULL) = (stop_code IS NULL)", name="stop_pair"),
    )


class RemoteTransportAttempt(Base):
    """One reservation, written and committed before its request is sent; the outcome is filled in after."""

    __tablename__ = "remote_transport_attempts"

    provider_id: Mapped[str] = mapped_column(String(64), ForeignKey("remote_transport_ledgers.provider_id", ondelete="RESTRICT"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # "search", "fetch" or "robots". No URL, term, native ID or header is recorded.
    request_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # NULL until the exchange finishes; a NULL here after the fact is an interrupted send, still charged.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    http_status: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ordinal_positive"),
        CheckConstraint("request_kind IN ('search', 'fetch', 'robots')", name="request_kind"),
        Index("ix_remote_transport_attempts_window", "provider_id", "reserved_at"),
    )
