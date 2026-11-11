"""Expose each agent's container path prefixes to ``analytics_ro`` (phaze-luy62).

A read-only consumer of archive audio is handed a read-only view of the archive, but the paths phaze stores are CONTAINER
paths (they start with one of the owning agent's ``scan_roots``). To open one it must know which agent owns the file and
which prefix to swap for its own mount point. This migration publishes exactly that, on the existing read-only interface
(``analytics_ro``, migration 095), rather than on an HTTP route:

* new view ``analytics_agent_path_prefixes`` -- one row per (agent, prefix): ``agent_id``, ``name``, ``kind``, ``active``
  (not revoked), ``prefix_ordinal`` (1-based position in the agent's ``scan_roots``) and ``path_prefix``. It projects no
  credential material: ``token_hash`` and ``last_status`` are deliberately absent;
* ``analytics_files`` gains ``agent_id`` as its LAST column, so a file joins to its agent. Appending a column at the end is
  not a breaking change under the stability rule in ``docs/analytics-read-only-access.md``.

Revision ID: 096
Revises: 095
"""

# DDL cannot take bind parameters; every interpolated value below is a module constant (ROLE, the VIEW names and bodies).
# ruff: noqa: S608

from collections.abc import Sequence

from alembic import op


revision: str = "096"
down_revision: str | None = "095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "analytics_ro"
NEW_VIEW = "analytics_agent_path_prefixes"

_FILES_COLUMNS = """f.id AS file_id,
               f.sha256_hash,
               f.original_path,
               f.original_filename,
               f.current_path,
               f.file_type,
               f.file_size,
               f.missing_at,
               f.created_at"""

# Frozen here on purpose: a migration must not change meaning when application code later changes.
FILES_V095 = f"SELECT {_FILES_COLUMNS} FROM files f"
FILES_V096 = f"SELECT {_FILES_COLUMNS},\n               f.agent_id FROM files f"

PATH_PREFIXES = """
    SELECT a.id AS agent_id,
           a.name,
           a.kind,
           (a.revoked_at IS NULL) AS active,
           p.ordinal::integer AS prefix_ordinal,
           p.prefix AS path_prefix
    FROM agents a
    CROSS JOIN LATERAL jsonb_array_elements_text(a.scan_roots) WITH ORDINALITY AS p(prefix, ordinal)
"""


def upgrade() -> None:
    """Append ``agent_id`` to ``analytics_files`` (grants survive CREATE OR REPLACE) and publish the prefix view."""
    op.execute(f"CREATE OR REPLACE VIEW analytics_files AS {FILES_V096}")
    op.execute(f"CREATE VIEW {NEW_VIEW} AS {PATH_PREFIXES}")
    op.execute(f"GRANT SELECT ON {NEW_VIEW} TO {ROLE}")


def downgrade() -> None:
    """Drop the prefix view and restore ``analytics_files`` to its 095 shape (a column cannot be removed in place)."""
    op.execute(f"DROP VIEW IF EXISTS {NEW_VIEW}")
    op.execute("DROP VIEW IF EXISTS analytics_files")
    op.execute(f"CREATE VIEW analytics_files AS {FILES_V095}")
    op.execute(f"GRANT SELECT ON analytics_files TO {ROLE}")
