"""SchedulingLedger model -- durable record that a stage was scheduled for an item (Phase 45).

A standalone app table (NOT part of SAQ's auto-managed ``saq_jobs``) holding one row per
keyed enqueue. The single ``before_enqueue`` chokepoint (``apply_deterministic_key``) upserts
a row keyed by the deterministic ``job.key`` (``"<function>:<natural_id>"``); the completion /
terminal-failure path clears it. Recovery then re-queues exactly::

    orphaned = (ledger entries) - (live saq_jobs keys, status in queued/active)

so never-scheduled work (e.g. a ``DISCOVERED`` file awaiting a manual DAG trigger) is left
alone -- the missing fact behind the 2026-06-18 over-enqueue incident (~44.5k jobs).

The ledger lives OUTSIDE ``saq_jobs`` so it survives a broker truncate/restore (the only
genuine post-Phase-36 Postgres-broker loss case). ``force=True`` recovery now means "reconcile
the ledger now", not "sweep the domain backlog".

Columns:
  - ``key``        : PK, the deterministic ``"<function>:<natural_id>"`` dedup key.
  - ``function``   : the task name to re-enqueue.
  - ``routing``    : ``"agent"`` | ``"controller"`` replay hint (derivable from ``function``,
                     stored explicitly for an explicit, testable replay).
  - ``payload``    : JSONB, the FULL original ``job.kwargs`` so agent stages get the complete
                     ``ProcessFilePayload`` / ``ExtractMetadataPayload`` / etc. on replay -- not
                     just the natural id (the ``extra="forbid"`` schemas would otherwise reject).
  - ``enqueued_at``: server-default timestamp of the (re)enqueue.

NO foreign keys to ``files`` / ``tracklists``: the row must survive even if its target row is
mid-flight; the natural id lives inside ``payload``.

``created_at`` / ``updated_at`` come from :class:`TimestampMixin` (``updated_at`` carries
``onupdate=func.now()``) -- do not redeclare them here.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — SQLAlchemy resolves Mapped[] annotations at runtime
from typing import Any

from sqlalchemy import Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class SchedulingLedger(TimestampMixin, Base):
    """One row per keyed enqueue -- the durable "was scheduled" fact for recovery."""

    __tablename__ = "scheduling_ledger"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    function: Mapped[str] = mapped_column(String(64), nullable=False)
    routing: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_scheduling_ledger_function", "function"),)
