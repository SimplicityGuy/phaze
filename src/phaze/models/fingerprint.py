"""FingerprintResult model -- per-engine fingerprint result for a file."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class FingerprintResult(TimestampMixin, Base):
    """Per-engine fingerprint result for a file."""

    __tablename__ = "fingerprint_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=False)
    engine: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_fprint_file_engine", "file_id", "engine", unique=True),
        # Phase 77 (PERF-01, migration 032): fingerprint-success partial index mirroring migration 032.
        # MUST be spelled `= ANY (ARRAY[...])` -- Postgres reserializes a bare `status IN (...)` to
        # `= ANY(ARRAY[...])`, which would break the empty-autogenerate-diff comparison (RESEARCH Pitfall 1).
        Index("ix_fprint_success", "file_id", postgresql_where=text("status = ANY (ARRAY['success','completed'])")),
    )
