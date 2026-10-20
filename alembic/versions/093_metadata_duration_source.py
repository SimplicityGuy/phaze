"""Record when a stored metadata duration was recomputed from the real MPEG frames (phaze-v03x9).

``metadata.duration_source`` is NULL for every existing row (the tag reader's own length, which is
what they hold, read as legacy/tags), ``tags``, and ``audio_frames`` for a duration recomputed behind a junk
prefix. A CHECK constraint keeps the set closed (the implementer's choice, not the operator's).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "093"
down_revision: str | None = "092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ANALYZE_EXEMPT_TABLES = {
    "metadata": "duration_source is display-only (it picks a label); no filter, join, or sort reads it, so planner statistics are unnecessary",
}


def upgrade() -> None:
    op.add_column("metadata", sa.Column("duration_source", sa.Text(), nullable=True))
    op.create_check_constraint("duration_source", "metadata", "duration_source IN ('tags', 'audio_frames')")


def downgrade() -> None:
    op.drop_constraint("duration_source", "metadata", type_="check")
    op.drop_column("metadata", "duration_source")
