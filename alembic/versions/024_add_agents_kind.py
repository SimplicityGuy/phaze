"""Add the ``kind`` capability marker to the ``agents`` table.

Additive-only migration. Adds a single NOT NULL ``kind`` column to the existing
``agents`` table so the row carries durable agent identity: the marker that
distinguishes a media-less compute (cloud) agent from a file-server agent.

Why on the row (not config alone): the Agents admin page reads ``kind`` from the
table (CLOUDAGENT-03) and the ``phaze agents add`` CLI sets it at insert time
(CLOUDAGENT-01). The DB is the authoritative store of agent identity.

Backfill: the column ships with ``server_default='fileserver'``, so every existing
row -- including the migration-012-seeded ``legacy-application-server`` -- reads
correctly with no separate UPDATE step.

Tampering defense (T-48-02): ``ck_agents_kind_enum`` (declared name ``kind_enum``,
auto-prefixed to ``ck_agents_kind_enum`` by the naming convention) restricts the
value to ``{'fileserver', 'compute'}`` at the database -- the innermost layer of the
3-layer enum defense (CLI choices + config Literal land in Plan 02).

CRITICAL: this migration touches ONLY ``agents``.
It must never reference ``saq_jobs`` (SAQ owns that table via init_db + saq_versions).

Revision ID: 024
Revises: 023
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "024"
down_revision: str | Sequence[str] | None = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the NOT NULL ``kind`` column (server_default backfills) + the enum CHECK."""
    op.add_column("agents", sa.Column("kind", sa.String(16), nullable=False, server_default="fileserver"))
    op.create_check_constraint("kind_enum", "agents", "kind IN ('fileserver', 'compute')")


def downgrade() -> None:
    """Drop the CHECK then the column (dependents first; mirror of upgrade).

    Pass the bare constraint name ``kind_enum`` -- the ``ck_%(table_name)s_%(constraint_name)s``
    naming convention re-applies the ``ck_agents_`` prefix, resolving to the live
    ``ck_agents_kind_enum`` (passing the already-prefixed name double-prefixes it).
    """
    op.drop_constraint("kind_enum", "agents", type_="check")
    op.drop_column("agents", "kind")
