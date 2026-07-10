"""Backfill analysis_completed_at for the analyzed corpus so the reenqueue cutover does not re-analyze it (Phase 80, READ-03, D-13).

One-shot, DATA-ONLY backfill. It stamps ``analysis.analysis_completed_at = analysis.updated_at`` for
every ``state='analyzed'`` file whose ``analysis_completed_at`` is still NULL. This is the BLOCKING
prerequisite of the reenqueue cutover (Plan 80-04): once ``reenqueue.py`` derives ``done(analyze)`` via
``done_clause(ANALYZE)`` (which requires ``analysis_completed_at IS NOT NULL`` -- ``stage_status.py:123``)
the ~1001 production ``analyzed`` rows that currently hold NULL would otherwise be judged NOT-done and
re-enqueued for 4-hour re-analysis (the 44.5K-job over-enqueue incident class). Backfilling the marker
makes those rows correctly domain-complete so the cutover leaves them alone.

``analysis_completed_at`` has existed since ``028`` (``models/analysis.py:38``); the go-forward writer
(Phase 57.1 ``put_analysis`` completion branch) only ever stamped it for rows analyzed AFTER 028, so the
pre-028 corpus and any partial-in-flight rows carry NULL. This migration repairs the EXISTING corpus.

The source column is ``a.updated_at`` (``AnalysisResult`` carries it via ``TimestampMixin``). The exact
value is immaterial -- ``done_clause(ANALYZE)`` tests only ``IS NOT NULL``; ``updated_at`` is simply the
most defensible timestamp already on the row (last write, i.e. when analysis last completed).

Contract (D-13):
  * SYNC migration -- plain ``def upgrade()`` / ``op.execute(...)``; only ``env.py`` is async.
  * STATIC, parameter-free SQL -- no interpolation, no f-string, no ``.format``, no model import. The
    ``'analyzed'`` FileState value is a fixed literal, so there is no injection surface (016/032/034/035
    precedent).
  * Touches NO ORM-mapped schema -- ``analysis_completed_at`` shipped in ``028`` -- so
    ``alembic revision --autogenerate`` against the ``036`` head stays EMPTY.
  * The ``AND a.failed_at IS NULL`` guard is MANDATORY. ``033``'s constraint is a NAND, not an XOR
    (``NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)`` -- ``models/analysis.py:56``):
    without the guard the UPDATE would stamp ``analysis_completed_at`` on an ``analysis_failed`` row that
    already carries ``failed_at``, violating the CHECK and aborting the whole migration. The guard also
    preserves the done-over-failed precedence (a failed row stays NULL-completed).
  * IDEMPOTENT -- the ``analysis_completed_at IS NULL`` predicate makes a second run a no-op (every
    already-stamped row is excluded); the statement is set-based and naturally re-runnable.
  * ``files.state`` is byte-unchanged -- ``036`` NEVER writes ``files`` (it is READ-only on ``files.state``).

``downgrade()`` is a documented NO-OP: this is a data-only backfill of a column that predates this
migration, so pre-existing NULLs are indistinguishable from backfilled values and there is nothing to
reverse (mirrors 032's set-based-backfill no-op downgrade precedent, and 035's pure-reconcile no-op).

CRITICAL: this migration must NEVER touch SAQ's job table (SAQ owns it -- 020/031/032 banner). This
migration source deliberately contains no such reference (acceptance criterion #4).

Revision ID: 036
Revises: 035
Create Date: 2026-07-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "036"
down_revision: str | Sequence[str] | None = "035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Static string literal -- no interpolation, no model import: the 'analyzed' FileState value is a fixed
# literal, so there is no injection surface (016/032/034/035 precedent). The ``a.failed_at IS NULL`` guard
# is MANDATORY -- 033's constraint is a NAND, not an XOR (models/analysis.py:56); without it the UPDATE
# would stamp analysis_completed_at on an already-failed row and abort the migration on the CHECK. The
# ``analysis_completed_at IS NULL`` predicate makes the backfill idempotent (a re-run stamps nothing).
_BACKFILL_ANALYSIS_COMPLETED_AT = """
UPDATE analysis a
SET analysis_completed_at = a.updated_at
FROM files f
WHERE a.file_id = f.id
  AND f.state = 'analyzed'
  AND a.analysis_completed_at IS NULL
  AND a.failed_at IS NULL
"""


def upgrade() -> None:
    """Stamp ``analysis_completed_at`` for every ``analyzed`` file still holding NULL, NAND-guarded (D-13)."""
    op.execute(sa.text(_BACKFILL_ANALYSIS_COMPLETED_AT))


def downgrade() -> None:
    """No-op (documented choice, D-13).

    ``036`` is a pure data backfill of ``analysis_completed_at``, a column that predates this migration
    (shipped in ``028``). A downgrade has no safe target: pre-existing NULLs are indistinguishable from
    values this migration backfilled, so blanking ``analysis_completed_at`` for the ``analyzed`` corpus
    would also destroy go-forward completion timestamps written by ``put_analysis``. Leaving the column
    untouched keeps the corpus in its most-consistent state (mirrors 032's set-based-backfill no-op
    downgrade precedent, and 035's pure-reconcile no-op).
    """
