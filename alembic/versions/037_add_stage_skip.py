"""Add the ``stage_skip`` force-skip marker sidecar (Phase 87, D-13/UI-04).

Additive-only, greenfield migration. It lands the ``(file_id, stage)`` sidecar table the rest of
Phase 87 derives from -- the durable, distinct *force-skip marker* for the three enrich stages
(``metadata`` / ``analyze`` / ``fingerprint``). Marker-row existence = force-skipped; status stays
*derived* (derive-don't-store). ``037.upgrade()`` touches ONLY the new ``stage_skip`` table.

Why a sidecar (not a ``skipped_at`` column, unlike the Phase-81 failure markers): fingerprint has no
1:1 output table (``fingerprint_results`` is 1:N), so the "add a column to the output table" shape
cannot cover all three enrich stages uniformly. A ``(file_id, stage)`` sidecar is the only uniform
enrich-wide shape (RESEARCH sec 1).

Structure (mirrors ``dedup_resolution``'s create-table shape from 032 sec B):
* ``id`` UUID PK, ``file_id`` UUID FK -> ``files.id`` (NOT unique alone), ``stage`` String,
  ``reason`` Text (D-09 required), ``skipped_at`` TIMESTAMPTZ default now, + ``created_at`` /
  ``updated_at`` (TimestampMixin).
* ``uq_stage_skip_file_stage`` UNIQUE(file_id, stage) -- the <=1-row-per-(file, stage) invariant
  (D-13a, T-87-03).
* ``ck_stage_skip_enrich_only`` CHECK ``stage IN ('metadata','analyze','fingerprint')`` (D-10, OQ-3,
  T-87-02) -- approval/execute can never carry a skip marker at the schema layer.

NO backfill: this is a greenfield marker -- there is NO historical "force-skipped" source to derive
from (unlike 032's ``_BACKFILL_*`` blocks). The table starts empty.

Bare constraint names via ``op.f(...)``: the ``pk_``/``uq_``/``fk_``/``ck_`` naming convention re-applies
the prefix, so ``op.f()`` marks each name as final (passing an already-prefixed name to a
convention-applied name double-prefixes it -- 032:66-67 warning). The ORM ``__table_args__`` in
``models/stage_skip.py`` mirrors these names byte-for-byte (the empty-autogenerate-diff contract).

CRITICAL: this migration must NEVER reference the SAQ-owned jobs table (SAQ owns it via ``init_db`` +
``saq_versions``; an Alembic migration touching it would collide -- 020/031/032 CRITICAL banner).

``downgrade()`` is a plain mirrored ``drop_table`` -- additive greenfield table, no data-loss concern
for the existing corpus (no backfill; T-87-04 accept).

Revision ID: 037
Revises: 036
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "037"
down_revision: str | Sequence[str] | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the additive ``stage_skip`` marker sidecar (no backfill -- greenfield marker)."""
    op.create_table(
        "stage_skip",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("skipped_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stage_skip")),
        sa.UniqueConstraint("file_id", "stage", name=op.f("uq_stage_skip_file_stage")),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name=op.f("fk_stage_skip_file_id_files")),
        sa.CheckConstraint("stage IN ('metadata','analyze','fingerprint')", name=op.f("ck_stage_skip_enrich_only")),
    )


def downgrade() -> None:
    """Mirrored reversal -- drop the additive greenfield table (no backfill to reverse)."""
    op.drop_table("stage_skip")
