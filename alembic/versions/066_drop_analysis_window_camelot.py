"""Drop ``analysis_window.camelot`` -- it becomes a read-time property (phaze-6r3eh).

**The inverse of migration 063.** ``alembic/versions/063_set_projection.py`` added
``analysis_window.camelot`` as a nullable ``String(3)`` alongside ``energy`` and ``mood_scores``.
This migration undoes only the ``camelot`` half of that change; ``energy`` and ``mood_scores``
are untouched -- the JSONB-scale argument that justifies persisting those two (reading a ~5 KB
``features`` JSONB per row does not scale to the millions of window rows the archive implies)
does not apply to ``camelot``, which is a pure lookup of the same row's ``musical_key`` through
the 24-entry Camelot table (:func:`phaze.services.set_projection.camelot_code`).

**Operator decision, 2026-09-16** (recorded in full on bead ``phaze-6r3eh`` and in
``docs/design/0018-set-projection-and-file-viewer.md``): drop the column; every reader takes
``AnalysisWindow.camelot`` off an already-loaded row, so a read-time property computed from
``musical_key`` gives every consumer the identical value with zero backfill and no
``projection_version`` bump. Usefulness was checked first: zero SQL-side uses of the column (no
filter, no ``ORDER BY``, no index) and no planned SQL use was named.

**Downgrade recreates the column NULLABLE and does NOT backfill it.** Every row's camelot value
is trivially recoverable from its own ``musical_key`` via ``camelot_code`` -- unlike a genuine
data-loss drop, nothing here is lost -- but recomputing and writing it back is a rewrite over
however many rows exist at downgrade time, which is out of scope for a schema-only downgrade.
A caller who downgrades and needs the column populated must run the equivalent of the
``phaze-x1qr3.3`` backfill (``services.set_projection_backfill``) themselves, adapted to write
``camelot`` again since the writer this migration accompanies no longer does.

Revision ID: 066
Revises: 065
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "066"
down_revision: str | None = "065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "analysis_window"
_COLUMN = "camelot"


def upgrade() -> None:
    """Drop ``analysis_window.camelot``. Purely additive-in-reverse: no other column touched."""
    op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    """Recreate ``analysis_window.camelot`` as nullable ``String(3)``, matching migration 063 (the
    one that originally added it -- this migration's own predecessor is 065, unrelated).

    NOT backfilled -- see the module docstring. Every row reads NULL until something recomputes
    and writes it, same as any newly added nullable column.
    """
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=3), nullable=True))
