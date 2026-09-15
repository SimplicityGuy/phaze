"""Metadata-only inventory of companion files skipped by a scan."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base


class OrphanCompanionDiagnostic(Base):
    """A scan-owned orphan companion path, never an ingested file record."""

    __tablename__ = "orphan_companion_diagnostics"

    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scan_batches.id", ondelete="CASCADE"),
        primary_key=True,
    )
    normalized_path: Mapped[str] = mapped_column(Text, primary_key=True)
    configured_root: Mapped[str] = mapped_column(Text, nullable=False)
    companion_extension: Mapped[str] = mapped_column(String(5), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "companion_extension IN ('.cue', '.m3u', '.m3u8', '.nfo', '.pls', '.txt')",
            name="accepted_extension",
        ),
        Index(
            "ix_orphan_companion_diagnostics_batch_root_path",
            "batch_id",
            "configured_root",
            "normalized_path",
        ),
        Index(
            "ix_orphan_companion_diagnostics_batch_extension_path",
            "batch_id",
            "companion_extension",
            "normalized_path",
        ),
    )
