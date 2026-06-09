"""ScanBatch model - tracks file discovery scan operations."""

from datetime import datetime
import enum
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class ScanStatus(enum.StrEnum):
    """Status of a scan batch operation."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    LIVE = "live"  # Watcher-originated sentinel; one per agent (D-09, D-10)


class ScanBatch(TimestampMixin, Base):
    """Tracks a file discovery scan operation with progress and status."""

    __tablename__ = "scan_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
        default="legacy-application-server",
    )
    scan_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ScanStatus.RUNNING)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    processed_files: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stamped once when the batch reaches a terminal (completed/failed) state so
    # the admin UI's elapsed timer freezes instead of running forever. tz-aware
    # to match the runtime type of TimestampMixin's columns (see the
    # elapsed_seconds docstring in routers/pipeline_scans.py).
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-progress heartbeat: stamped every time the scan makes real progress
    # (agent PATCH applying a non-no-op change, both create paths, run_scan's
    # terminal updates). Drives the admin UI's "live activity" indicator (green
    # pulsing dot + "·Ns ago") and the control-side stall reaper, which marks a
    # RUNNING batch FAILED once this heartbeat is older than scan_stall_seconds.
    # tz-aware to match TimestampMixin's columns (see elapsed_seconds in
    # routers/pipeline_scans.py). Nullable: legacy rows are backfilled to
    # updated_at by migration 017.
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_scan_batches_agent_id", "agent_id"),
        Index(
            "uq_scan_batches_agent_id_live",
            "agent_id",
            unique=True,
            postgresql_where=text("status = 'live'"),
        ),
    )
