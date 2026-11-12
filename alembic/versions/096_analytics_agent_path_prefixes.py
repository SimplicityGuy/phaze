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

# Every statement below is a literal: no SQL is formatted at run time (Semgrep's formatted-SQL rules, phaze-2al7q.3).

from collections.abc import Sequence

from alembic import op


revision: str = "096"
down_revision: str | None = "095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "analytics_ro"
NEW_VIEW = "analytics_agent_path_prefixes"

# Frozen here on purpose: a migration must not change meaning when application code later changes.
FILES_V096 = """
    CREATE OR REPLACE VIEW analytics_files AS
        SELECT f.id AS file_id,
               f.sha256_hash,
               f.original_path,
               f.original_filename,
               f.current_path,
               f.file_type,
               f.file_size,
               f.missing_at,
               f.created_at,
               f.agent_id
        FROM files f
"""

FILES_V095 = """
    CREATE VIEW analytics_files AS
        SELECT f.id AS file_id,
               f.sha256_hash,
               f.original_path,
               f.original_filename,
               f.current_path,
               f.file_type,
               f.file_size,
               f.missing_at,
               f.created_at
        FROM files f
"""

PATH_PREFIXES = """
    CREATE VIEW analytics_agent_path_prefixes AS
    SELECT a.id AS agent_id,
           a.name,
           a.kind,
           (a.revoked_at IS NULL) AS active,
           p.ordinal::integer AS prefix_ordinal,
           p.prefix AS path_prefix
    FROM agents a
    CROSS JOIN LATERAL jsonb_array_elements_text(a.scan_roots) WITH ORDINALITY AS p(prefix, ordinal)
"""

# The role is provisioned outside phaze (phaze-euo4a; see migration 095): refuse, before any DDL, rather than fail on a
# bare GRANT. The same literal as 095's, frozen here on purpose.
REQUIRE_ROLE = """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_ro') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'undefined_object',
                MESSAGE = 'role "analytics_ro" does not exist, and phaze migrations do not create roles. Provision it before '
                    'upgrading, as a role that may create roles (the cluster superuser): LOGIN, no superuser, no createdb, no '
                    'createrole, no replication, default_transaction_read_only = on. The statements are in '
                    'docs/analytics-read-only-access.md, "Provisioning the role". Then restart phaze to re-run the upgrade.';
        END IF;
    END
    $$
"""

GRANT_PATH_PREFIXES = "GRANT SELECT ON analytics_agent_path_prefixes TO analytics_ro"
DROP_PATH_PREFIXES = "DROP VIEW IF EXISTS analytics_agent_path_prefixes"
DROP_FILES = "DROP VIEW IF EXISTS analytics_files"
GRANT_FILES = "GRANT SELECT ON analytics_files TO analytics_ro"


def upgrade() -> None:
    """Append ``agent_id`` to ``analytics_files`` (grants survive CREATE OR REPLACE) and publish the prefix view."""
    op.execute(REQUIRE_ROLE)
    op.execute(FILES_V096)
    op.execute(PATH_PREFIXES)
    op.execute(GRANT_PATH_PREFIXES)


def downgrade() -> None:
    """Drop the prefix view and restore ``analytics_files`` to its 095 shape (a column cannot be removed in place)."""
    op.execute(REQUIRE_ROLE)
    op.execute(DROP_PATH_PREFIXES)
    op.execute(DROP_FILES)
    op.execute(FILES_V095)
    op.execute(GRANT_FILES)
