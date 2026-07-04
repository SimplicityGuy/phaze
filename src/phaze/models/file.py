"""FileRecord model - central file record with state machine."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from phaze.models.metadata import FileMetadata


class FileState(enum.StrEnum):
    """States in the file processing pipeline.

    Phase 26 D-28 adds MOVED and UNCHANGED. Conceptually:
    - MOVED  -- agent successfully copy-verified-deleted the file at the new path
                (set jointly with ProposalStatus.EXECUTED via PATCH /proposals/{id}/state).
    - UNCHANGED -- proposal execution failed (or was cancelled) and the file remains
                   at its original path (set jointly with ProposalStatus.FAILED).
    EXECUTED and FAILED are Phase 25-era state names retained for compatibility with
    existing execution log emit paths; Phase 28's batch execution will adopt MOVED/UNCHANGED
    when wiring the PATCH endpoint into the live dispatch loop.
    """

    DISCOVERED = "discovered"
    METADATA_EXTRACTED = "metadata_extracted"
    FINGERPRINTED = "fingerprinted"
    ANALYZED = "analyzed"
    # Phase 43: terminal failure of windowed analysis (timeout / crash / error).
    # Code-only -- FileRecord.state is String(30), so no enum migration is needed.
    ANALYSIS_FAILED = "analysis_failed"
    # Phase 49 D-01: a long file held for cloud (compute-agent) analysis instead of the
    # on-prem file-server. Code-only -- "awaiting_cloud" is 14 chars and fits the existing
    # String(30) state column, so no enum migration is needed (ANALYSIS_FAILED precedent).
    AWAITING_CLOUD = "awaiting_cloud"
    # Phase 50 D-08: cloud push pipeline states. A long file held in AWAITING_CLOUD is
    # staged by the bounded cloud-window cron: PUSHING == rsync in progress to the compute
    # agent's scratch dir; PUSHED == landed on compute scratch, awaiting/within analysis.
    # Code-only StrEnum over the existing String(30) state column ("pushing"/"pushed" are 7/6
    # chars) → no enum migration is needed (ANALYSIS_FAILED / AWAITING_CLOUD precedent).
    PUSHING = "pushing"
    PUSHED = "pushed"
    # Phase 69 (SCHED-01/03, CR-01 gap-close): a long file the tiered drain spilled to the LOCAL
    # backend (rank 99) and is analyzing on-prem via ``process_file``. Code-only StrEnum over the
    # existing String(30) state column ("local_analyzing" is 15 chars ≤ 30) → no enum migration is
    # needed (ANALYSIS_FAILED / AWAITING_CLOUD / PUSHING precedent). This is the drain-local
    # in-analysis lane: it removes the file from ``get_cloud_staging_candidates`` (which selects
    # ``state == AWAITING_CLOUD``) while its local ``process_file`` is in flight, so a locally-spilled
    # file can NOT be double-dispatched to a cloud backend once a slot frees. Deliberately NOT
    # analyze-done and NOT push-done (it is in neither ``_select_done_analyze_ids`` nor
    # ``_select_done_push_ids`` in reenqueue.py), so a lost local job re-drives via the scheduling
    # ledger; and it carries no ``cloud_job`` row, so reconcile/recovery never misclassifies it as
    # cloud-owned. ``put_analysis`` flips it → ANALYZED on a non-empty result body (never gated on
    # prior state), so completion proceeds normally.
    LOCAL_ANALYZING = "local_analyzing"
    PROPOSAL_GENERATED = "proposal_generated"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    DUPLICATE_RESOLVED = "duplicate_resolved"
    MOVED = "moved"
    UNCHANGED = "unchanged"


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
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scan_batches.id"), nullable=True)
    agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
        default="legacy-application-server",
    )

    file_metadata: Mapped[FileMetadata | None] = relationship("FileMetadata", foreign_keys="FileMetadata.file_id", uselist=False, lazy="noload")

    __table_args__ = (
        Index("ix_files_state", "state"),
        Index("ix_files_sha256_hash", "sha256_hash"),
        Index("uq_files_agent_id_original_path", "agent_id", "original_path", unique=True),
    )
