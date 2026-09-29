"""Create ``runtime_config_override`` -- the DB override layer for hot-reloadable config (phaze-mvq8z.6).

The TOP layer of ``phaze.runtime_config``'s precedence ladder
(``docs/design/0019-runtime-config-hot-reload.md`` §3): one row per reloadable key an operator has
overridden through the admin API/UI, read through the degrade-safe
``phaze.services.runtime_config_overrides.get_runtime_config_overrides`` and wired into
``phaze.runtime_config.RuntimeConfigStore`` as its ``override_provider``. Mirrors
``route_control`` / ``pipeline_stage_control``: a small, standalone, directly operator-mutable
control table outside SAQ's schema.

Pure additive DDL: one new table, no changes to any existing one, no seed rows -- a key with no
row here simply falls through to the ``file`` / ``env`` / ``default`` layers underneath it
(``phaze.runtime_config._build``), which is the correct, already-tested behavior for "never
overridden".

``downgrade`` drops the table outright. This molecule lands as a SINGLE ``--no-ff`` merge to
``main`` so it can be reverted with one ``git revert -m 1`` (operator decision, epic ``phaze-mvq8z``
comment 2026-09-28); a code revert alone would leave this table in place with no code left that
reads it, so the downgrade must genuinely work, not merely exist. Losing the override rows on
downgrade is correct, not merely tolerated: every overridden key falls back to the ``file`` / ``env``
/ ``default`` layers the process was already honoring underneath the override, exactly as if the
operator had cleared every override through the admin API first.

Revision ID: 072
Revises: 071
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "072"
down_revision: str | None = "071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "runtime_config_override"


def upgrade() -> None:
    """Create the (initially empty) override table."""
    op.create_table(
        _TABLE,
        sa.Column("key", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("value", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    """Drop the override table. Every previously-overridden key falls back to its next layer
    (``file`` / ``env`` / ``default``) -- see this module's docstring for why that is the correct
    (not merely tolerated) behavior for a single-merge, single-revert molecule."""
    op.drop_table(_TABLE)
