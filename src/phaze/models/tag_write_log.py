"""TagWriteLog model - append-only audit trail for tag write operations."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — SQLAlchemy resolves Mapped[] annotations at runtime
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phaze.enums.tag_write import TagWriteStatus
from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from phaze.models.file import FileRecord


# phaze-6bkk: the enum moved to the DB-free ``phaze.enums.tag_write`` so the agent-side
# ``write_file_tags`` task and the ``schemas.agent_tag_writes`` wire contract can name the same
# statuses without importing SQLAlchemy (the D-25 agent import boundary). Re-exported here so every
# existing ``from phaze.models.tag_write_log import TagWriteStatus`` import keeps working.
__all__ = ["TagWriteLog", "TagWriteStatus"]


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
    reviewed_before_tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    review_source_versions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    discrepancies: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Migration 040 (phaze-36rc) moved this column to ``timestamptz`` in the database but left the
    # model declaring bare ``DateTime`` -- so the ORM went on claiming naive while asyncpg decoded
    # aware. phaze-cz3m closes that half-fix; see ``models.base.TimestampMixin`` for why the
    # mismatch is a live defect and not a cosmetic one.
    written_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    file: Mapped[FileRecord] = relationship("FileRecord", foreign_keys=[file_id], lazy="noload")

    __table_args__ = (
        Index("ix_tag_write_log_file_id", "file_id"),
        Index("ix_tag_write_log_status", "status"),
    )
