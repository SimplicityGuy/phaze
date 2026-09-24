"""Add ``cloud_job.telemetry_slot`` -- the burst lane's controller-allocated telemetry identity (phaze-w15ju).

A burst-lane analysis runs in a one-shot Kueue pod that is Postgres-less and shares no memory with
its peers, so nothing inside the pod can allocate a telemetry worker slot against the up-to-``cap``
pods running beside it. Every burst pod therefore exported under the same ``service.instance.id``,
and a collector's Prometheus exporter keeps ONE series per identity and takes the last write:
measured against the real collector, two producers under one identity produced a DECREASING
cumulative counter and an ``increase()`` of 590 where 320 was delivered (+84.4%, growing to +221.5%
over twice as many exports) -- ``docs/telemetry/concurrent-identity.md``.

The only seat that can see every competitor is the CONTROLLER, at submit time, which already
enforces the per-backend cap from these same rows. This column is where that decision survives a
controller restart: without it a restarted controller would re-allocate from an empty view and hand
a second pod a slot the first is still exporting under -- the exact merge the slot exists to prevent.

**Occupancy is DERIVED from ``status``, so nothing releases a slot.**
``phaze.services.burst_telemetry_slots.allocate_slot`` reads the held set as the slots on other
``cloud_job`` rows whose ``status`` is in the in-flight set {uploading, uploaded, submitted, running}.
A row that goes succeeded/failed, or is spilled back to 'awaiting' by the reconcile cron or by
``KueueBackend._reap_stranded_staging``, frees its slot at that instant with no writer involved --
which is how a crashed or evicted pod cannot permanently shrink the pool.

Pure additive DDL: one NULLABLE integer column with no default, plus a non-negativity CHECK. Every
live row backfills to NULL, which is exactly right -- no slot was ever allocated for a row that
predates the column, and NULL is the value ``allocate_slot`` treats as "needs one". The UPPER bound
is intentionally NOT in the CHECK: it is the sum of the configured kueue backends' ``cap`` values,
which lives in ``backends.toml`` and must be raisable without a migration. ``downgrade`` drops both,
losing only the slot assignment of a Job already in flight -- whose pod already holds its slot in
its own environment, so the loss is a possible duplicate identity on the NEXT submit, never a
corrupted row.

Revision ID: 065
Revises: 064
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "065"
down_revision: str | None = "064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "cloud_job"
_COLUMN = "telemetry_slot"
# **DECLARE THE BARE NAME, exactly as the ORM does -- in BOTH directions.** ``Base.metadata``'s
# naming convention (``models/base.py``) renders ``ck_%(table_name)s_%(constraint_name)s``, and
# alembic builds the throwaway MetaData behind ``op.create_check_constraint`` AND behind
# ``op.drop_constraint`` from that same convention. A pre-prefixed name therefore gets the prefix a
# second time on the way in (migration 056 exists to repair five constraints that did exactly that)
# and, more quietly, on the way OUT: a ``downgrade`` naming the rendered
# ``ck_cloud_job_telemetry_slot_nonnegative`` tries to drop
# ``ck_cloud_job_ck_cloud_job_telemetry_slot_nonnegative`` and fails with UndefinedObject, breaking
# the round-trip. The database carries ``ck_cloud_job_telemetry_slot_nonnegative``; this constant
# stays bare.
_CHECK = "telemetry_slot_nonnegative"

# phaze-gx8p2: telemetry_slot IS NOT NULL is the exact filter migration 068 (phaze-3agnm) fixed
# the statistics for, via `ANALYZE public.cloud_job` -- see that migration's docstring, which
# names this column and its query (services/burst_telemetry_slots.allocate_slot) directly.
ANALYZE_EXEMPT_TABLES = {
    _TABLE: (
        "telemetry_slot IS NOT NULL is the exact filter migration 068 (phaze-3agnm) fixed the "
        "statistics for, via `ANALYZE public.cloud_job` -- see that migration's docstring."
    ),
}


def upgrade() -> None:
    """Add the NULLABLE slot column and its non-negativity CHECK; every existing row backfills to NULL."""
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))
    op.create_check_constraint(_CHECK, _TABLE, f"{_COLUMN} IS NULL OR {_COLUMN} >= 0")


def downgrade() -> None:
    """Drop the CHECK and the column. Loses only an in-flight Job's recorded slot, never a budget counter."""
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
