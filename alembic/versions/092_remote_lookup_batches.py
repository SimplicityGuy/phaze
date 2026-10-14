"""Durable remote lookup batches and bounded continuation evidence."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op


revision: str = "092"
down_revision: str | None = "091"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remote_lookup_batches",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("providers", JSONB(), nullable=False),
        sa.Column("cursor", sa.UUID()),
        sa.Column("upper_bound", sa.UUID()),
        sa.Column("expanded", sa.Boolean(), nullable=False),
        sa.Column("selected", sa.Integer(), nullable=False),
        sa.Column("existing", sa.Integer(), nullable=False),
        sa.Column("excluded", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "remote_lookup_work",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("media_id", sa.UUID(), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("code", sa.String(64)),
        sa.Column("not_before", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("context_digest", sa.String(64)),
        sa.Column("result", JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "uq_remote_lookup_active_pair",
        "remote_lookup_work",
        ["media_id", "provider_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running','deferred','blocked')"),
    )
    op.create_table(
        "remote_lookup_memberships",
        sa.Column("batch_id", sa.UUID(), sa.ForeignKey("remote_lookup_batches.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("work_id", sa.UUID(), sa.ForeignKey("remote_lookup_work.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "remote_lookup_reads",
        sa.Column("run_id", sa.UUID(), sa.ForeignKey("remote_lookup_work.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("request_hash", sa.String(64), primary_key=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("http_status", sa.Integer()),
    )


def downgrade() -> None:
    op.drop_table("remote_lookup_reads")
    op.drop_table("remote_lookup_memberships")
    op.drop_index("uq_remote_lookup_active_pair", table_name="remote_lookup_work")
    op.drop_table("remote_lookup_work")
    op.drop_table("remote_lookup_batches")
