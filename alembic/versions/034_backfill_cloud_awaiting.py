"""Repair backfill: gap-fill the missing awaiting cloud_job rows for the held corpus (Phase 83, SIDECAR-01, D-04).

One-shot, DATA-ONLY repair. It re-runs ``032``'s ``_BACKFILL_CLOUD_AWAITING`` statement VERBATIM
(``032:96-102``): an ``INSERT .. SELECT gen_random_uuid(), f.id, 'awaiting' FROM files WHERE
state='awaiting_cloud' ON CONFLICT (file_id) DO NOTHING``. Since ``032``, the go-forward writer of
``cloud_job.status='awaiting'`` did not exist (83-CONTEXT D-01): ``trigger_analysis`` held long files
with a bare ``file.state = AWAITING_CLOUD`` and never imported ``CloudJob``. Every file parked at
``state='awaiting_cloud'`` since ``032`` therefore carries NO sidecar row, violating the HARD shadow
invariant ``AWAITING_CLOUD ⇒ cloud_job(status='awaiting')`` (``services/shadow_compare.py:131``).

``034`` repairs the EXISTING corpus so the invariant holds for all held files; the go-forward writer
(83-01/83-05) fixes NEW holds. Sequenced in Wave 1 so it lands BEFORE the drain-candidate cutover
(83-06) reads the sidecar — otherwise every already-held file strands.

Contract (D-04):
  * SYNC migration — plain ``def upgrade()`` / ``op.execute(...)``; only ``env.py`` is async.
  * STATIC, parameter-free SQL — no interpolation, no f-string, no ``.format``, no model import. The
    ``'awaiting'`` literal and the ``awaiting_cloud`` FileState value are fixed, so there is no
    injection surface (016/032 precedent).
  * Touches NO ORM-mapped schema — the ``'awaiting'`` CHECK value and ``ix_cloud_job_awaiting`` partial
    index already shipped in ``032`` (``models/cloud_job.py:114,122``; ``032:144-156``). No
    ``__table_args__`` mirroring is needed and ``alembic revision --autogenerate`` stays EMPTY (77 D-01
    empty-diff contract holds trivially).
  * IDEMPOTENT — ``ON CONFLICT (file_id) DO NOTHING`` + ``uq_cloud_job_file_id`` make a double-run inert
    and leave any pre-existing row (a live hold, or a cloud row at another status) UNCHANGED.

``downgrade()`` is a documented-LOSSY ``DELETE FROM cloud_job WHERE status = 'awaiting'``: it cannot
distinguish rows this migration repaired from live go-forward holds written after it ran, so it removes
BOTH. This mirrors the best-effort-DDL / non-reversed-backfill precedent (016/032/033 downgrades).

CRITICAL: this migration must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/031/032 banner).

Revision ID: 034
Revises: 033
Create Date: 2026-07-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "034"
down_revision: str | Sequence[str] | None = "033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Re-run of 032's _BACKFILL_CLOUD_AWAITING (032:96-102), verbatim. Static string literal -- no
# interpolation, no model import: the 'awaiting' status and the 'awaiting_cloud' FileState value are
# fixed literals, so there is no injection surface (016/032 precedent). ON CONFLICT (file_id) DO NOTHING
# + uq_cloud_job_file_id make the backfill idempotent and non-clobbering of any pre-existing row.
_BACKFILL_CLOUD_AWAITING = """
INSERT INTO cloud_job (id, file_id, status)
SELECT gen_random_uuid(), f.id, 'awaiting'
FROM files f
WHERE f.state = 'awaiting_cloud'
ON CONFLICT (file_id) DO NOTHING
"""

# Documented-LOSSY downgrade: cannot distinguish repaired rows from live go-forward holds -- removes both.
_DOWNGRADE_DELETE_AWAITING = "DELETE FROM cloud_job WHERE status = 'awaiting'"


def upgrade() -> None:
    """Gap-fill an ``awaiting`` cloud_job row for every ``state='awaiting_cloud'`` file missing one (D-04)."""
    op.execute(sa.text(_BACKFILL_CLOUD_AWAITING))


def downgrade() -> None:
    """Delete every ``awaiting`` cloud_job row (LOSSY -- it cannot tell repaired rows from live holds).

    This removes rows written by the go-forward writer (83-01/83-05) as well as the ones this migration
    repaired; the migration keeps no discriminator between them. Best-effort reversal, matching the
    016/032/033 non-reversed-backfill precedent.
    """
    op.execute(sa.text(_DOWNGRADE_DELETE_AWAITING))
