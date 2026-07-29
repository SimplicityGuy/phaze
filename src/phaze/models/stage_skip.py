"""StageSkip model -- per-(file, stage) force-skip marker for the enrich stages (Phase 87, D-13).

A ``(file_id, stage)`` sidecar recording that an operator has *force-skipped* an enrich stage for a
file. Marker-row existence = skipped; undo = DELETE the row (the derive-don't-store principle -- status
stays derived, this is the sole *stored* fact).

Why a sidecar (not a ``skipped_at`` column, unlike the Phase-81 failure markers): at design time one
enrich stage (the retired fingerprint stage, phaze-0jpe) had no 1:1 output table, so the "add a column
to the output table" shape could not cover every enrich stage uniformly. A ``(file_id, stage)`` sidecar
was the only uniform enrich-wide shape (RESEARCH sec 1), and it is kept -- it is stage-agnostic by
construction, so adding an enrich stage never needs a schema shape decision again.

Mirrors the ``dedup_resolution`` sidecar precedent (imports, ``TimestampMixin + Base``, UUID PK,
FK-to-``files.id``, ``server_default=func.now()`` timestamp). The one structural delta: uniqueness is on
the **composite** ``(file_id, stage)`` (<=1 skip per file/stage), not ``file_id`` alone.
``created_at`` / ``updated_at`` come from :class:`TimestampMixin` -- do not redeclare.

``__table_args__`` mirrors what migration 037 creates byte-for-byte (the ORM half of the
empty-autogenerate-diff contract):

* ``uq_stage_skip_file_stage`` UNIQUE(file_id, stage) -- the <=1-row-per-(file, stage) invariant (D-13a,
  T-87-03). A plain b-tree UNIQUE avoids the ``= ANY(ARRAY[...])`` reserialization trap (Pitfall 5).
* ``ck_stage_skip_enrich_only`` CHECK -- ``stage IN ('metadata','analyze','fingerprint')`` (D-10, OQ-3,
  T-87-02): approval/execute can never carry a skip marker at the schema layer. ``'fingerprint'`` is a
  RESIDUAL value: phaze-0jpe removed that stage, and ``skipped_clause`` (services/stage_status.py)
  already rejects it, so nothing can write one. Narrowing the CHECK is a schema change and is owned by
  the removal molecule's migration bead (phaze-0jpe.4), not by the code removal -- editing the ORM
  constraint ahead of the migration would break the baseline's autogenerate-parity contract. The bare ``name`` here is
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
    # The enrich value 'metadata' | 'analyze' (guarded by the CHECK constraint, D-10; the constraint
    # still admits the residual 'fingerprint' until phaze-0jpe.4's migration narrows it -- no writer
    # can produce one, see the module docstring).
    stage: Mapped[str] = mapped_column(String, nullable=False)
    # D-09: a reason is required (nullable=False) -- force-skip is a deliberate, justified operator action.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    skipped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("file_id", "stage", name="uq_stage_skip_file_stage"),
        CheckConstraint("stage IN ('metadata','analyze','fingerprint')", name="enrich_only"),
    )
