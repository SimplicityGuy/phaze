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
INVALID) a no-op rather than an error; see the runbook note in ``downgrade``.

Revision ID: 048
Revises: 047
Create Date: 2026-07-28
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None

# Static string-literal DDL -- no interpolation, no user input reaches this SQL.
_CREATE_INDEX = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_files_original_filename_id ON public.files USING btree (original_filename, id)"
_DROP_INDEX = "DROP INDEX CONCURRENTLY IF EXISTS public.ix_files_original_filename_id"


def upgrade() -> None:
    """Create the (original_filename, id) btree concurrently (outside the migration transaction)."""
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block. Alembic wraps each migration
    # in one by default, so drop to an autocommit connection for this statement only.
    with op.get_context().autocommit_block():
        op.execute(_CREATE_INDEX)


def downgrade() -> None:
    """Drop the index concurrently.

    Operational note: an INTERRUPTED concurrent build leaves an INVALID index behind that still
    costs write amplification while serving no reads. ``DROP INDEX CONCURRENTLY IF EXISTS`` cleans
    that up, and re-running ``upgrade`` afterwards rebuilds it -- check
    ``SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid`` if a build was cancelled.
    """
    with op.get_context().autocommit_block():
        op.execute(_DROP_INDEX)
