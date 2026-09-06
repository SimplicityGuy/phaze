"""Add the set projection: per-window energy/camelot/mood_scores and the set_profile table.

phaze-x1qr3.1, the schema half of section E's "one projection, then everything rides it".
See ``models/set_profile.py`` and ``models/analysis.py``'s ``AnalysisWindow`` docstring for the
field-by-field rationale, and ``services/set_projection.MOOD_ORDER`` for the 11 fixed names and
the fixed archive-wide order ``mood_scores`` and ``mean_vector`` share.

PURELY ADDITIVE, AND DELIBERATELY SO. Three NULLABLE columns on ``analysis_window`` plus one new
table. No existing row is rewritten and no existing read changes: a nullable column with no
DEFAULT is a catalogue-only ``ADD COLUMN`` in Postgres 11+, and there is no CHECK constraint here
to force a validating scan over what is already millions of window rows. Every new column reads
NULL until ``phaze-x1qr3.3`` backfills it FROM STORED JSONB -- no file is re-analyzed by this
epic, and a NULL is the honest "not yet projected", which the lanes must render as a gap rather
than as a zero.

Revision ID: 063
Revises: 062
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "063"
down_revision: str | None = "062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WINDOW_TABLE = "analysis_window"
_PROFILE_TABLE = "set_profile"
# The three per-window projection columns, in the order they are added and the REVERSE of the
# order they are dropped -- named once so upgrade and downgrade cannot drift apart.
_WINDOW_COLUMNS = ("energy", "camelot", "mood_scores")


def upgrade() -> None:
    """Add the per-window projection columns, then create the per-file profile table."""
    op.add_column(_WINDOW_TABLE, sa.Column("energy", sa.Float(), nullable=True))
    op.add_column(_WINDOW_TABLE, sa.Column("camelot", sa.String(length=3), nullable=True))
    op.add_column(_WINDOW_TABLE, sa.Column("mood_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_table(
        _PROFILE_TABLE,
        # file_id IS the primary key: 1:1 with files, so no surrogate id and no separate unique
        # index. ON DELETE CASCADE so this sidecar can never block a scan deletion -- the same
        # reason `analysis_window` carries one and is therefore absent from the explicit ordered
        # delete list in services/scan_deletion.py.
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        # float8[]. Postgres does not enforce array length, so mean_vector's 11 and arc's 64 are
        # writer contracts (carried by MOOD_ORDER's own length and the resample width), not DDL.
        sa.Column("mean_vector", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("arc", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("glyph", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # Same width as analysis_window.camelot -- "8A", "12B".
        sa.Column("camelot_modal", sa.String(length=3), nullable=True),
        sa.Column("harmonic_discipline", sa.Float(), nullable=True),
        sa.Column("peak_sec", sa.Float(), nullable=True),
        # The only NOT NULL column besides the key: a row exists because the projection ran, so
        # there is no "no version" state. Lets a weight change re-backfill only stale rows.
        sa.Column("projection_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("file_id"),
    )


def downgrade() -> None:
    """Drop the profile table, then the per-window projection columns.

    Loses only projected values: every byte they were derived from still sits in
    ``analysis_window.features``, so a re-upgrade plus the phaze-x1qr3.3 backfill reconstructs
    this schema's entire contents with no re-analysis.
    """
    op.drop_table(_PROFILE_TABLE)
    for column in reversed(_WINDOW_COLUMNS):
        op.drop_column(_WINDOW_TABLE, column)
