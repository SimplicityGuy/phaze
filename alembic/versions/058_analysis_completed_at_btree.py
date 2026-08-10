"""Add a partial btree ON ``analysis.analysis_completed_at`` for the lane cards' PROCESSED counts (phaze-5c6i2).

The lane cards (``services.backends._lane_processed_counts``) now render a rolling 24h PROCESSED
count per lane (``analysis_completed_at >= :cutoff``) alongside a lifetime total, both gated
``analysis_completed_at IS NOT NULL`` first -- the operator's fourth number, replacing the
misleading ``{in_flight}/{cap}`` numeral (see the bead description). The existing
``ix_analysis_completed`` index (migration 032) is on ``file_id``, existence-scoped by the SAME
predicate -- it answers "does this file have a completed analysis?" but cannot serve an ORDER-BY /
range scan on the completion TIMESTAMP itself, so both counts previously required a sequential scan
of ``analysis`` filtered post-scan.

This index puts the VALUE of ``analysis_completed_at`` in the btree (still partial on
``IS NOT NULL``, so a partial/failed row costs nothing here and the index stays as small as the
completed-row subset), which serves both the windowed range predicate directly and the lifetime
COUNT via an index-only-ish scan. The design's COST note flags ``analysis`` as an unbounded,
growing table on a 5s poll -- this is the cheap-index half of that concern; see
``services.backends._lane_processed_counts`` for the read side and the docstring's note on
promoting the lifetime figure to a cached/slower-cadence read if this index alone is not enough.

Index-only DDL; no data migration, no application-visible schema change. Built CONCURRENTLY with an
autocommit connection, mirroring migration 048's self-healing pattern (``analysis`` grows with every
completed file for the life of the deployment, so a plain ``CREATE INDEX`` -- ACCESS EXCLUSIVE for
its whole duration -- would block every in-flight analysis-completion write for as long as the build
takes). ``IF NOT EXISTS`` + the INVALID self-heal make a re-run after an interrupted build a real
rebuild, not a silent no-op (phaze-44sj, mirrored verbatim from migration 048).

Revision ID: 058
Revises: 057
Create Date: 2026-08-10
"""

import sqlalchemy as sa

from alembic import context, op


# revision identifiers, used by Alembic.
revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None

# Static string-literal DDL -- no interpolation, no user input reaches this SQL (mirrors migration 048).
_CREATE_INDEX = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_analysis_completed_at_when ON public.analysis USING btree (analysis_completed_at) WHERE analysis_completed_at IS NOT NULL"
_DROP_INDEX = "DROP INDEX CONCURRENTLY IF EXISTS public.ix_analysis_completed_at_when"
# NULL-safe: to_regclass(...) is NULL on a first-ever run (index doesn't exist yet), so the WHERE
# comparison matches no pg_index row and this returns NULL (falsy), not an error. Only an
# existing-but-INVALID index (an interrupted CONCURRENTLY build) is truthy.
_CHECK_INVALID = "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass('public.ix_analysis_completed_at_when')"


def upgrade() -> None:
    """Create the partial ``analysis_completed_at`` btree concurrently, self-healing an INVALID leftover first.

    CREATE INDEX CONCURRENTLY cannot run inside a transaction block. Alembic wraps each migration in
    one by default, so drop to an autocommit connection for these statements.
    """
    with op.get_context().autocommit_block():
        if not context.is_offline_mode() and op.get_bind().execute(sa.text(_CHECK_INVALID)).scalar():
            # An interrupted prior build left this index INVALID -- drop it so the CREATE below
            # actually rebuilds it instead of no-opping on IF NOT EXISTS.
            op.execute(_DROP_INDEX)
        op.execute(_CREATE_INDEX)


def downgrade() -> None:
    """Drop the index concurrently."""
    with op.get_context().autocommit_block():
        op.execute(_DROP_INDEX)
