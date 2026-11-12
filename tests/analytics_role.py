"""The test harness's stand-in for the deployment's provisioning of ``analytics_ro`` (phaze-euo4a).

Migrations do not create, alter or drop roles: production's app role has no ``CREATEROLE``, and a migration that ran
``CREATE ROLE`` crash-looped the 2026.11.0 deploy. The role is provisioned OUTSIDE phaze, by a role that may create
roles, before phaze starts on a release that needs it (``docs/analytics-read-only-access.md``). Migration 095 refuses
to run without it.

The harness connects as its bootstrap superuser, so it plays that provisioning step here, with the same attributes the
documentation asks production for. A role is cluster-wide while every seat's database is not, so this only ever creates
the role when it is absent and never drops it: other seats' databases hold grants to it.
"""

from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


ANALYTICS_ROLE = "analytics_ro"

# The deployment's own provisioning SQL, run verbatim: the harness provisions the role exactly as the documentation and the
# compose stack do, so the three cannot drift apart.
PROVISIONING_SQL = Path(__file__).resolve().parents[1] / "deploy" / "postgres-init" / "10-analytics-ro.sql"

# Two seats provisioning at once must not race check-then-create. An advisory lock cannot serialize them (its key is
# scoped to the current database), but a lock on the shared catalog pg_authid is cluster-wide. The harness user is a
# superuser, so it may take it here; no migration does (tests/shared/test_migrations_issue_no_role_ddl.py).
_LOCK_ROLE_CATALOG = "LOCK TABLE pg_catalog.pg_authid IN SHARE UPDATE EXCLUSIVE MODE"


async def ensure_analytics_role(superuser_url: str) -> None:
    """Create ``analytics_ro`` on the cluster behind ``superuser_url`` if it is absent, read-only by default."""
    url = make_url(superuser_url)
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"), poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(_LOCK_ROLE_CATALOG)
            await conn.exec_driver_sql(PROVISIONING_SQL.read_text(encoding="utf-8"))
    finally:
        await engine.dispose()
