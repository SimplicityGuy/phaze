"""Create ``tracklist_lookup_cache`` -- the persisted positive/negative 1001TL lookup cache (phaze-fq9h.3).

The 1001Tracklists drain is capped by an external politeness ceiling (robots.txt crawl-delay 8,
applied per HOST) at ~10,800 requests/day for the entire system -- ~4,300 lookups. It therefore
runs for months, and it restarts. This table is what stops a restart from re-asking questions the
system has already paid for, and it is the storage half of the acceptance criterion "the cache
prevents re-querying a set (positive or negative) on re-run".

``outcome`` is a string, not a boolean, and that is the whole design: "1001TL genuinely has no
tracklist for this set" and "a Turnstile interstitial survived our retry loop" are both "no
tracklist" and must NEVER share a representation. Caching the second as the first would silently
remove sets from the queue for the negative TTL because of a flaky browser -- data loss with no
error and no way to notice. See ``models/tracklist_lookup_cache.py`` for the full argument.

Pure additive DDL: one new table, no changes to any existing one, nothing to backfill (an empty
cache is exactly correct -- it means "nothing has been looked up yet"). ``downgrade`` drops it,
which loses the memory of past lookups but costs only budget, never a tracklist: every positive
result is independently persisted in ``tracklists`` / ``tracklist_versions``, and this table is a
skip-list over that, not a store of record.

The table is created with a plain (non-CONCURRENT) ``CREATE TABLE`` and its indexes inline: a
brand-new empty table has no readers to block, so the 048 CONCURRENTLY dance -- which exists
because ``files`` is the largest table in the deployment and cannot take an ACCESS EXCLUSIVE lock
during a build -- buys nothing here.

Revision ID: 050
Revises: 049
Create Date: 2026-08-02
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None

_TABLE = "tracklist_lookup_cache"
_OUTCOME_EXPIRES_INDEX = "ix_tracklist_lookup_cache_outcome_expires_at"


def upgrade() -> None:
    """Create the lookup-cache table and its sweep index."""
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        # UNIQUE, not merely indexed: one row per unique set is the invariant the whole cache
        # rests on, and it is also the ON CONFLICT target the resumable drain's upsert needs --
        # two workers racing the same key after a restart must collapse to one row, not raise and
        # roll back a result that cost one of the day's requests.
        sa.Column("set_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column("query_text", sa.Text(), nullable=False),
        # No CHECK constraint on outcome, matching the tracklists.source precedent: the outcome
        # taxonomy tracks a third-party site's failure modes and will grow. A CHECK would make
        # each new failure mode a migration while adding no integrity the service layer (which
        # maps unknown values to "re-query", the safe side) does not already provide.
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=50), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("result_confidence", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_attempted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        # NULL means "never expires" and is reserved for positives: a past event's tracklist does
        # not change. For NOT_FOUND it is the negative TTL; for a transient failure it is a short
        # backoff. Same column, two meanings, disambiguated by `outcome` -- documented on the model.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    # The drain's hot path ("may I skip these keys?") rides the UNIQUE on set_key. This index
    # serves the opposite direction -- the maintenance/report sweeps for expired negatives and
    # retry-ready transients -- over a table that ends up with one row per unique set in a
    # ~250,000-file archive, where a seq scan per sweep is not free.
    op.create_index(_OUTCOME_EXPIRES_INDEX, _TABLE, ["outcome", "expires_at"])


def downgrade() -> None:
    """Drop the lookup-cache table (index goes with it)."""
    op.drop_index(_OUTCOME_EXPIRES_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
