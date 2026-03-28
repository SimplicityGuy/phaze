"""RenameProposal model - AI-generated rename/move proposals."""

import enum
import uuid

from sqlalchemy import Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class ProposalStatus(enum.StrEnum):
    """Status of a rename proposal."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RenameProposal(TimestampMixin, Base):
    """AI-generated rename/move proposal for a file."""

    __tablename__ = "proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=False)
    proposed_filename: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ProposalStatus.PENDING)
    context_used: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_proposals_status", "status"),)
