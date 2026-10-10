"""Bind Discogs evidence to a legacy track or immutable provider observation/position."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "088"
down_revision: str | None = "087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Existing UUIDs, accepted links and legacy ownership remain unchanged."""
    op.add_column("discogs_links", sa.Column("source_observation_id", sa.UUID(), nullable=True))
    op.add_column("discogs_links", sa.Column("source_track_position", sa.Integer(), nullable=True))
    op.create_foreign_key(
        None,
        "discogs_links",
        "provider_source_observations",
        ["source_observation_id"],
        ["id"],
    )
    op.alter_column("discogs_links", "track_id", existing_type=sa.UUID(), nullable=True)
    op.create_check_constraint(
        "discogs_track_binding",
        "discogs_links",
        "(track_id IS NOT NULL AND source_observation_id IS NULL AND source_track_position IS NULL) OR "
        "(track_id IS NULL AND source_observation_id IS NOT NULL AND source_track_position IS NOT NULL AND source_track_position > 0)",
    )
    op.create_index("ix_discogs_links_source_track", "discogs_links", ["source_observation_id", "source_track_position"])
    op.create_index(
        "ix_discogs_links_one_accepted_per_source_track",
        "discogs_links",
        ["source_observation_id", "source_track_position"],
        unique=True,
        postgresql_where=sa.text("status = 'accepted' AND source_observation_id IS NOT NULL"),
    )
    op.execute("ANALYZE public.discogs_links")


def downgrade() -> None:
    """Only an unused expansion is reversible; candidate and accepted evidence survive."""
    used = op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM discogs_links WHERE source_observation_id IS NOT NULL OR source_track_position IS NOT NULL)")
    )
    if used:
        raise RuntimeError("Provider-bound Discogs evidence exists; lossy downgrade refused")
    op.drop_index("ix_discogs_links_one_accepted_per_source_track", table_name="discogs_links")
    op.drop_index("ix_discogs_links_source_track", table_name="discogs_links")
    op.drop_constraint(op.f("ck_discogs_links_discogs_track_binding"), "discogs_links", type_="check")
    op.alter_column("discogs_links", "track_id", existing_type=sa.UUID(), nullable=False)
    op.drop_constraint(op.f("fk_discogs_links_source_observation_id_provider_source_observations"), "discogs_links", type_="foreignkey")
    op.drop_column("discogs_links", "source_track_position")
    op.drop_column("discogs_links", "source_observation_id")
