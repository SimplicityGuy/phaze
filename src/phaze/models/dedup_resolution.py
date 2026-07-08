"""DedupResolution model -- per-file marker that a duplicate resolved to a canonical file (Phase 77, D-07).

A new 1:1 sidecar (unique FK to ``files.id``) recording that a duplicate file has been resolved AND
which file it resolves *to* (``canonical_file_id``) -- enabling a future "duplicate of X" UI, robust
if the sha256 group later shifts. Marker-row existence = resolved; undo = DELETE the row (the enum's
``previous_state`` was a transition artifact, unnecessary under the derive-don't-store principle).

Backfilled from ``FileState.DUPLICATE_RESOLVED``, deriving ``canonical_file_id`` as a deterministic
non-resolved member of each ``sha256_hash`` group (NULL if none -- best-effort, RESEARCH Pitfall 4).

Mirrors the ``cloud_job`` 1:1 unique-FK sidecar precedent and the ``scheduling_ledger`` standalone
sidecar shape. ``created_at`` / ``updated_at`` come from :class:`TimestampMixin` -- do not redeclare.
No extra ``__table_args__`` index: the unique ``file_id`` constraint's implicit index serves the
marker-EXISTS lookup.
"""

from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class DedupResolution(TimestampMixin, Base):
    """One row per resolved duplicate file -- existence = resolved; undo = DELETE the row (D-07)."""

    __tablename__ = "dedup_resolution"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Unique FK: one resolution marker per file (cloud_job.py:72 precedent) + the ON CONFLICT (file_id)
    # backfill target.
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), unique=True, nullable=False)
    # NULLABLE (RESEARCH Pitfall 4): the canonical pointer is best-effort -- the original human keeper is
    # not recoverable and a group may have 0 or >1 non-resolved members; the marker's primary job is
    # "this file is resolved".
    canonical_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
