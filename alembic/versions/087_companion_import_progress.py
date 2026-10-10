"""Durable acquisition attempts and resumable bounded companion imports."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op


revision: str = "087"
down_revision: str | None = "086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_provider_objects_source_file_id", "provider_source_objects", ["source_file_id"])
    op.create_table(
        "provider_acquisition_attempts",
        sa.Column("attempt_id", sa.UUID(), primary_key=True),
        sa.Column("source_object_id", sa.UUID(), sa.ForeignKey("provider_source_objects.id"), nullable=False),
        sa.Column("observation_id", sa.UUID(), sa.ForeignKey("provider_source_observations.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("code", sa.String(4096), nullable=False),
        sa.Column("revision", sa.String(2048)),
        sa.Column("revision_scope", sa.String(16), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("freshness", sa.String(32), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("envelope", JSONB(), nullable=False),
        sa.UniqueConstraint("source_object_id", "ordinal"),
    )
    op.create_index("ix_provider_acquisition_attempts_source_ordinal", "provider_acquisition_attempts", ["source_object_id", "ordinal"])
    op.create_table(
        "companion_import_runs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("continuation", sa.UUID(), nullable=False),
        sa.Column("request_key", sa.String(128), unique=True),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("cursor", sa.UUID()),
        sa.Column("associated", sa.Boolean(), nullable=False),
        sa.Column("enumerated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_companion_import_runs_agent", "companion_import_runs", ["agent_id", "created_at"])
    op.create_table(
        "companion_import_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("run_id", sa.UUID(), sa.ForeignKey("companion_import_runs.id"), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=False),
        sa.Column("expected_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("lease_token", sa.UUID()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("observation_id", sa.UUID(), sa.ForeignKey("provider_source_observations.id")),
        sa.Column("target_cursor", sa.UUID()),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True)),
        sa.Column("had_incomplete", sa.Boolean(), nullable=False),
        sa.Column("tracklist_status", sa.String(32)),
        sa.Column("release_status", sa.String(32)),
        sa.Column("had_unresolved", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.UniqueConstraint("run_id", "file_id"),
    )
    op.create_index("ix_companion_import_items_run_state", "companion_import_items", ["run_id", "state", "id"])
    op.execute(
        "CREATE FUNCTION reject_provider_acquisition_attempt_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'provider acquisition attempts are immutable'; END $$"
    )
    op.execute(
        "CREATE TRIGGER provider_acquisition_attempts_immutable BEFORE UPDATE OR DELETE ON provider_acquisition_attempts FOR EACH ROW EXECUTE FUNCTION reject_provider_acquisition_attempt_mutation()"
    )


def downgrade() -> None:
    used = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM provider_acquisition_attempts) OR EXISTS(SELECT 1 FROM companion_import_runs) OR EXISTS(SELECT 1 FROM companion_import_items)"
        )
    )
    if used:
        raise RuntimeError("Companion acquisition/progress evidence exists; lossy downgrade refused")
    op.execute("DROP TRIGGER provider_acquisition_attempts_immutable ON provider_acquisition_attempts")
    op.execute("DROP FUNCTION reject_provider_acquisition_attempt_mutation()")
    op.drop_table("companion_import_items")
    op.drop_table("companion_import_runs")
    op.drop_table("provider_acquisition_attempts")
    op.drop_index("ix_provider_objects_source_file_id", table_name="provider_source_objects")
