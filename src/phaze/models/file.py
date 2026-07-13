"""FileRecord model - central file record.

Phase 90 (MIG-04): the ``FileState`` StrEnum, the ``files.state`` column, and the
``ix_files_state`` index were removed. A file's stage/status is now DERIVED entirely from its
output tables (``analysis`` / ``metadata`` / ``fingerprint_results`` / ``proposals`` markers, the
``cloud_job`` sidecar, and the ``dedup_resolution`` marker) via ``services/stage_status.py``. The
irreversible column drop shipped in migration ``039_drop_files_state_column``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from phaze.models.metadata import FileMetadata


class FileRecord(TimestampMixin, Base):
    """Central file record tracking each file through the processing pipeline."""

    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    current_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(String(10), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scan_batches.id"), nullable=True)
    agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
    )

    file_metadata: Mapped[FileMetadata | None] = relationship("FileMetadata", foreign_keys="FileMetadata.file_id", uselist=False, lazy="noload")

    __table_args__ = (
        Index("ix_files_sha256_hash", "sha256_hash"),
        Index("uq_files_agent_id_original_path", "agent_id", "original_path", unique=True),
    )
