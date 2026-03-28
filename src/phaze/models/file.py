"""FileRecord model - central file record with state machine."""

import enum
import uuid

from sqlalchemy import BigInteger, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class FileState(enum.StrEnum):
    """States in the file processing pipeline."""

    DISCOVERED = "discovered"
    METADATA_EXTRACTED = "metadata_extracted"
    FINGERPRINTED = "fingerprinted"
    ANALYZED = "analyzed"
    PROPOSAL_GENERATED = "proposal_generated"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


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
    state: Mapped[str] = mapped_column(String(30), nullable=False, default=FileState.DISCOVERED)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        Index("ix_files_state", "state"),
        Index("ix_files_sha256_hash", "sha256_hash"),
    )
