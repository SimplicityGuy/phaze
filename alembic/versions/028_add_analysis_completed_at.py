"""Add the ``analysis_completed_at`` completion discriminator to ``analysis`` (Phase 57.1, D-03 KEY RISK).

Additive-only, reversible migration. Adds one nullable timestamp column to the ``analysis`` table:

* ``analysis_completed_at`` (``DateTime(timezone=True)``, NULL) -- stamped via ``func.now()`` ONLY in
  the existing ``put_analysis`` completion branch (the same dumped-guarded txn that flips
  ``FileState.ANALYZED``). An in-flight/partial analysis row (D-03 upserts one at analysis START)
  leaves it NULL, so "row exists" is no longer a sound "analysis complete" proxy.

Why this column exists (the KEY RISK): under Phase 57.1 D-03 an ``analysis`` row can be upserted at
analysis START while the file is still ``METADATA_EXTRACTED``. The proposal convergence gate
(``get_proposal_pending_batches``) previously treated bare ``exists(AnalysisResult)`` as "analysis
complete" -- which a partial START row would satisfy, leaking NULL aggregates into proposals. This
column is the completion discriminator the tightened gate requires (``analysis_completed_at IS NOT NULL``).

A plain nullable column needs NO CHECK constraint (unlike 027's string-backed enum).

CRITICAL: this migration touches ONLY the ``analysis`` table.
It must never reference ``saq_jobs`` (SAQ owns that table via init_db + saq_versions; an Alembic
migration touching it would collide -- 020 CRITICAL banner).

Revision ID: 028
Revises: 027
Create Date: 2026-06-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "028"
down_revision: str | Sequence[str] | None = "027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable analysis_completed_at timestamp column to ``analysis``."""
    op.add_column("analysis", sa.Column("analysis_completed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the analysis_completed_at column."""
    op.drop_column("analysis", "analysis_completed_at")
