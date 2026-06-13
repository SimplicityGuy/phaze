"""PipelineStageControl model - durable per-stage pause/priority operator intent (Phase 37).

A standalone app table (NOT part of SAQ's auto-managed ``saq_jobs``) holding one row per
agent pipeline stage (``metadata`` / ``analyze`` / ``fingerprint``). Each row records whether
the stage is paused and its dequeue priority. The before-enqueue hook (Plan 37-02) stamps new
jobs from this table, and the control endpoints (Plan 37-04) mutate it alongside the live
``saq_jobs`` backlog UPDATE.

The ``priority`` value maps DIRECTLY onto SAQ's ``saq_jobs.priority`` (SMALLINT, LOWER dequeues
sooner) with no inversion. The DB CHECK ``priority BETWEEN 0 AND 100`` keeps every stage priority
inside SAQ's dequeue window (``priority BETWEEN 0 AND 32767``) so a stage can never be driven
silently un-dequeueable at the schema layer, even if endpoint clamping is bypassed (threat T-37-02).

``created_at`` / ``updated_at`` come from :class:`TimestampMixin` (``updated_at`` carries
``onupdate=func.now()``) -- do not redeclare them here.
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, SmallInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class PipelineStageControl(TimestampMixin, Base):
    """Per-stage pause/priority control row (metadata|analyze|fingerprint)."""

    __tablename__ = "pipeline_stage_control"

    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("50"))

    __table_args__ = (CheckConstraint("priority BETWEEN 0 AND 100", name="priority_range"),)
