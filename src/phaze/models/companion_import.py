"""Small durable acquisition evidence and resumable companion import work."""

from datetime import datetime
from typing import Any
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base


class ProviderAcquisitionAttempt(Base):
    """One authenticated physical read; semantic observation reuse never erases its outcome."""

    __tablename__ = "provider_acquisition_attempts"
    attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_object_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("provider_source_objects.id"), nullable=False)
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("provider_source_observations.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp())
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(String(4096), nullable=False)
    revision: Mapped[str | None] = mapped_column(String(2048))
    revision_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    truncated: Mapped[bool] = mapped_column(nullable=False)
    freshness: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="capture")
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    __table_args__ = (
        UniqueConstraint("source_object_id", "ordinal"),
        Index("ix_provider_acquisition_attempts_source_ordinal", "source_object_id", "ordinal"),
    )


class CompanionImportRun(Base):
    """Fixed server-time inventory boundary; UUID cursor only traverses that inventory."""

    __tablename__ = "companion_import_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    continuation: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    request_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    cursor: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    associated: Mapped[bool] = mapped_column(nullable=False, default=False)
    enumerated: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (Index("ix_companion_import_runs_agent", "agent_id", "created_at"),)


class CompanionImportItem(Base):
    """Durable per-source checkpoint, not authority to approve imported content."""

    __tablename__ = "companion_import_items"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companion_import_runs.id"), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    code: Mapped[str] = mapped_column(String(128), nullable=False, default="enumerated")
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("provider_source_observations.id"))
    target_cursor: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    had_incomplete: Mapped[bool] = mapped_column(nullable=False, default=False)
    tracklist_status: Mapped[str | None] = mapped_column(String(32))
    release_status: Mapped[str | None] = mapped_column(String(32))
    had_unresolved: Mapped[bool] = mapped_column(nullable=False, default=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint("run_id", "file_id"), Index("ix_companion_import_items_run_state", "run_id", "state", "id"))
