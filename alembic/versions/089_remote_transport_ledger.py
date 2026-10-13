"""Durable remote transport ledger: per-provider reservations, pacing anchor, Retry-After and stop."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "089"
down_revision: str | None = "088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remote_transport_ledgers",
        sa.Column("provider_id", sa.String(64), primary_key=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("last_reserved_at", sa.DateTime(timezone=True)),
        sa.Column("not_before", sa.DateTime(timezone=True)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("stop_code", sa.String(64)),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        sa.Column("cleared_by", sa.String(128)),
        sa.Column("clear_reason", sa.String(512)),
        sa.CheckConstraint("ordinal >= 0", name="ordinal_nonneg"),
        sa.CheckConstraint("(ordinal = 0) = (last_reserved_at IS NULL)", name="reservation_anchor"),
        sa.CheckConstraint("(stopped_at IS NULL) = (stop_code IS NULL)", name="stop_pair"),
    )
    op.create_table(
        "remote_transport_attempts",
        sa.Column("provider_id", sa.String(64), sa.ForeignKey("remote_transport_ledgers.provider_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_kind", sa.String(16), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("http_status", sa.Integer()),
        sa.Column("outcome", sa.String(64)),
        sa.CheckConstraint("ordinal > 0", name="ordinal_positive"),
        sa.CheckConstraint("request_kind IN ('search', 'fetch', 'robots')", name="request_kind"),
    )
    op.create_index("ix_remote_transport_attempts_window", "remote_transport_attempts", ["provider_id", "reserved_at"])


def downgrade() -> None:
    """Spent budget and stops are evidence; only an unused ledger is reversible."""
    used = op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM remote_transport_ledgers)"))
    if used:
        raise RuntimeError("Remote transport ledger state exists; lossy downgrade refused")
    op.drop_index("ix_remote_transport_attempts_window", table_name="remote_transport_attempts")
    op.drop_table("remote_transport_attempts")
    op.drop_table("remote_transport_ledgers")
