"""Add the ``backend_breaker`` table -- the per-backend control-plane-unreachable breaker (phaze-j0ixx).

On 2026-09-26/27 two infrastructure faults (the app host's outage, then the burst node's cluster DNS
losing its upstream) made every burst pod fail its presign request, and reconcile charged each of
those failures against the file's cloud ``attempts`` -- 302 healthy files spent their whole budget
(spike ``phaze-79mu7``). The pod now exits ``EXIT_CONTROL_PLANE_UNREACHABLE`` (14) for that case,
reconcile charges nothing for it, and this table holds the per-backend breaker that stops the drain
dispatching into the fault: one row per backend id, written only once that backend has reported the
control plane unreachable.

The breaker's trip rule counts ``cloud_job`` rows by ``last_exit_code`` / ``last_failed_at`` -- the
first query that filters on those migration-069 columns -- so this also ANALYZEs ``cloud_job`` rather
than leaving the planner on default selectivity for them (the phaze-gx8p2 lesson).

Pure additive DDL. ``downgrade`` drops the table, which closes every breaker: the drain then
dispatches to every backend exactly as it did before this revision.

Revision ID: 071
Revises: 070
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "071"
down_revision: str | None = "070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "backend_breaker"


def upgrade() -> None:
    """Create the breaker table (no rows: every backend starts closed) and ANALYZE ``cloud_job``."""
    op.create_table(
        _TABLE,
        sa.Column("backend_id", sa.String(255), primary_key=True),
        sa.Column("tripped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trip_reason", sa.Text(), nullable=True),
        sa.Column("next_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        # NOT NULL + timestamptz, matching TimestampMixin exactly (migration 055's precedent).
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.execute(sa.text("ANALYZE cloud_job"))


def downgrade() -> None:
    """Drop the table. Every breaker closes; no budget counter or cloud_job status is touched."""
    op.drop_table(_TABLE)
