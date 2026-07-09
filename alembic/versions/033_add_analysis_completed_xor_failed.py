"""Enforce analysis_completed_at XOR failed_at at the DB (Phase 81, FAIL-01 / D-06 / D-09).

FAIL-01's analyze failure marker (``analysis.failed_at``, shipped additively by ``032``) must be
mutually exclusive with the ``analysis_completed_at`` completion discriminator: a derived analyze
status can be *done* or *failed*, never both. This migration adds that CHECK.

ORDER IS MANDATORY (D-09). ``032``'s ``_BACKFILL_ANALYZE_FAILED`` is an
``INSERT .. ON CONFLICT (file_id) DO UPDATE`` with NO ``analysis_completed_at IS NULL`` guard, so on
the live corpus it stamped ``failed_at`` onto rows that already carried ``analysis_completed_at``
(a file analyzed successfully, then re-analyzed into ``state='analysis_failed'``). Those *mixed* rows
exist TODAY. ``create_check_constraint`` validates every existing row, so it would abort the whole
migration unless the mixed rows are cleaned first:

  1. ``UPDATE analysis SET failed_at = NULL WHERE analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL``
  2. ``op.create_check_constraint(...)``

The cleanup clears ``failed_at`` and KEEPS ``analysis_completed_at`` -- *done wins* (D-04). That
matches ``_analyze_status``'s done>failed precedence, so no file's derived status changes and the
Phase 79 shadow-compare gate stays green.

``downgrade()`` is DDL-only best-effort reversal (016/032 precedent): it drops the CHECK and does NOT
resurrect the ``failed_at`` values the D-09 cleanup nulled (the migration cannot know which rows were
mixed before it ran).

The bare constraint name ``analysis_completed_xor_failed`` is passed to both
``create_check_constraint`` and ``drop_constraint``: the ``ck_%(table_name)s_%(constraint_name)s``
naming convention (``models/base.py``) re-applies the ``ck_analysis_`` prefix, rendering
``ck_analysis_analysis_completed_xor_failed``. Passing an already-prefixed name double-prefixes it
(same caveat as ``032``'s ``status_enum``).

The CHECK is mirrored into ``AnalysisResult.__table_args__`` so ``alembic revision --autogenerate``
keeps yielding an empty diff (D-06, the 77/PERF-01 empty-diff contract).

CRITICAL: this migration must NEVER reference the SAQ-owned table (020/031/032 CRITICAL banner).

Revision ID: 033
Revises: 032
Create Date: 2026-07-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "033"
down_revision: str | Sequence[str] | None = "032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Bare constraint name -- the ``ck_%(table_name)s_%(constraint_name)s`` naming convention re-applies
# the ``ck_analysis_`` prefix (passing the already-prefixed name double-prefixes it). See 032:66-67.
_CHECK_NAME = "analysis_completed_xor_failed"
_CHECK_SQL = "NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)"

# D-09 mixed-row cleanup. Static SQL literal (no interpolation, no model import). Clears ``failed_at``
# and retains ``analysis_completed_at`` so *done wins* on every mixed row (D-04): no derived status
# flips, no done marker is lost.
_CLEANUP_MIXED_ROWS = "UPDATE analysis SET failed_at = NULL WHERE analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL"


def upgrade() -> None:
    """Clean the mixed rows 032's unguarded backfill produced, THEN add the XOR CHECK (D-09 order)."""
    # (1) Cleanup FIRST. Without it, create_check_constraint validates the pre-existing mixed rows and
    #     aborts the migration on the live corpus.
    op.execute(sa.text(_CLEANUP_MIXED_ROWS))

    # (2) THEN the CHECK (D-06). Bare name -- see module comment.
    op.create_check_constraint(_CHECK_NAME, "analysis", _CHECK_SQL)


def downgrade() -> None:
    """Drop the CHECK. The D-09 cleanup UPDATE is NOT reversed (016/032 best-effort-DDL precedent)."""
    op.drop_constraint(_CHECK_NAME, "analysis", type_="check")
