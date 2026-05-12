"""RenameProposal model - AI-generated rename/move proposals."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from phaze.models.file import FileRecord


class ProposalStatus(enum.StrEnum):
    """Status of a rename proposal.

    Phase 26 D-28 extends this enum with EXECUTED and FAILED to support the
    state-machine transitions emitted by PATCH /api/internal/agent/proposals/{id}/state.
    Transitions: APPROVED -> EXECUTED or APPROVED -> FAILED (terminal).
    Re-PATCHing the same terminal state is a 200 idempotent no-op; other
    transitions return 409.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


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

    file: Mapped[FileRecord] = relationship(lazy="raise")

    __table_args__ = (Index("ix_proposals_status", "status"),)
