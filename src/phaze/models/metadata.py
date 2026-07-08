"""FileMetadata model - extracted tag metadata."""

from datetime import datetime
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class FileMetadata(TimestampMixin, Base):
    """Extracted tag metadata for a file (1:1 with files)."""

    __tablename__ = "metadata"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), unique=True, nullable=False)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    album: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    genre: Mapped[str | None] = mapped_column(Text, nullable=True)
    track_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Phase 77 (D-01, migration 032): nullable metadata-stage failure markers on the 1:1 output table.
    # Go-forward only -- D-03 gives metadata NO backfill (report_metadata_failed persisted no source).
    # The future done(metadata) predicate tightens to `EXISTS metadata WHERE file_id=... AND failed_at
    # IS NULL` (D-02, honored in the derivation phase, not here).
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 77 (PERF-01, migration 032): partial index mirroring migration 032 with byte-identical name
    # + normalized IS-NOT-NULL predicate text (empty-autogenerate-diff contract). FileMetadata had no
    # __table_args__ before this phase.
    __table_args__ = (Index("ix_metadata_failed", "file_id", postgresql_where=text("failed_at IS NOT NULL")),)
