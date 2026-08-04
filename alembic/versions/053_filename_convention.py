"""Create ``filename_convention`` -- the generic corpus-learned convention store (phaze-5fta.2).

The generalized cache the phaze-5fta epic's design calls for: rows keyed
``(scope, scope_value, convention_kind)`` with a learned ``convention_value`` plus evidence
counts (``supporting_count`` / ``contradicting_count`` / ``ambiguous_count``) and a
DB-DERIVED ``confidence``. Date order (DD-MM vs MM-DD) is the first ``convention_kind`` the
learner (phaze-5fta.3) will populate, keyed by ``scope='release_group'`` -- but nothing here
names a date. Future convention kinds (field ordering, separator style, source-tag vocabulary,
episode placement) and future scopes (``directory``, ``agent``, ``global``) are new ROW values,
not new columns or a migration. See ``models/filename_convention.py`` for the full argument,
including why ``confidence`` is a Postgres ``GENERATED ALWAYS AS (...) STORED`` column rather than
an application-set one.

Pure additive DDL: one new table, no changes to any existing one, nothing to backfill (an empty
table is exactly correct -- nothing has been learned yet). ``downgrade`` drops it, which loses
nothing but the cached computation: every row is fully rebuildable from the files corpus by
re-running the learner's full-refresh recompute.

Revision ID: 053
Revises: 052
Create Date: 2026-08-04
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None

_TABLE = "filename_convention"

# Kept byte-identical to ``models.filename_convention.CONFIDENCE_EXPRESSION`` -- the single
# formula the ORM and this migration must never disagree on. supporting share of
# (supporting + contradicting) evidence; 0.0 when there is none yet. ``ambiguous_count`` is
# deliberately excluded -- it sizes the payoff, it is not evidence for or against the convention.
_CONFIDENCE_EXPRESSION = (
    "CASE WHEN (supporting_count + contradicting_count) > 0 "
    "THEN supporting_count::double precision / (supporting_count + contradicting_count) "
    "ELSE 0.0 END"
)


def upgrade() -> None:
    """Create the filename_convention table."""
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        # No enum CHECK on scope/convention_kind -- both taxonomies are expected to grow with new
        # row values (directory/agent/global scopes; field-ordering/separator-style/... kinds),
        # matching the tracklist_lookup_cache.outcome precedent (models/filename_convention.py).
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("scope_value", sa.Text(), nullable=False),
        sa.Column("convention_kind", sa.String(length=64), nullable=False),
        sa.Column("convention_value", sa.Text(), nullable=True),
        sa.Column("supporting_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("contradicting_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ambiguous_count", sa.Integer(), nullable=False, server_default="0"),
        # DERIVED, NEVER HAND-SET: Postgres rejects any INSERT/UPDATE that names this column
        # directly. See models/filename_convention.py for the full argument.
        sa.Column("confidence", sa.Float(), sa.Computed(_CONFIDENCE_EXPRESSION, persisted=True), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint(
            "scope",
            "scope_value",
            "convention_kind",
            name="uq_filename_convention_scope_scope_value_convention_kind",
        ),
        sa.CheckConstraint(
            "supporting_count >= 0 AND contradicting_count >= 0 AND ambiguous_count >= 0",
            name="ck_filename_convention_counts_non_negative",
        ),
    )


def downgrade() -> None:
    """Drop the filename_convention table. Loses only the cached computation -- every row is
    fully rebuildable by re-running the learner's full-refresh recompute against the files
    corpus (phaze-5fta.3)."""
    op.drop_table(_TABLE)
