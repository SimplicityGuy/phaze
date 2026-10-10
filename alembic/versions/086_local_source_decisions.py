"""Record fresh association derivation and immutable target review evidence."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op


revision: str = "086"
down_revision: str | None = "085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Historical links/choices remain unknown; no production inference or backfill."""
    op.add_column("file_companions", sa.Column("derivation_method", sa.String(32), nullable=True))
    op.add_column("file_companions", sa.Column("derivation_revision", sa.String(64), nullable=True))
    op.add_column("file_companions", sa.Column("derivation_evidence", JSONB(), nullable=True))
    op.add_column("file_companions", sa.Column("derived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("provider_recording_selections", sa.Column("selection_token", sa.UUID(), nullable=True))
    op.add_column("provider_recording_selections", sa.Column("target_mapping", JSONB(), nullable=True))
    op.add_column("provider_selection_events", sa.Column("decision_id", sa.UUID(), nullable=True))
    op.add_column("provider_selection_events", sa.Column("decision_payload", JSONB(), nullable=True))
    op.add_column("provider_selection_events", sa.Column("action", sa.String(16), nullable=True))
    op.add_column("provider_selection_events", sa.Column("target_mapping", JSONB(), nullable=True))
    op.create_unique_constraint("uq_provider_selection_events_decision_id", "provider_selection_events", ["decision_id"])
    op.execute(
        "CREATE FUNCTION reject_provider_selection_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'provider selection events are immutable'; END $$"
    )
    op.execute(
        "CREATE TRIGGER provider_selection_events_immutable BEFORE UPDATE OR DELETE ON provider_selection_events FOR EACH ROW EXECUTE FUNCTION reject_provider_selection_event_mutation()"
    )
    op.execute("ANALYZE public.file_companions")
    op.execute("ANALYZE public.provider_recording_selections")
    op.execute("ANALYZE public.provider_selection_events")


def downgrade() -> None:
    """Only an unused expansion is reversible; reviewed evidence must survive."""
    used = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM provider_selection_events WHERE decision_id IS NOT NULL OR decision_payload IS NOT NULL OR action IS NOT NULL OR target_mapping IS NOT NULL) OR EXISTS (SELECT 1 FROM provider_recording_selections WHERE selection_token IS NOT NULL OR target_mapping IS NOT NULL) OR EXISTS (SELECT 1 FROM file_companions WHERE derived_at IS NOT NULL OR derivation_method IS NOT NULL OR derivation_revision IS NOT NULL OR derivation_evidence IS NOT NULL)"
        )
    )
    if used:
        raise RuntimeError("Local source review evidence exists; lossy downgrade refused")
    op.execute("DROP TRIGGER provider_selection_events_immutable ON provider_selection_events")
    op.execute("DROP FUNCTION reject_provider_selection_event_mutation()")
    op.drop_constraint("uq_provider_selection_events_decision_id", "provider_selection_events", type_="unique")
    for column in ("target_mapping", "action", "decision_payload", "decision_id"):
        op.drop_column("provider_selection_events", column)
    for column in ("target_mapping", "selection_token"):
        op.drop_column("provider_recording_selections", column)
    for column in ("derived_at", "derivation_evidence", "derivation_revision", "derivation_method"):
        op.drop_column("file_companions", column)
