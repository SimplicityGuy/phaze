"""Persist the cloud Job's start time for the current attempt.

Revision ID: 076
Revises: 075
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "076"
down_revision: str | None = "075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
ANALYZE_EXEMPT_TABLES = {
    "cloud_job": "started_at is display-only; no filter, join, or sort reads it",
}


def upgrade() -> None:
    """Add a nullable start time; reconcile fills existing active Jobs from Kubernetes."""
    op.add_column("cloud_job", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Remove the current-attempt start time."""
    op.drop_column("cloud_job", "started_at")
