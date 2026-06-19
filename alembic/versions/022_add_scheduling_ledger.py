"""Add scheduling_ledger table (durable "was scheduled" record for orphan recovery).

Additive-only migration for Phase 45. Creates the new standalone app table
``scheduling_ledger`` -- one row per keyed enqueue, written at the single
``before_enqueue`` chokepoint and cleared on completion / terminal failure. Recovery
re-queues exactly ``ledger - live broker keys``, so never-scheduled work (a
``DISCOVERED`` file awaiting a manual DAG trigger) is left alone -- the missing fact
behind the 2026-06-18 over-enqueue incident.

Columns:
  - key         : PK, the deterministic ``"<function>:<natural_id>"`` dedup key.
  - function    : the task name to re-enqueue.
  - routing     : ``"agent"`` | ``"controller"`` replay hint.
  - payload     : JSONB, the FULL original ``job.kwargs`` to replay.
  - enqueued_at : timestamp of the (re)enqueue.
  - created_at / updated_at : TimestampMixin columns.

A plain index on ``function`` supports per-stage diagnostics. NO foreign keys: a ledger
row must survive even if its target file/tracklist row is mid-flight (the natural id lives
inside ``payload``).

CRITICAL: this migration touches ONLY ``scheduling_ledger``. It must NEVER reference saq_jobs
-- SAQ owns that table via its own ``init_db()`` + ``saq_versions`` and an Alembic migration
touching it would collide (020 CRITICAL banner, 45-RESEARCH §3).

No data migration -- this migration only creates the new app table; the live-broker
backfill is a one-time idempotent startup reconcile (Plan 04), NOT a DDL data step.

Revision ID: 022
Revises: 021
Create Date: 2026-06-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "022"
down_revision: str | Sequence[str] | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create scheduling_ledger with a PK on ``key`` and an index on ``function``."""
    op.create_table(
        "scheduling_ledger",
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("function", sa.String(64), nullable=False),
        sa.Column("routing", sa.String(16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_scheduling_ledger")),
    )
    op.create_index("ix_scheduling_ledger_function", "scheduling_ledger", ["function"])


def downgrade() -> None:
    """Drop scheduling_ledger and its function index (mirror of upgrade)."""
    op.drop_index("ix_scheduling_ledger_function", table_name="scheduling_ledger")
    op.drop_table("scheduling_ledger")
