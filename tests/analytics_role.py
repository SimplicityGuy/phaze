"""The test harness's stand-in for the deployment's provisioning of ``analytics_ro`` (phaze-euo4a).

Migrations do not create, alter or drop roles: production's app role has no ``CREATEROLE``, and a migration that ran
``CREATE ROLE`` crash-looped the 2026.11.0 deploy. The role is provisioned OUTSIDE phaze, by a role that may create
roles, before phaze starts on a release that needs it (``docs/analytics-read-only-access.md``). Migration 095 refuses
to run without it.

The harness connects as its bootstrap superuser, so it plays that provisioning step here, with the same attributes the
documentation asks production for. A role is cluster-wide while every seat's database is not, so this only ever creates
the role when it is absent and never drops it: other seats' databases hold grants to it.
"""

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


ANALYTICS_ROLE = "analytics_ro"

# Two seats provisioning at once must not race check-then-create. An advisory lock cannot serialize them (its key is
# scoped to the current database), but a lock on the shared catalog pg_authid is cluster-wide. The harness user is a
# superuser, so it may take it; no migration does (tests/shared/test_migrations_issue_no_role_ddl.py).
_STATEMENTS: tuple[str, ...] = (
    "LOCK TABLE pg_catalog.pg_authid IN SHARE UPDATE EXCLUSIVE MODE",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_ro') THEN
            CREATE ROLE analytics_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT;
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM pg_db_role_setting s
            JOIN pg_roles r ON r.oid = s.setrole
            WHERE r.rolname = 'analytics_ro' AND s.setdatabase = 0 AND 'default_transaction_read_only=on' = ANY (s.setconfig)
        ) THEN
            ALTER ROLE analytics_ro SET default_transaction_read_only = on;
        END IF;
    END
    $$
    """,
)


async def ensure_analytics_role(superuser_url: str) -> None:
    """Create ``analytics_ro`` on the cluster behind ``superuser_url`` if it is absent, read-only by default."""
    engine = create_async_engine(superuser_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for statement in _STATEMENTS:
                await conn.exec_driver_sql(statement)
    finally:
        await engine.dispose()
