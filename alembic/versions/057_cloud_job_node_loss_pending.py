"""Add ``cloud_job.node_loss_pending`` -- durable carry for a verdict lost across the re-drive deferral (phaze-mwbz3).

``reconcile_cloud_jobs._handle_no_callback_terminal`` classifies a Failed/Evicted terminal's cause
(ordinary vs. ``PodLiveness.NODE_LOST``) via a per-call, in-memory-only ``node_loss_reason`` argument.
When the prior Job is still terminating after ``delete_job`` the branch defers to a later tick with a
commit that exists ONLY to release the per-row advisory lock (phaze-nq3c) -- no other row mutation ran,
so that classification died with the stack frame. If the deleted Job finished vanishing before the next
tick, ``_reconcile_one`` re-entered through the vanished-Job branch, which has no pods left to classify
and passes no verdict at all -- silently charging the file's ``attempts`` budget (ceiling default 3)
instead of the deliberately tighter ``node_loss_redrives`` (ceiling default 1), recreating the unbounded
node-loss re-drive recurrence phaze-1q4g was built to close (one file, eight pods over five days, taking
the burst node down each time).

This column is the durable copy that closes the gap: the deferral stashes its classified verdict (or
``NULL`` to clear a stale one) here before committing, and the eventual re-drive/spill-to-awaiting reads
it back as a fallback whenever its OWN classification argument is unavailable. Free-text (mirrors
``_terminal_node_loss_reason``'s ``f"node_lost ({...})"`` shape) rather than a boolean so the eventually
charged re-drive's log line still carries the original pod evidence.

Pure additive DDL: one NULLABLE column with no default on an existing table. Every live row backfills to
NULL ("no pending verdict"), which is exactly correct for every row that predates the column -- nothing
was ever stashed, so nothing can be reconstructed, and NULL is the conservative value (it falls through
to the caller's own, possibly-None classification rather than inventing a node-loss verdict that was
never recorded). ``downgrade`` drops the column, losing only an in-flight deferral's stashed verdict --
at worst a single re-drive on an already-in-progress deferral falls back to the pre-fix behaviour.

Revision ID: 057
Revises: 056
Create Date: 2026-08-10
"""

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None

_TABLE = "cloud_job"
_COLUMN = "node_loss_pending"


def upgrade() -> None:
    """Add the NULLABLE ``node_loss_pending`` verdict carry; every existing row backfills to NULL."""
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the carry column. Loses only a verdict stashed mid-deferral, never a spent budget counter."""
    op.drop_column(_TABLE, _COLUMN)
