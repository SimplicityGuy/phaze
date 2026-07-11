"""StageSkip model -- per-(file, stage) force-skip marker for the enrich stages (Phase 87, D-13).

A ``(file_id, stage)`` sidecar recording that an operator has *force-skipped* an enrich stage for a
file. Marker-row existence = skipped; undo = DELETE the row (the derive-don't-store principle -- status
stays derived, this is the sole *stored* fact).

Why a sidecar (not a ``skipped_at`` column, unlike the Phase-81 failure markers): fingerprint has no
1:1 output table (``fingerprint_results`` is 1:N), so the "add a column to the output table" shape
cannot cover all three enrich stages uniformly. A ``(file_id, stage)`` sidecar is the only uniform
enrich-wide shape (RESEARCH sec 1).

Mirrors the ``dedup_resolution`` sidecar precedent (imports, ``TimestampMixin + Base``, UUID PK,
FK-to-``files.id``, ``server_default=func.now()`` timestamp). The one structural delta: uniqueness is on
the **composite** ``(file_id, stage)`` (<=1 skip per file/stage), not ``file_id`` alone.
``created_at`` / ``updated_at`` come from :class:`TimestampMixin` -- do not redeclare.

``__table_args__`` mirrors what migration 037 creates byte-for-byte (the ORM half of the
empty-autogenerate-diff contract):

* ``uq_stage_skip_file_stage`` UNIQUE(file_id, stage) -- the <=1-row-per-(file, stage) invariant (D-13a,
  T-87-03). A plain b-tree UNIQUE avoids the ``= ANY(ARRAY[...])`` reserialization trap (Pitfall 5).
* ``ck_stage_skip_enrich_only`` CHECK -- ``stage IN ('metadata','analyze','fingerprint')`` (D-10, OQ-3,
  T-87-02): approval/execute can never carry a skip marker at the schema layer. The bare ``name`` here is
  the ``%(constraint_name)s`` token; the ``ck_%(table_name)s_%(constraint_name)s`` convention prepends
  ``ck_stage_skip_``, rendering ``ck_stage_skip_enrich_only`` -- matching the ``op.f(...)`` name in 037
  (mirror the ``analysis.py`` bare-name CheckConstraint discipline).
"""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class StageSkip(TimestampMixin, Base):
    """One row per force-skipped (file, stage) -- existence = skipped; undo = DELETE the row (D-13)."""

    __tablename__ = "stage_skip"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # NOT unique on its own -- uniqueness is the composite (file_id, stage) constraint below.
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=False)
    # The enrich value 'metadata' | 'analyze' | 'fingerprint' (guarded by the CHECK constraint, D-10).
    stage: Mapped[str] = mapped_column(String, nullable=False)
    # D-09: a reason is required (nullable=False) -- force-skip is a deliberate, justified operator action.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    skipped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("file_id", "stage", name="uq_stage_skip_file_stage"),
        CheckConstraint("stage IN ('metadata','analyze','fingerprint')", name="enrich_only"),
    )
