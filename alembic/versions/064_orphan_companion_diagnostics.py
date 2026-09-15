"""Persist scan-owned orphan companion diagnostics.

Revision ID: 064
Revises: 063
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "064"
down_revision: str | None = "063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Preserve configured-root identity and add the metadata-only sidecar."""
    op.add_column("scan_batches", sa.Column("configured_root", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE scan_batches SET configured_root = scan_path WHERE configured_root IS NULL"))
    op.alter_column("scan_batches", "configured_root", existing_type=sa.Text(), nullable=False)

    op.create_table(
        "orphan_companion_diagnostics",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("normalized_path", sa.Text(), nullable=False),
        sa.Column("configured_root", sa.Text(), nullable=False),
        sa.Column("companion_extension", sa.String(length=5), nullable=False),
        sa.CheckConstraint(
            "companion_extension IN ('.cue', '.m3u', '.m3u8', '.nfo', '.pls', '.txt')",
            name="accepted_extension",
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["scan_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("batch_id", "normalized_path"),
    )
    op.create_index(
        "ix_orphan_companion_diagnostics_batch_root_path",
        "orphan_companion_diagnostics",
        ["batch_id", "configured_root", "normalized_path"],
    )
    op.create_index(
        "ix_orphan_companion_diagnostics_batch_extension_path",
        "orphan_companion_diagnostics",
        ["batch_id", "companion_extension", "normalized_path"],
    )


def downgrade() -> None:
    """Remove the diagnostic sidecar and configured-root identity."""
    op.drop_index("ix_orphan_companion_diagnostics_batch_extension_path", table_name="orphan_companion_diagnostics")
    op.drop_index("ix_orphan_companion_diagnostics_batch_root_path", table_name="orphan_companion_diagnostics")
    op.drop_table("orphan_companion_diagnostics")
    op.drop_column("scan_batches", "configured_root")
