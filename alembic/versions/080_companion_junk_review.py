"""Add ``companion_junk_review`` -- the junk-companion review queue, audit trail and tombstone (phaze-bk5jp).

One row per proposal to quarantine one companion file, keyed on the natural identity
``(agent_id, original_path, sha256_hash)`` and deliberately free of foreign keys, so scan and file
deletion can neither be blocked by it nor erase it. The design and its evidence are
``docs/spikes/phaze-ib3ub-junk-companion-review.md``; the writer is
``services/companion_junk_review.py``.

No backfill: the detector (``phaze backfill junk-review``) proposes rows from the stored content
features.

Revision ID: 080
Revises: 079
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "080"
down_revision: str | None = "079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "companion_junk_review"


def upgrade() -> None:
    """Create the FK-free junk-review table."""
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("original_path", sa.Text(), nullable=False),
        sa.Column("sha256_hash", sa.String(length=64), nullable=False),
        # Traceability while the files row lives. NO foreign key: the record must outlive the row.
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("file_type", sa.String(length=10), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("content_group", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        # BARE constraint names: the ck naming convention adds the table prefix (phaze-x8tof).
        sa.CheckConstraint("status IN ('approved', 'executing', 'failed', 'pending', 'quarantined', 'rejected')", name="known_status"),
        sa.CheckConstraint("reason IN ('all_nul', 'duplicate', 'empty', 'known_stamp', 'site_ad')", name="known_reason"),
        sa.CheckConstraint("file_type IN ('cue', 'm3u', 'm3u8', 'nfo', 'pls', 'txt')", name="companion_file_type"),
        sa.CheckConstraint("file_size >= 0", name="file_size_nonneg"),
        sa.CheckConstraint("(status = 'pending') = (decided_at IS NULL)", name="decided_at_iff_decided"),
        sa.CheckConstraint("(status IN ('failed', 'quarantined')) = (executed_at IS NOT NULL)", name="executed_at_iff_terminal"),
    )
    op.create_index(
        "uq_companion_junk_review_live_identity",
        _TABLE,
        ["agent_id", "original_path", "sha256_hash"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('failed', 'quarantined')"),
    )
    op.create_index("ix_companion_junk_review_sha256_hash", _TABLE, ["sha256_hash"])
    op.create_index("ix_companion_junk_review_status_content_group", _TABLE, ["status", "content_group"])


def downgrade() -> None:
    """Drop the table; no other table references it."""
    op.drop_index("ix_companion_junk_review_status_content_group", table_name=_TABLE)
    op.drop_index("ix_companion_junk_review_sha256_hash", table_name=_TABLE)
    op.drop_index("uq_companion_junk_review_live_identity", table_name=_TABLE)
    op.drop_table(_TABLE)
