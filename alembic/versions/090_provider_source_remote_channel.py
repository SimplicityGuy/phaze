"""Allow a remote provider source object: widen the source channel check to include 'remote'.

A remote source object has no inventory file, so its ``source_file_id`` stays NULL
(docs/design/0025-remote-provider-sources.md, section 6). Existing local rows are untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "090"
down_revision: str | None = "089"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The bare name, as the ORM declares it: alembic applies the ck_<table>_ naming convention in both
# directions, so the database carries ck_provider_source_objects_provider_channel (see migration 065).
_NAME = "provider_channel"


def upgrade() -> None:
    op.drop_constraint(_NAME, "provider_source_objects", type_="check")
    op.create_check_constraint(_NAME, "provider_source_objects", "channel IS NULL OR channel IN ('companion', 'embedded', 'remote')")


def downgrade() -> None:
    """Remote source objects are retained evidence; only a database without one is reversible."""
    used = op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM provider_source_objects WHERE channel = 'remote')"))
    if used:
        raise RuntimeError("Remote provider source objects exist; lossy downgrade refused")
    op.drop_constraint(_NAME, "provider_source_objects", type_="check")
    op.create_check_constraint(_NAME, "provider_source_objects", "channel IS NULL OR channel IN ('companion', 'embedded')")
