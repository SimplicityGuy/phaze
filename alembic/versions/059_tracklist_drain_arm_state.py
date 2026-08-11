"""Create ``tracklist_drain_arm_state`` -- the durable operator ARM/DISARM flag (phaze-6nrrf).

``tasks.tracklist_drain.drain_tracklists`` keeps dispensing one bounded (<=100 lookup) slice per
enqueue, unchanged. This table lets the operator ARM the drain once so a CronJob
(``continue_armed_tracklist_drain``) re-enqueues the next slice after each one finishes, instead of
requiring a click per slice -- see ``models.tracklist_drain_arm_state`` for the full field-by-field
rationale.

Pure additive DDL: one new table, no changes to any existing one. The single seed row is inserted
with ``armed = false`` -- DEFAULT OFF is the safety invariant this migration exists to establish:
a fresh deploy, or a deploy that predates this bead, never starts crawling on its own. Everything
else on the row is left at its column default / NULL.

Revision ID: 059
Revises: 058
Create Date: 2026-08-11
"""

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None

_TABLE = "tracklist_drain_arm_state"
_SINGLETON_ID = "tracklist_drain"


def upgrade() -> None:
    """Create the arm-state table and seed its single, disarmed row."""
    op.create_table(
        _TABLE,
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
        # Bare name -- alembic's op.create_table builds against the SAME naming_convention as the
        # ORM (models/base.py), so a pre-prefixed name here would double-prefix exactly like it
        # would in a model (phaze-x8tof / migration 056's whole reason for existing).
        sa.CheckConstraint(f"id = '{_SINGLETON_ID}'", name="singleton"),
    )
    # Literal SQL + bound param (the migration-039 seed pattern) rather than an f-string, which
    # trips Semgrep's avoid-sqlalchemy-text even with constant inputs.
    op.execute(sa.text("INSERT INTO tracklist_drain_arm_state (id, armed) VALUES (:id, false)").bindparams(id=_SINGLETON_ID))


def downgrade() -> None:
    """Drop the arm-state table. Loses only the operator's live armed/disarmed intent -- every
    tracklist the drain already wrote lives in ``tracklists`` / ``tracklist_lookup_cache`` and is
    untouched by this."""
    op.drop_table(_TABLE)
