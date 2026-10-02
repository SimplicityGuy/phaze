"""Store host-observed Phaze container versions.

Revision ID: 074
Revises: 073
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "074"
down_revision: str | None = "073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the deployment inventory."""
    op.create_table(
        "deployments",
        sa.Column("container_id", sa.String(64), primary_key=True),
        sa.Column("host", sa.String(128), nullable=False),
        sa.Column("service", sa.String(32), nullable=False),
        sa.Column("role", sa.String(16)),
        sa.Column("lane", sa.String(32)),
        sa.Column("app_version", sa.String(64)),
        sa.Column("image_ref", sa.String(256)),
        sa.Column("image_digest", sa.String(71)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_deployments_host", "deployments", ["host"])


def downgrade() -> None:
    """Remove the deployment inventory."""
    op.drop_index("ix_deployments_host", table_name="deployments")
    op.drop_table("deployments")
