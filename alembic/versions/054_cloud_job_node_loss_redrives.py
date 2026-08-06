"""Add ``cloud_job.node_loss_redrives`` -- the independent node-loss re-drive budget (phaze-1q4g).

``cloud_job.attempts`` is the file's ANALYZE retry budget, capped by ``cloud_submit_max_attempts``.
A re-drive taken because the pod died WITH ITS NODE deliberately does not charge it (an
infrastructure fault is not the file's fault) -- but "not charged" had silently become "not
bounded", and one pathological file used that to produce eight pods over five days against a cap of
three, crashing the burst node on every one (spike ``phaze-wcrb`` §5).

This column is that path's OWN counter, bounded by its OWN knob (``cloud_node_loss_max_redrives``).
Keeping it separate rather than folding node loss into ``attempts`` preserves the distinction the
row is supposed to record: ``attempts=3 / node_loss_redrives=0`` is a file that failed analysis three
times; ``attempts=0 / node_loss_redrives=1`` is a file that lost a node once. Total pods per row is
now bounded by ``1 + cloud_submit_max_attempts + cloud_node_loss_max_redrives``.

Pure additive DDL: one NOT NULL column with ``server_default '0'`` on an existing table, so every
live row backfills to "no node-loss re-drive taken yet" -- which is the truth for every row that
predates the counter (nothing was ever counted, so nothing can be reconstructed, and 0 is the
conservative value: it grants each existing row its full node-loss budget rather than retroactively
spending one). The server_default is RETAINED (not dropped after backfill) so a plain
``INSERT INTO cloud_job (...)`` that omits the column -- e.g. the ``hold_awaiting_cloud`` upsert's
INSERT arm -- keeps working unchanged. ``downgrade`` drops the column, losing only the counter.

Revision ID: 054
Revises: 053
Create Date: 2026-08-06
"""

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None

_TABLE = "cloud_job"
_COLUMN = "node_loss_redrives"


def upgrade() -> None:
    """Add the NOT NULL ``node_loss_redrives`` counter, defaulting existing + future rows to 0."""
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    """Drop the counter. Loses only the node-loss re-drive tally; no other column depends on it."""
    op.drop_column(_TABLE, _COLUMN)
