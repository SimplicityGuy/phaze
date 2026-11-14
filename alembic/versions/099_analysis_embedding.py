"""Add ``analysis_embedding``: one float16 EffNet embedding per coarse window (phaze-g79ea).

Persists what phaze-l5mzm made ``analyze_file(include_embeddings=True)`` emit, as spike phaze-cmsr0
recommended: a child table of ``files`` with a plain ``bytea`` column holding the pooled 1280-d vector as
little-endian float16 (2,560 B). No pgvector, no index: nothing queries by similarity yet.

Key ``(file_id, model, window_index)``; ``ON DELETE CASCADE`` to ``files``. A re-analysis replaces a file's
rows for a model, never appends (``services/analysis_embedding_store.replace_embeddings``).

Sizes at the 121,572.3 h archive scale (180 s coarse windows):

* rows: 121,572.3 h x 3,600 s / 180 s = 2,431,446 coarse windows, one row each;
* vector bytes: 2,431,446 x 1280 x 2 B = 2,431,446 x 2,560 B = 6,224,501,760 B = 5.80 GiB;
* on disk, MEASURED on this harness's Postgres (20,000 rows of incompressible random float16, 2026-11-14):
  ``pg_total_relation_size`` 59,547,648 B = 2,977 B per row (heap 101 B, primary-key index 97 B, the rest TOAST,
  because a 2,560 B value exceeds the ~2 kB TOAST threshold and float16 does not compress);
* so 2,431,446 x 2,977 B = 7.24 GB = 6.74 GiB all-in, 16% over the raw vector bytes.

Additive only: no existing table changes; the table starts empty (only a re-analysis fills it). The
migration touches no role, so it runs as the non-superuser app role
(tests/integration/test_migrations/test_restricted_app_role.py).

Revision ID: 099
Revises: 098
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "099"
down_revision: str | None = "098"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "analysis_embedding"


def upgrade() -> None:
    """Create the empty child table of ``files``."""
    op.create_table(
        _TABLE,
        # ON DELETE CASCADE so these rows never block a file or scan deletion (stage_skip's lesson).
        sa.Column("file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("window_index", sa.Integer(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("file_id", "model", "window_index"),
        # BARE constraint names: the ck naming convention adds the table prefix (phaze-x8tof).
        sa.CheckConstraint("window_index >= 0 AND dim > 0", name="indexes_positive"),
        sa.CheckConstraint("octet_length(vector) = dim * 2", name="vector_is_float16"),
    )


def downgrade() -> None:
    """Drop the table."""
    op.drop_table(_TABLE)
