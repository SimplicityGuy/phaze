"""Add ``companion_content_features`` -- what each companion file contains, read on its agent (phaze-osy6j).

One row per companion ``files`` row: detected encoding, the media references it names (with their
source), the tracklist flag, the per-file and effective junk class, the content fingerprint that
groups identical copies, and the media names of the folder it sat in. The rules behind every column
are ``docs/spikes/phaze-lm73u-companion-content-survey.md``; the writer is
``routers/agent_companion_features.py``.

No backfill here: only the agent can read the files. ``phaze backfill companion-features`` (dry run
by default) covers the rows that exist before the agents report features at ingest.

Revision ID: 079
Revises: 078
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "079"
down_revision: str | None = "078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "companion_content_features"


def upgrade() -> None:
    """Create the per-companion content-features sidecar."""
    op.create_table(
        _TABLE,
        # file_id IS the PK. ON DELETE CASCADE so this sidecar can never block a scan or file deletion
        # (stage_skip's lesson); services/scan_deletion.py deletes it explicitly as well.
        sa.Column("file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("files.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("encoding", sa.String(length=16), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("media_references", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("reference_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_tracklist", sa.Boolean(), nullable=False),
        sa.Column("content_junk_class", sa.String(length=16), nullable=True),
        sa.Column("junk_class", sa.String(length=16), nullable=True),
        sa.Column("folder_media", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("folder_media_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extractor_version", sa.SmallInteger(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        # BARE constraint names: the ck naming convention adds the table prefix (phaze-x8tof).
        sa.CheckConstraint(
            "encoding IN ('ascii', 'utf-8', 'utf-8-sig', 'utf-16', 'utf-16-le', 'cp437', 'cp1252', 'all-nul')",
            name="known_encoding",
        ),
        sa.CheckConstraint(
            "content_junk_class IS NULL OR content_junk_class IN ('empty', 'all_nul', 'site_ad')",
            name="known_content_junk_class",
        ),
        sa.CheckConstraint(
            "junk_class IS NULL OR junk_class IN ('empty', 'all_nul', 'known_stamp', 'site_ad')",
            name="known_junk_class",
        ),
        sa.CheckConstraint("byte_size >= 0 AND reference_count >= 0 AND folder_media_count >= 0", name="counts_nonneg"),
    )
    op.create_index("ix_companion_content_features_agent_id_fingerprint", _TABLE, ["agent_id", "fingerprint"])


def downgrade() -> None:
    """Drop the sidecar; the companions' files rows are untouched."""
    op.drop_index("ix_companion_content_features_agent_id_fingerprint", table_name=_TABLE)
    op.drop_table(_TABLE)
