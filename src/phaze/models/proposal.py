"""RenameProposal model - AI-generated rename/move proposals."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import Float, ForeignKey, Index, String, Text, text
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


# The review-UI state machine (phaze-uu17), stated ONCE next to the enum it constrains.
#
# It lived in routers/proposals.py until phaze-a6hm.11, which needed the SAME fact in two places:
# the router, which enforces it on the write, and the propose workspace render, which greys out the
# checkbox of a row that cannot legally transition. Those two must never disagree -- a UI that
# offers a checkbox the server will silently refuse is how "50 approved" gets reported for 12 real
# transitions -- and they cannot import each other (routers/proposals imports routers/shell for the
# propose list context, so the reverse edge would be a cycle). Hoisting it to the model, which both
# already import, is what makes ONE definition reachable from both.
#
# Terminal EXECUTED/FAILED rows are the authoritative record that a rename was applied and must
# never be flipped back by the UI, so PENDING is the only legal from-state for approve/reject.
APPROVE_REJECT_FROM = frozenset({ProposalStatus.PENDING})
UNDO_FROM = frozenset({ProposalStatus.PENDING, ProposalStatus.APPROVED, ProposalStatus.REJECTED})


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

    # ``uq_proposals_file_id_pending`` mirrors alembic revision 019: a partial
    # UNIQUE index enforcing one PENDING proposal per file (D-04). It is the
    # ON CONFLICT target for ``services.proposal.store_proposals``' upsert.
    # Kept here so autogenerate / the ORM stay in sync with the migration.
    __table_args__ = (
        Index("ix_proposals_status", "status"),
        Index("uq_proposals_file_id_pending", "file_id", unique=True, postgresql_where=text("status = 'pending'")),
    )
