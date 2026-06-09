"""Backfill scan_batches.completed_at for terminal rows that never stamped it.

Data-only migration closing the NULL-completed_at gap behind incident
260608/260609. Two row populations are affected:

  1. Rows that reached a terminal (completed/failed) state *before* the
     completed_at column existed (added in migration 015). Migration 015
     deliberately did not backfill, so these rows carry completed_at IS NULL.
  2. Rows written by the legacy ``services.ingestion.run_scan`` path, which
     transitioned to COMPLETED/FAILED without stamping completed_at (fixed in
     the same PR).

For every such terminal row we set ``completed_at = updated_at``. ``updated_at``
is the natural freeze point: the TimestampMixin bumps it via ``onupdate`` on the
status transition, so it is the closest recorded approximation of the true
completion instant. After this backfill the admin UI's ``elapsed_seconds`` timer
freezes (terminal rows stop tracking the wall clock).

RUNNING/LIVE (non-terminal) rows are intentionally untouched -- their elapsed
timer must keep ticking against ``now``.

Revision ID: 016
Revises: 015
Create Date: 2026-06-09
"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "016"
down_revision: str | Sequence[str] | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Set completed_at = updated_at for terminal rows with a NULL completed_at.

    Raw SQL only -- no model imports, no untrusted input (static literals), so
    there is no injection surface (threat T-PR2-02: accept). Only rows whose
    status is terminal AND whose completed_at is currently NULL are touched.
    """
    op.execute("UPDATE scan_batches SET completed_at = updated_at WHERE status IN ('completed', 'failed') AND completed_at IS NULL")


def downgrade() -> None:
    """No-op: a data backfill of this shape is not reversibly undoable.

    The migration cannot know which terminal rows were originally NULL versus
    legitimately stamped (by the agent PATCH path or the run_scan fix), so it
    has no safe way to restore the prior NULLs. Re-NULLing every terminal
    completed_at would corrupt correctly-completed rows. The forward backfill is
    idempotent and harmless, so leaving the data in place on downgrade is the
    only non-destructive choice.
    """
