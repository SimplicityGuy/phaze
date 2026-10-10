"""FileCompanion model - join table linking companion files to media files."""

from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class FileCompanion(TimestampMixin, Base):
    """Many-to-many join table linking companion FileRecords to media FileRecords."""

    __tablename__ = "file_companions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    companion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    media_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False)

    derivation_method: Mapped[str | None] = mapped_column(String(32))
    derivation_revision: Mapped[str | None] = mapped_column(String(64))
    derivation_evidence: Mapped[list[str] | None] = mapped_column(JSONB)
    derived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("companion_id", "media_id", name="uq_file_companions_pair"),
        Index("ix_file_companions_companion_id", "companion_id"),
        Index("ix_file_companions_media_id", "media_id"),
    )
