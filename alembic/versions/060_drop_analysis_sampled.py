"""Drop ``analysis.sampled`` -- analysis is exhaustive, nothing is sampled any more (phaze-w55w1).

``sampled`` was the fifth field of the Phase 43 coverage contract (migration 021): True when a
file's natural window count exceeded ``analysis_fine_cap`` / ``analysis_coarse_cap`` and the tier
was strided evenly across the file instead of analyzed window by window. It drove the amber
"Sampled -- more data available" badge and gated the per-file "Deepen analysis" action.

The operator removed the caps on 2026-08-11 (ADR-0007 section 7): every file now receives every
natural window of both tiers, so no analysis can be sampled, and the deepen path that existed to
un-sample one is gone with it. The column can only ever be NULL (new rows) or a historical True/
False that describes a policy the code no longer implements -- exactly the shape that misleads a
future reader, so it is dropped rather than left dark.

HISTORICAL STRIDED FILES STAY IDENTIFIABLE -- verified, not assumed
------------------------------------------------------------------
Dropping ``sampled`` alongside the ``deepen`` action removes both the flag that marked a
partially-analyzed file and the button that re-analyzed one, so the obvious worry is that files
strided under the old caps are stranded with no way to find them. They are not, and the
replacement predicate was checked against the live schema at head rather than reasoned about:

* The four ``*_windows_analyzed`` / ``*_windows_total`` columns are **untouched by this
  migration** and remain ``integer NULL`` after ``upgrade head`` (confirmed by querying
  ``information_schema.columns`` on a freshly migrated database).
* A historically strided row therefore still carries ``fine_windows_analyzed <
  fine_windows_total`` (e.g. 60 < 240) -- the stride wrote the post-stride count into
  ``analyzed`` and the natural count into ``total``, and both survive.
* Rows written before Phase 43 carry NULL in all four. That is correct, not a gap: there were no
  caps before Phase 43, so those analyses were already exhaustive and must NOT be re-run.
* The predicate is a mild SUPERSET -- ``analyzed < total`` is also true where individual windows
  failed to decode. For a one-time re-enqueue that over-selection is desirable (a partially
  failed file is worth re-analyzing too), not a defect.

**The re-run path is bead ``phaze-kj8dl``** -- the sanctioned one-time re-enqueue script plus its
deploy runbook. This migration must ship in the SAME RELEASE as that script's availability: on its
own it removes the label without providing the remedy, and the remedy depends on the columns above
still being readable, which is exactly why they are not dropped here.

The four progress columns are also still WRITTEN and READ on the go-forward path -- they are the
denominators the in-flight bar and the completion PUT share. What changed is their meaning:
``analyzed == total`` on a healthy file, and a shortfall now means individual windows failed to
decode rather than that coverage was deliberately skipped.

Data loss is intentional and bounded to the historical flag; no other column derives from it and
no query outside the removed badge/deepen surface read it. ``downgrade`` re-adds the column as a
nullable boolean, which restores the SHAPE but not the values -- an irreversible-by-nature drop,
declared here rather than pretended otherwise.

Revision ID: 060
Revises: 059
Create Date: 2026-08-11
"""

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop the now-meaningless ``analysis.sampled`` flag."""
    op.drop_column("analysis", "sampled")


def downgrade() -> None:
    """Re-add ``analysis.sampled`` as a nullable boolean (shape only -- values are not recoverable)."""
    op.add_column("analysis", sa.Column("sampled", sa.Boolean(), nullable=True))
