"""Migrations run as production's app role: a login role with no superuser and no CREATEROLE (phaze-euo4a).

Every other migration test connects as the harness's bootstrap superuser, which is why 2026.11.0 shipped a migration that
ran ``CREATE ROLE`` and crash-looped production with ``permission denied to create role``. This module is the real
consumer for a migration (CLAUDE.md, rule 3): it builds the privileges production's ``phaze`` role has, and migrates as
that role.

What production provisions, mirrored here statement for statement:

* the database is created by the cluster's bootstrap superuser and OWNED by it (``CREATE DATABASE ... OWNER <su>``),
  with ``pg_trgm`` created by that superuser;
* the app role is ``LOGIN`` with ``CONNECTION LIMIT 85``, and no ``SUPERUSER``, ``CREATEROLE`` or ``CREATEDB``;
* the app role is a MEMBER of the database owner (``GRANT <su> TO <app>``, inheriting): it reaches the owner's objects
  through that membership. Membership in a superuser role does not make the member a superuser -- the attribute is never
  inherited -- which ``test_the_mirror_cannot_create_a_role`` checks rather than assumes.

``analytics_ro`` is provisioned before the upgrade, as production must do it (``tests/analytics_role.py``).

Two histories are covered, because which role owns production's tables depends on the DSN in force when each was
created: the whole chain run as the app role (it owns every table), and the chain to 094 run as the database owner with
094 -> head run as the app role (it owns none of the tables it builds views over, indexes or alters).
"""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType
import uuid

from alembic.config import Config
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.analytics_role import ANALYTICS_ROLE, ensure_analytics_role
from tests.db_guard import require_test_database

from .analytics_contract import EXPECTED_COLUMNS
from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, downgrade_to, upgrade_to


pytestmark = pytest.mark.asyncio

_APP_PASSWORD = "app-role-test-only"
# A database of its own, derived from this seat's migrations database so two seats never share it, and created fresh so
# it carries what a fresh production database carries (template1's public schema, owned by pg_database_owner).
_DATABASE = f"phaze_approle_{hashlib.sha256(make_url(MIGRATIONS_TEST_DATABASE_URL).database.encode()).hexdigest()[:10]}_test"
_SUPERUSER_URL = make_url(MIGRATIONS_TEST_DATABASE_URL).set(database=_DATABASE).render_as_string(hide_password=False)
require_test_database(_SUPERUSER_URL, context="restricted app-role migration tests")

_VIEWS = "SELECT count(*) FROM pg_views WHERE schemaname = 'public' AND viewname LIKE 'analytics\\_%'"
_GRANTS = "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = 'analytics_ro'"
# The role's OWN entry in public's ACL: has_schema_privilege() would also count PUBLIC's USAGE, which a fresh database
# (production's included) grants on public, so it reads true with or without 095's grant.
_SCHEMA_USAGE = (
    "SELECT EXISTS (SELECT 1 FROM pg_namespace n, aclexplode(n.nspacl) a "
    "WHERE n.nspname = 'public' AND a.grantee = 'analytics_ro'::regrole AND a.privilege_type = 'USAGE')"
)


def _migration(filename: str) -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class AppRoleDatabase:
    """A fresh database owned by the harness superuser, and a login role shaped like production's app role."""

    role: str
    owner: str
    app_url: str
    superuser: AsyncEngine

    def config(self, *, as_app: bool) -> Config:
        return _build_alembic_config(self.app_url if as_app else _SUPERUSER_URL)


async def _autocommit(url: str, *statements: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.exec_driver_sql(statement)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_db() -> AsyncGenerator[AppRoleDatabase]:
    """Provision the production shape on the harness, yield it, and drop the database and the throwaway role."""
    cluster = create_async_engine(MIGRATIONS_TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with cluster.connect() as conn:
            owner, is_superuser = (await conn.execute(text("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"))).one()
    finally:
        await cluster.dispose()
    assert is_superuser, "the harness must connect as its bootstrap superuser to build the production role shape"
    role = f"phaze_app_{uuid.uuid4().hex[:10]}"
    await ensure_analytics_role(MIGRATIONS_TEST_DATABASE_URL)
    await _autocommit(
        MIGRATIONS_TEST_DATABASE_URL,
        f'DROP DATABASE IF EXISTS "{_DATABASE}" WITH (FORCE)',
        f'CREATE DATABASE "{_DATABASE}" OWNER "{owner}"',
        f"CREATE ROLE {role} LOGIN PASSWORD '{_APP_PASSWORD}' CONNECTION LIMIT 85 NOSUPERUSER NOCREATEROLE NOCREATEDB",
        f'GRANT "{owner}" TO {role}',
    )
    await _autocommit(_SUPERUSER_URL, "CREATE EXTENSION IF NOT EXISTS pg_trgm")
    app_url = make_url(_SUPERUSER_URL).set(username=role, password=_APP_PASSWORD).render_as_string(hide_password=False)
    superuser = create_async_engine(_SUPERUSER_URL, poolclass=NullPool)
    try:
        yield AppRoleDatabase(role=role, owner=owner, app_url=app_url, superuser=superuser)
    finally:
        await superuser.dispose()
        await _autocommit(MIGRATIONS_TEST_DATABASE_URL, f'DROP DATABASE IF EXISTS "{_DATABASE}" WITH (FORCE)', f"DROP ROLE IF EXISTS {role}")


async def _scalar(engine: AsyncEngine, sql: str) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar_one()


async def _assert_analytics_interface(db: AppRoleDatabase, *, present: bool) -> None:
    assert await _scalar(db.superuser, _VIEWS) == (len(EXPECTED_COLUMNS) if present else 0)
    assert await _scalar(db.superuser, _GRANTS) == (len(EXPECTED_COLUMNS) if present else 0)
    assert await _scalar(db.superuser, _SCHEMA_USAGE) is present
    assert await _scalar(db.superuser, "SELECT count(*) FROM pg_roles WHERE rolname = 'analytics_ro'") == 1, "a migration must never drop the role"


async def test_the_mirror_cannot_create_a_role(app_db: AppRoleDatabase) -> None:
    """The fixture really has production's shape: not a superuser, no CREATEROLE, a member of the database owner."""
    app = create_async_engine(app_db.app_url, poolclass=NullPool)
    try:
        async with app.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolconnlimit, pg_has_role(current_user, d.datdba, 'USAGE') "
                        "FROM pg_roles r, pg_database d WHERE r.rolname = current_user AND d.datname = current_database()"
                    )
                )
            ).one()
            assert tuple(row) == (False, False, False, 85, True)
        async with app.connect() as conn:
            with pytest.raises(DBAPIError, match="permission denied to create role"):
                await conn.exec_driver_sql("CREATE ROLE phaze_app_should_not_exist")
    finally:
        await app.dispose()


async def test_the_whole_chain_upgrades_and_downgrades_as_the_app_role(app_db: AppRoleDatabase) -> None:
    cfg = app_db.config(as_app=True)
    await asyncio.to_thread(upgrade_to, cfg, "head")
    await _assert_analytics_interface(app_db, present=True)
    assert await _scalar(app_db.superuser, "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'files'") == app_db.role

    await asyncio.to_thread(downgrade_to, cfg, "094")
    await _assert_analytics_interface(app_db, present=False)

    await asyncio.to_thread(upgrade_to, cfg, "head")  # repeatable: the role surviving the downgrade does not break it
    await _assert_analytics_interface(app_db, present=True)


async def test_094_to_head_as_the_app_role_over_tables_owned_by_both_roles(app_db: AppRoleDatabase) -> None:
    """Production's history at 094: MIXED ownership (some tables the app role's, some the database owner's).

    Measured on production 2026-11-12 (read-only, reported by the dispatcher): 31 tables owned by the app role, 19 by the
    database owner, no views in public, public owned by pg_database_owner, and the app role a member of the owner with
    inherit. Here the owner builds the chain to 094 and every other table (by name) is handed to the app role, so the
    tables 095-097 read, index and alter are owned by both.
    """
    await asyncio.to_thread(upgrade_to, app_db.config(as_app=False), "094")
    async with app_db.superuser.begin() as conn:
        tables = (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"))).scalars().all()
        for table in tables[::2]:
            await conn.exec_driver_sql(f'ALTER TABLE public."{table}" OWNER TO {app_db.role}')
        owners = dict((await conn.execute(text("SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public'"))).tuples().all())
        schema_owner = (await conn.execute(text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'"))).scalar_one()
    assert set(owners.values()) == {app_db.role, app_db.owner}
    assert {"files", "analysis", "agents", "tracklists", "provider_source_observations"} & {t for t, o in owners.items() if o == app_db.owner}
    assert {"files", "analysis", "agents", "tracklists", "provider_source_observations"} & {t for t, o in owners.items() if o == app_db.role}
    assert schema_owner == "pg_database_owner"
    assert await _scalar(app_db.superuser, _VIEWS) == 0

    cfg = app_db.config(as_app=True)
    await asyncio.to_thread(upgrade_to, cfg, "head")
    await _assert_analytics_interface(app_db, present=True)
    view_owners = await _scalar(app_db.superuser, "SELECT array_agg(DISTINCT viewowner) FROM pg_views WHERE viewname LIKE 'analytics\\_%'")
    assert view_owners == [app_db.role], "the app role creates the views, so it may grant on them without CREATEROLE"
    assert await _scalar(app_db.superuser, "SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_files_created_at_id'") == 1, "097 as the app role"

    await asyncio.to_thread(downgrade_to, cfg, "094")
    await _assert_analytics_interface(app_db, present=False)


class _Recorder:
    """Stands in for ``alembic.op``: records each statement, and refuses the role check the way a missing role does."""

    def __init__(self, refuse: str) -> None:
        self.refuse = refuse
        self.executed: list[str] = []

    def execute(self, statement: str) -> None:
        self.executed.append(statement)
        if statement == self.refuse:
            raise RuntimeError("role check refused")


@pytest.mark.parametrize(
    ("filename", "steps"), [("095_analytics_ro_role.py", ("upgrade",)), ("096_analytics_agent_path_prefixes.py", ("upgrade", "downgrade"))]
)
async def test_a_missing_role_fails_the_migration_first_with_the_provisioning_step(
    app_db: AppRoleDatabase, filename: str, steps: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stated behaviour (phaze-euo4a): no role -> the migration FAILS, before any DDL, naming the fix.

    ``analytics_ro`` is cluster-wide and other seats' databases hold grants to it, so it cannot be dropped to test this.
    The migration's own guard is run instead with an absent role's name in it, as the app role, on a database at 094;
    and each step that grants is shown to run that guard before anything else, so a refusal leaves nothing behind.
    """
    migration = _migration(filename)
    absent = f"analytics_ro_absent_{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(upgrade_to, app_db.config(as_app=True), "094")
    app = create_async_engine(app_db.app_url, poolclass=NullPool)
    try:
        async with app.connect() as conn:
            with pytest.raises(DBAPIError) as excinfo:
                await conn.exec_driver_sql(migration.REQUIRE_ROLE.replace(ANALYTICS_ROLE, absent))
    finally:
        await app.dispose()
    message = str(excinfo.value)
    assert f'role "{absent}" does not exist, and phaze migrations do not create roles' in message
    assert 'docs/analytics-read-only-access.md, "Provisioning the role"' in message
    assert "default_transaction_read_only = on" in message

    for step in steps:
        recorder = _Recorder(refuse=migration.REQUIRE_ROLE)
        monkeypatch.setattr(migration, "op", recorder)
        with pytest.raises(RuntimeError, match="role check refused"):
            getattr(migration, step)()
        assert recorder.executed == [migration.REQUIRE_ROLE], f"{filename} {step}: the role check must be the first statement"
