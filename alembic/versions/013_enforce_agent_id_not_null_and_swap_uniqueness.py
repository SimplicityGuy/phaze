"""Enforce NOT NULL on agent_id columns and swap files unique constraint to composite.

Revision ID: 013
Revises: 012
Create Date: 2026-05-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "013"
down_revision: str | Sequence[str] | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enforce NOT NULL on agent_id columns and swap files unique constraint to composite."""
    # 1. SET NOT NULL on both agent_id columns (D-13: 012 backfill guarantees every row has a value)
    op.alter_column("files", "agent_id", nullable=False, existing_type=sa.String(64))
    op.alter_column("scan_batches", "agent_id", nullable=False, existing_type=sa.String(64))

    # 2. Drop legacy single-column uniqueness from migration 002
    op.drop_index("uq_files_original_path", table_name="files")

    # 3. Create composite UQ: same original_path under different agent_id is now allowed (D-15 leading column)
    op.create_index(
        "uq_files_agent_id_original_path",
        "files",
        ["agent_id", "original_path"],
        unique=True,
    )


def downgrade() -> None:
    """Reverse 013: dupe-check first, then swap unique back to single column and relax NOT NULL.

    D-16: downgrade fails LOUDLY if the same original_path now lives under multiple agents.
    Silent dedup is forbidden -- this is an irreplaceable personal music collection.
    The operator must resolve duplicates manually before retrying the downgrade.
    """
    # Dupe-detection guard MUST run BEFORE any DDL so the error aborts pre-mutation.
    bind = op.get_bind()
    dupes = bind.execute(sa.text("SELECT original_path FROM files GROUP BY original_path HAVING COUNT(*) > 1 LIMIT 5")).scalars().all()
    if dupes:
        raise RuntimeError(
            "Cannot downgrade 013->012: original_path is no longer unique across agents. "
            f"Example collisions: {dupes!r}. "
            "Resolve manually before retrying. Silent dedup is FORBIDDEN per phase-24 D-16."
        )

    op.drop_index("uq_files_agent_id_original_path", table_name="files")
    op.create_index("uq_files_original_path", "files", ["original_path"], unique=True)
    op.alter_column("scan_batches", "agent_id", nullable=True, existing_type=sa.String(64))
    op.alter_column("files", "agent_id", nullable=True, existing_type=sa.String(64))
