"""Add agents table, agent_id columns, FKs, and backfill legacy agent.

Revision ID: 012
Revises: 011
Create Date: 2026-05-11
"""

from collections.abc import Sequence
import json
import logging
import os
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "012"
down_revision: str | Sequence[str] | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    """Create agents table, seed legacy agent + sentinel, add agent_id columns + FKs, backfill."""
    # 1. Create agents table - pattern from 011_add_tag_write_log.py:24-39
    op.create_table(
        "agents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=True),
        sa.Column("scan_roots", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.CheckConstraint("id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_agents_id_charset"),
    )

    # 2. Resolve SCAN_PATH from env, log resolution, seed legacy agent (D-05, D-06, D-07)
    raw_scan_path = os.environ.get("SCAN_PATH", "/data/music")
    scan_roots_json = json.dumps([raw_scan_path])
    logger.info(
        "phaze-024: resolved legacy-application-server scan_roots=%s (SCAN_PATH=%r)",
        scan_roots_json,
        raw_scan_path,
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO agents (id, name, token_hash, scan_roots, revoked_at, created_at, updated_at) "
            "VALUES (:id, :name, NULL, CAST(:scan_roots AS jsonb), NOW(), NOW(), NOW())"
        ),
        {"id": "legacy-application-server", "name": "legacy-application-server", "scan_roots": scan_roots_json},
    )

    # 3. Add nullable agent_id columns - pattern from 005_add_metadata_columns.py:24-26
    op.add_column("files", sa.Column("agent_id", sa.String(64), nullable=True))
    op.add_column("scan_batches", sa.Column("agent_id", sa.String(64), nullable=True))

    # 4. FKs with ON DELETE RESTRICT - pattern from 002_add_scan_batches_and_unique_path.py:43
    op.create_foreign_key(
        "fk_files_agent_id_agents",
        "files",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scan_batches_agent_id_agents",
        "scan_batches",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 5. Plain index on scan_batches.agent_id (D-15: no separate index on files.agent_id - composite UQ covers it)
    op.create_index("ix_scan_batches_agent_id", "scan_batches", ["agent_id"])

    # 6. Backfill (D-08, D-14: pure raw SQL via sa.text - no model imports)
    op.execute(sa.text("UPDATE files SET agent_id = 'legacy-application-server' WHERE agent_id IS NULL"))
    op.execute(sa.text("UPDATE scan_batches SET agent_id = 'legacy-application-server' WHERE agent_id IS NULL"))

    # 7. Sentinel LIVE scan_batch for legacy agent (D-11; UUID generated in Python per RESEARCH Pattern 4)
    sentinel_id = uuid.uuid4()
    op.get_bind().execute(
        sa.text(
            "INSERT INTO scan_batches (id, agent_id, scan_path, status, total_files, processed_files, "
            "created_at, updated_at) "
            "VALUES (:id, :agent_id, '<watcher>', 'live', 0, 0, NOW(), NOW())"
        ),
        {"id": sentinel_id, "agent_id": "legacy-application-server"},
    )

    # 8. Partial UQ for sentinel (D-12) - AFTER the INSERT so the first run cannot violate it
    op.create_index(
        "uq_scan_batches_agent_id_live",
        "scan_batches",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status = 'live'"),
    )


def downgrade() -> None:
    """Drop partial UQ, sentinel, agent_id columns, FKs, and agents table."""
    op.drop_index("uq_scan_batches_agent_id_live", table_name="scan_batches")
    op.execute(sa.text("DELETE FROM scan_batches WHERE status = 'live'"))
    op.drop_index("ix_scan_batches_agent_id", table_name="scan_batches")
    op.drop_constraint("fk_scan_batches_agent_id_agents", "scan_batches", type_="foreignkey")
    op.drop_constraint("fk_files_agent_id_agents", "files", type_="foreignkey")
    op.drop_column("scan_batches", "agent_id")
    op.drop_column("files", "agent_id")
    op.drop_table("agents")
