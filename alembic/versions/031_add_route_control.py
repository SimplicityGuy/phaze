"""Add ``route_control`` table + seed the single ``'global'`` row (Phase 71, BEUI-02, D-09).

Additive, reversible migration -- the durable home for the force-local routing override. It
creates the standalone one-row app table ``route_control`` and seeds EXACTLY ONE row
(``id='global'``, ``force_local=false``) so the runtime reader has a stable row to flip.

Mirrors the Phase-37 ``020_add_pipeline_stage_control`` idiom: ``create_table`` with a
``server_default false`` Boolean, then a bound-param seed INSERT (NO f-string interpolation of any
value into the SQL -- threat T-71-04, the 012/019/020 discipline). The single ``'global'`` PK row is
the only row the reader ever touches.

CRITICAL: this migration touches ONLY ``route_control``. It must NEVER reference ``saq_jobs`` --
SAQ owns that table via its own ``init_db()`` + ``saq_versions`` and an Alembic migration touching it
would collide (020/030 CRITICAL banner).

Revision ID: 031
Revises: 030
Create Date: 2026-07-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "031"
down_revision: str | Sequence[str] | None = "030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create route_control, then seed the single default-false ``'global'`` row."""
    op.create_table(
        "route_control",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("force_local", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_route_control")),
    )
    # Seed the single 'global' row with the cloud-enabled baseline (force_local=false). Bound params
    # only -- NO f-string interpolation of any value into the SQL (threat T-71-04; mirrors 020's idiom).
    bind = op.get_bind()
    bind.execute(
        sa.text("INSERT INTO route_control (id, force_local, created_at, updated_at) VALUES (:id, false, NOW(), NOW())"),
        {"id": "global"},
    )


def downgrade() -> None:
    """Drop route_control (mirror of upgrade)."""
    op.drop_table("route_control")
