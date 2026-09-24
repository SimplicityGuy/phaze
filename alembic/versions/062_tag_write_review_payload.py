"""Persist the reviewed tag payload and source versions.

Revision ID: 062
Revises: 061
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "062"
down_revision: str | None = "061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# phaze-gx8p2: reviewed_before_tags and review_source_versions are never filtered in a WHERE
# clause -- read/written only by tag_writer.py and routers/tags.py, keyed on the log row's own
# primary/foreign keys. No planner misestimate is possible for a column nothing filters on.
ANALYZE_EXEMPT_TABLES = {
    "tag_write_log": (
        "reviewed_before_tags and review_source_versions are never filtered in a WHERE clause "
        "-- read/written only keyed on the row's own keys, never as a predicate."
    ),
}


def upgrade() -> None:
    empty = sa.text("'{}'::jsonb")
    op.add_column("tag_write_log", sa.Column("reviewed_before_tags", postgresql.JSONB(astext_type=sa.Text()), server_default=empty, nullable=False))
    op.add_column("tag_write_log", sa.Column("review_source_versions", postgresql.JSONB(astext_type=sa.Text()), server_default=empty, nullable=False))


def downgrade() -> None:
    op.drop_column("tag_write_log", "review_source_versions")
    op.drop_column("tag_write_log", "reviewed_before_tags")
