"""ANALYZE the tables whose filter columns can carry no planner statistics (phaze-3agnm).

``ALTER TABLE ... ADD COLUMN`` writes no rows, so it does not count toward
``n_mod_since_analyze``. A column added to a table that sees no further writes is therefore
never analyzed: autoanalyze never fires, ``pg_stats`` holds no row for it, and the planner
falls back to default selectivity for every predicate on it. Measured read-only on host-prod
(2026-09-23, 11,428 ``metadata`` rows, ``last_autoanalyze`` 2026-07-08,
``n_mod_since_analyze`` 0): no ``pg_stats`` row for ``metadata.failed_at``, and
``failed_at IS NULL`` was estimated at 57 rows against a real 11,428. That misestimate drove
two per-row nested loops on every ``/s/analyze`` render and ``/pipeline/stats`` poll: the
proposals-convergence count in ``get_stage_progress`` and the metadata orphan split. The
operator had ``ANALYZE metadata`` run once on host-prod on 2026-09-23 (bead comment on
phaze-3agnm). This migration is the durable version for any other database with the same
history.

Tables analyzed, each because a column lacking statistics there is used in a filter:

* ``metadata`` -- ``failed_at IS NULL`` / ``IS NOT NULL`` in the stage-progress and orphan
  queries. ``error_message`` shares the table and is read, never filtered.
* ``cloud_job`` -- ``telemetry_slot IS NOT NULL`` (065) in
  ``burst_telemetry_slots.allocate_slot``.

Not analyzed: ``scan_batches.configured_root`` (064) is never filtered on ``scan_batches``;
the orphan-diagnostic filter reads ``orphan_companion_diagnostic.configured_root``, a table
064 created and autoanalyze covers as rows arrive.

Safe on any database. ANALYZE reads a sample and writes only ``pg_statistic``; it changes no
row and no schema. It runs inside the migration's transaction, which PostgreSQL allows
(unlike VACUUM), and takes SHARE UPDATE EXCLUSIVE, which conflicts with neither reads nor
writes. On an empty table it records nothing and costs nothing.

The downgrade is a deliberate no-op. Statistics are not schema, a downgraded schema reads
them exactly as before, and discarding them would re-create the misestimate this fixes.

Revision ID: 068
Revises: 067
Create Date: 2026-09-23
"""

from collections.abc import Sequence

from alembic import op


revision: str = "068"
down_revision: str | None = "067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Collect planner statistics for tables whose filter columns were added without any."""
    op.execute("ANALYZE public.metadata")
    op.execute("ANALYZE public.cloud_job")


def downgrade() -> None:
    """No-op: statistics are not schema, and dropping them would restore the misestimate."""
