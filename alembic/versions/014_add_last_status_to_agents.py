"""Add agents.last_status JSONB column and partial token-hash index.

Revision ID: 014
Revises: 013
Create Date: 2026-05-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "014"
down_revision: str | Sequence[str] | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add last_status JSONB + partial index on token_hash WHERE revoked_at IS NULL."""
    # 1. JSONB column - pattern from migration 012:38 (scan_roots JSONB)
    # Nullable; no backfill needed (legacy agent never heartbeats - per phase-24 D-06 + phase-25 D-07).
    op.add_column("agents", sa.Column("last_status", postgresql.JSONB, nullable=True))

    # 2. Partial index for the only query the auth dep ever runs:
    #    SELECT * FROM agents WHERE token_hash = $1 AND revoked_at IS NULL
    # Predicate literal `revoked_at IS NULL` MUST match `Agent.revoked_at.is_(None)`
    # byte-for-byte so Postgres uses the partial index for the auth lookup.
    # Pattern verbatim from migration 012:104-110 (uq_scan_batches_agent_id_live);
    # difference: non-unique (this is a lookup index, not a uniqueness constraint).
    op.create_index(
        "ix_agents_token_hash_active",  # ix_ prefix per base.py:9 convention dict
        "agents",
        ["token_hash"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    """Drop partial index and last_status column.

    No data-loss guard required: legacy agent never heartbeats so last_status
    is always NULL for the only row that pre-dates this migration.
    """
    op.drop_index("ix_agents_token_hash_active", table_name="agents")
    op.drop_column("agents", "last_status")
