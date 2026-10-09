"""Remove retired acquisition state; preserve stored tracklists and their provenance.

Revision ID: 082
Revises: 081
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "082"
down_revision: str | None = "081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop acquisition-only state and stop defaulting new tracklists to the retired provider."""
    op.drop_table("tracklist_file_lookups")
    op.drop_table("tracklist_priority_flags")
    op.drop_table("tracklist_drain_arm_state")
    op.drop_table("tracklist_lookup_cache")
    op.alter_column("tracklists", "source", server_default="manual")


def downgrade() -> None:
    """Restore empty acquisition tables, disarmed; removed lookup history is not recoverable."""
    op.alter_column("tracklists", "source", server_default="1001tracklists")
    op.create_table(
        "tracklist_lookup_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("set_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=50), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("result_confidence", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_attempted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_tracklist_lookup_cache_outcome_expires_at", "tracklist_lookup_cache", ["outcome", "expires_at"])
    op.create_table(
        "tracklist_priority_flags",
        sa.Column(
            "file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE", name="fk_tracklist_priority_flags_file_id_files"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_table(
        "tracklist_drain_arm_state",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("armed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("armed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disarmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disarmed_reason", sa.String(length=32), nullable=True),
        sa.Column("consecutive_failures", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("in_flight", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("slice_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(f"id = '{'tracklist_drain'}'", name="singleton"),
    )
    op.execute(sa.text("INSERT INTO tracklist_drain_arm_state (id, armed) VALUES (:id, false)").bindparams(id="tracklist_drain"))
    op.create_table(
        "tracklist_file_lookups",
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("set_key", sa.String(length=64), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name="fk_tracklist_file_lookups_file_id_files", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("file_id", name="pk_tracklist_file_lookups"),
    )
    op.add_column("tracklist_lookup_cache", sa.Column("retry_url", sa.Text(), nullable=True))
