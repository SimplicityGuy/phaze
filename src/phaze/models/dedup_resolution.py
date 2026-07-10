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

scan_deletion dual-FK behavior (Phase 84, D-08 -- accepted, NOT a bug)
----------------------------------------------------------------------
``services/scan_deletion.py`` deletes markers matching EITHER foreign key -- both
``file_id IN batch`` AND ``canonical_file_id IN batch`` (the two-column ``FileCompanion`` precedent).
Consequence: deleting the scan batch that holds a *keeper* (canonical) file un-resolves that keeper's
duplicates -- their markers are removed, so once the readers key on ``NOT EXISTS(marker)`` those
duplicates reappear in the dedup UI for re-review. This is deliberate and safe: the keeper is gone, so
"keep this one, drop those" no longer holds, and re-review is the correct outcome (a wrongly-*kept*
marker would instead hide a file forever with no operator path to fix it). ``canonical_file_id`` has
been populated since ``032``'s backfill, so this already exists today; Phase 84's D-03 -- go-forward
writes populate ``canonical_file_id`` with the operator's actual pick -- merely exposes *every*
go-forward resolution to it. Documented here so it is not later rediscovered as a bug;
``services/scan_deletion.py`` is intentionally left unchanged.
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
