"""Drop the never-populated ``analysis.fingerprint`` column (phaze-s7mb).

**Schema hygiene, NOT fingerprint-engine removal.** This column is unrelated to the
audfprint/Panako fingerprinting that phaze-0jpe removed (runtime in phaze-0jpe.2, schema in
migration 046): it predates both engines, dating to the original Phase 01-02 ``analysis`` table,
and no code path in the project's history ever wrote to it. The removed engines persisted their
results to ``fingerprint_results`` (dropped by 046), never here. The phaze-0jpe closeout sweep
flagged this column precisely because it merely *shares a name* with the removed feature —
dropping it now stops that name from misleading a future reader into thinking phaze-0jpe left
residue behind.

**Populated-state check (the bead's acceptance requires verifying, not assuming):** verified
against the live database 2026-07-29 — ``SELECT count(*), count(fingerprint) FROM analysis``
returned 2,747 rows, 0 non-null. ``upgrade()`` re-verifies at run time in ONLINE mode: it counts
non-null values and aborts (raising, applying nothing) if any exist, so a surprise-populated
database — a restored backup with out-of-band writes, say — fails loudly instead of silently
discarding data. In OFFLINE (``--sql``) mode there is no live connection to count against, so the
rendered SQL is the unconditional ``DROP COLUMN`` alone; that is safe to run against any database
that satisfies the verified precondition above, and an operator rendering offline SQL for some
other database is explicitly on the hook for that check (this asymmetry is stated here rather
than hidden — mirrors 046's stance of documenting what offline mode can and cannot do).

**Downgrade — schema-symmetric, trivially data-complete:** ``downgrade()`` recreates the column
as nullable ``TEXT`` in its original spot semantically (Postgres appends columns; ordering is
cosmetic and no code reads positionally). Unlike 046's data-irreversible drops there is no data
to lose here — the column has never held a value — so downgrade-then-upgrade round-trips cleanly.

**Offline (``--sql``) mode:** the DDL statements are static string literals with no bound
parameters, so they render identically online and offline (the 039-baseline parameterized-loop
trap phaze-0r9a found does not apply). The online-only count guard uses the live connection and
is skipped in offline rendering, as described above.

Revision ID: 047
Revises: 046
Create Date: 2026-07-29
"""

import sqlalchemy as sa

from alembic import context, op


# revision identifiers, used by Alembic.
revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None

_DROP_ANALYSIS_FINGERPRINT = "ALTER TABLE public.analysis DROP COLUMN fingerprint"
_ADD_ANALYSIS_FINGERPRINT = "ALTER TABLE public.analysis ADD COLUMN fingerprint text"


def upgrade() -> None:
    """Drop ``analysis.fingerprint``, verifying online that it is still unpopulated first."""
    if not context.is_offline_mode():
        populated = op.get_bind().execute(sa.text("SELECT count(fingerprint) FROM public.analysis")).scalar_one()
        if populated:
            msg = (
                f"analysis.fingerprint unexpectedly holds {populated} non-null value(s); refusing to drop. "
                "This column was verified never-populated (2026-07-29: 2,747 rows, 0 non-null) and no code "
                "writes it — investigate where these values came from before re-running this migration."
            )
            raise RuntimeError(msg)
    op.execute(_DROP_ANALYSIS_FINGERPRINT)


def downgrade() -> None:
    """Recreate ``analysis.fingerprint`` as nullable text (schema-symmetric; no data ever existed to restore)."""
    op.execute(_ADD_ANALYSIS_FINGERPRINT)
