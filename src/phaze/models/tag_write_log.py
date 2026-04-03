"""TagWriteLog model - append-only audit trail for tag write operations."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from datetime import datetime

    from phaze.models.file import FileRecord


class TagWriteStatus(enum.StrEnum):
    """Status of a tag write operation."""

    COMPLETED = "completed"
    FAILED = "failed"
    DISCREPANCY = "discrepancy"


class TagWriteLog(TimestampMixin, Base):
    """Append-only audit log for tag write operations.

    Records every tag write attempt with before/after snapshots for full
    traceability. Follows the ExecutionLog append-only pattern.
    """

    __tablename__ = "tag_write_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=False)
    before_tags: Mapped[dict] = mapped_column(JSONB, nullable=False)
    after_tags: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    discrepancies: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    written_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    file: Mapped[FileRecord] = relationship("FileRecord", foreign_keys=[file_id], lazy="noload")

    __table_args__ = (
        Index("ix_tag_write_log_file_id", "file_id"),
        Index("ix_tag_write_log_status", "status"),
    )
