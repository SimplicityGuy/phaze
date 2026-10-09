"""Add nullable ``files.missing_at`` -- a row whose file is gone from its agent's disk (phaze-5rfev).

``phaze backfill reconcile-stale-rows`` stamps it on a row whose ``current_path`` no longer exists
and whose content (SHA-256) is nowhere under the agent's scan roots. The row is kept -- its
proposals, analysis, metadata and audit rows stay -- and it leaves the enrich pending and retry sets
so it stops failing on every "Extract all". Any upsert of the same path clears it.

A nullable column with no default is a catalog-only change in Postgres (no table rewrite, no
backfill). ``missing_at IS NULL`` is a filter on every enrich pending set, so the upgrade also
``ANALYZE``s ``files`` (the migration ``068`` pattern, guarded by
``tests/shared/test_migration_add_column_analyze_guard.py``): an added column carries no planner
statistics until something analyzes the table. ANALYZE samples and writes only ``pg_statistic``,
runs inside the migration's transaction, and takes SHARE UPDATE EXCLUSIVE, which blocks neither
reads nor writes. Downgrade drops the column.

Revision ID: 083
Revises: 082
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "083"
down_revision: str | None = "082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable missing marker."""
    op.add_column("files", sa.Column("missing_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("ANALYZE files")


def downgrade() -> None:
    """Drop the missing marker."""
    op.drop_column("files", "missing_at")
