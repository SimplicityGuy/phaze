"""ScanBatch model - tracks file discovery scan operations."""

import enum
import uuid

from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class ScanStatus(enum.StrEnum):
    """Status of a scan batch operation."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ScanBatch(TimestampMixin, Base):
    """Tracks a file discovery scan operation with progress and status."""

    __tablename__ = "scan_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scan_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ScanStatus.RUNNING)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    processed_files: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
