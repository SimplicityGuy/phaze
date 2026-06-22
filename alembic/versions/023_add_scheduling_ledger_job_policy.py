"""Add timeout + retries columns to scheduling_ledger (recover-button policy preservation).

Additive-only migration. Adds two nullable integer columns to the existing
``scheduling_ledger`` table so the ``before_enqueue`` WRITE hook can capture each job's
effective SAQ ``timeout`` / ``retries`` policy and recovery can replay the SAME bound.

Why: pre-023 recovery replayed only the stored payload, so a recovered ``process_file``
(analyze) job lost its 7200s outer net and fell back to the 600s ``worker_job_timeout``
default -- a 12x reduction that timed out every long concert set the moment the "Recover
orphaned work" button touched it. The columns are NULLABLE: an existing row (or a producer
that set no explicit policy) stays NULL and replay omits the kwarg, so the queue's
``apply_project_job_defaults`` default applies exactly as before (backward compatible).

Columns:
  - timeout : nullable INTEGER, the SAQ Job ``timeout`` (seconds) at enqueue time.
  - retries : nullable INTEGER, the SAQ Job ``retries`` budget at enqueue time.

CRITICAL: like 022, this migration touches ONLY ``scheduling_ledger``.
It must never reference ``saq_jobs`` (SAQ owns that table via init_db + saq_versions).

Revision ID: 023
Revises: 022
Create Date: 2026-06-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "023"
down_revision: str | Sequence[str] | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``timeout`` / ``retries`` integer columns."""
    op.add_column("scheduling_ledger", sa.Column("timeout", sa.Integer(), nullable=True))
    op.add_column("scheduling_ledger", sa.Column("retries", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the two columns (mirror of upgrade)."""
    op.drop_column("scheduling_ledger", "retries")
    op.drop_column("scheduling_ledger", "timeout")
