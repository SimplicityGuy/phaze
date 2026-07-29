"""Add the ``files (original_filename, id)`` btree the tag-write review keyset paging needs (phaze-bto9).

``services.review.get_tagwrite_review_page`` keyset-pages the applied-file candidate set with
``ORDER BY original_filename, id`` plus a ``(original_filename, id) > (:last_name, :last_id)`` range
predicate. No index could serve either half: every index on ``files`` before this migration is
``ix_files_sha256_hash`` (btree on one unrelated column), ``uq_files_agent_id_original_path`` (btree
on a different leading column), ``ix_files_search_vector`` (GIN) and ``ix_files_filename_trgm``
(GIN trgm). A GIN trigram index supports similarity/containment lookups; it cannot produce an
ordered scan and cannot answer a row-tuple range.

The practical consequence at the project's stated 200K design scale: EACH of the ~400 keyset batches
re-evaluated the correlated ``applied_clause()`` EXISTS plus the terminal-``TagWriteLog`` anti-join
over the whole table and re-SORTED the result, to return 500 rows. That made the paging itself the
dominant cost of the workspace render -- so batching the per-row lookups (the other half of
phaze-bto9) without this index would have left the scan quadratic and the page still unusable.

Index-only DDL; no data migration, no application-visible schema change. Built ``CONCURRENTLY`` with
an autocommit connection because ``files`` is the largest table in the deployment and a plain
``CREATE INDEX`` takes an ACCESS EXCLUSIVE lock for its whole duration, blocking every agent file
upsert. ``IF NOT EXISTS`` makes a re-run after an interrupted build (which can leave the index
INVALID) a no-op rather than an error -- **but a no-op is the bug** (phaze-44sj): an INVALID index
still costs write amplification while serving zero reads, and the operator was left to run the
downgrade's manual ``DROP INDEX CONCURRENTLY`` before upgrade would do anything. ``upgrade()`` now
self-heals: ONLINE, it checks ``pg_index.indisvalid`` for this index by name via ``to_regclass``
(NULL-safe -- a first-ever run where the index does not exist yet skips straight to CREATE) and
drops an INVALID leftover before (re)creating it, so re-running ``upgrade`` after an interrupted
build heals it without operator intervention. OFFLINE (``--sql``) mode has no connection to check
against, so it renders the unconditional CREATE alone, matching the pre-fix behavior there.

Revision ID: 048
Revises: 047
Create Date: 2026-07-28
"""

import sqlalchemy as sa

from alembic import context, op


# revision identifiers, used by Alembic.
revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None

# Static string-literal DDL -- no interpolation, no user input reaches this SQL. Keep all three
# statements as PLAIN literals (the index name spelled out in each): an f-string here, even one
# interpolating a module-level constant, trips semgrep's formatted-sql-query /
# sqlalchemy-execute-raw-query blocking rules in CI.
_CREATE_INDEX = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_files_original_filename_id ON public.files USING btree (original_filename, id)"
_DROP_INDEX = "DROP INDEX CONCURRENTLY IF EXISTS public.ix_files_original_filename_id"
# to_regclass(...) is NULL when the index doesn't exist yet, and the WHERE comparison against a
# NULL indexrelid then matches no pg_index row -- so this returns NULL (falsy), not an error, on
# a first-ever run. Only an existing-but-INVALID index (an interrupted CONCURRENTLY build) is truthy.
_CHECK_INVALID = "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass('public.ix_files_original_filename_id')"


def upgrade() -> None:
    """Create the (original_filename, id) btree concurrently, self-healing an INVALID leftover first.

    CREATE INDEX CONCURRENTLY cannot run inside a transaction block. Alembic wraps each migration
    in one by default, so drop to an autocommit connection for these statements.
    """
    with op.get_context().autocommit_block():
        if not context.is_offline_mode() and op.get_bind().execute(sa.text(_CHECK_INVALID)).scalar():
            # An interrupted prior build left this index INVALID -- drop it so the CREATE below
            # actually rebuilds it instead of no-opping on IF NOT EXISTS.
            op.execute(_DROP_INDEX)
        op.execute(_CREATE_INDEX)


def downgrade() -> None:
    """Drop the index concurrently.

    Operational note: this is no longer the only recovery path for an INTERRUPTED concurrent build
    (``upgrade`` now self-heals an INVALID leftover automatically, see the module docstring), but
    remains available to drop the index outright without rebuilding it.
    """
    with op.get_context().autocommit_block():
        op.execute(_DROP_INDEX)
