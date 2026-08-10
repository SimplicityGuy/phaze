"""Add ``scheduling_ledger.redrive_attempt`` dedicated re-drive counter (phaze-2jl1 / phaze-y0j0).

The ``push_file`` (push-mismatch) and ``s3_upload`` (S3-upload-failure) re-drive loops kept their
bounded attempt counter inside the ledger ``payload`` JSONB. But the single ``before_enqueue`` WRITE
hook (``apply_deterministic_key``) upserts ``payload`` WHOLESALE on every re-drive enqueue, from its
OWN short-lived session, committing BEFORE the handler stamps the incremented counter. A crash (deploy
restart / OOM) in that window durably left the row with a counter-less payload -- the counter was not
merely un-incremented, it was RESET to 0, silently restarting the bounded push/upload budget the loop
exists to enforce.

Moving the counter to a DEDICATED column the hook never writes closes the window: the hook's
ON CONFLICT DO UPDATE set-list excludes this column, so a crash leaves the counter at its prior value
(un-incremented) rather than zeroed. The budget survives.

The column is nullable (NULL == 0, never re-driven). The upgrade backfills it from any existing
``push_attempt`` / ``s3_upload_attempt`` payload key so in-flight budgets carry across the deploy.

Revision ID: 042
Revises: 041
Create Date: 2026-07-22
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None

# Static string-literal DDL -- no interpolation, no user input reaches this SQL.
_ADD_COLUMN = "ALTER TABLE public.scheduling_ledger ADD COLUMN redrive_attempt integer"
# Backfill from whichever counter key the row carried (push_file rows used push_attempt; s3_upload rows
# used s3_upload_attempt). COALESCE picks the present one; rows with neither stay NULL (== 0).
_BACKFILL = (
    "UPDATE public.scheduling_ledger "
    "SET redrive_attempt = COALESCE((payload ->> 'push_attempt')::integer, (payload ->> 's3_upload_attempt')::integer) "
    "WHERE payload ? 'push_attempt' OR payload ? 's3_upload_attempt'"
)
_DROP_COLUMN = "ALTER TABLE public.scheduling_ledger DROP COLUMN redrive_attempt"
# Mirror-image of _BACKFILL: this is a REPRESENTATION migration (the counter moves between two
# storage locations, not an additive column with nothing to move back), so the downgrade must
# write the dedicated column's current value back into the legacy payload key before dropping it
# -- else the DROP silently discards the only live copy of the counter (post-042 code writes
# ONLY redrive_attempt; nothing writes push_attempt/s3_upload_attempt into payload anymore), and
# a rollback resets every in-flight push/upload re-drive budget to zero (phaze-dt2cx). Keyed on
# `function` because that is what pre-042 code branched on to choose which payload key to read.
_BACKFILL_PAYLOAD_FROM_COLUMN = (
    "UPDATE public.scheduling_ledger "
    "SET payload = jsonb_set("
    "  payload,"
    "  CASE WHEN function = 'push_file' THEN '{push_attempt}'::text[] ELSE '{s3_upload_attempt}'::text[] END,"
    "  to_jsonb(redrive_attempt)"
    ") "
    "WHERE redrive_attempt IS NOT NULL"
)


def upgrade() -> None:
    """Add the dedicated ``redrive_attempt`` column and backfill it from the legacy payload keys."""
    op.execute(_ADD_COLUMN)
    op.execute(_BACKFILL)


def downgrade() -> None:
    """Back-fill ``redrive_attempt`` into the legacy payload JSONB keys, then drop the column.

    Without the back-fill, dropping the column destroys the only live copy of the re-drive
    counter (post-042 code never writes the legacy payload keys) and a rollback silently resets
    every in-flight push/upload re-drive budget to zero -- the exact failure mode 042 exists to
    eliminate, reintroduced by its own downgrade (phaze-dt2cx).
    """
    op.execute(_BACKFILL_PAYLOAD_FROM_COLUMN)
    op.execute(_DROP_COLUMN)
