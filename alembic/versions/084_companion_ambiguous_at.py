"""Persist confirmed-absent companions with ambiguous destinations (phaze-st1ty).

Nullable, no default, no inventory rewrite. Missing and ambiguity remain separate from extraction
coverage. Reappearance clears both markers; only an explicit reconciliation sets ambiguity.

Revision ID: 084
Revises: 083
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "084"
down_revision: str | None = "083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable companion ambiguity marker and refresh filter statistics."""
    op.add_column("files", sa.Column("companion_ambiguous_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("ANALYZE files")


def downgrade() -> None:
    """Drop the companion ambiguity marker."""
    op.drop_column("files", "companion_ambiguous_at")
