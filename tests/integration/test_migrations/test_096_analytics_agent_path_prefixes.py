"""Migration 096 publishes each agent's container path prefixes to ``analytics_ro`` (phaze-luy62).

Acceptance criterion "the per-agent path prefix mapping is exposed (view or endpoint) and documented" is discharged here,
as the role itself would use it:

* ``analytics_ro`` reads ``analytics_agent_path_prefixes``: one row per agent and prefix, ``active`` false once revoked;
* no credential column is projected (``token_hash``, ``last_status``);
* ``analytics_files.agent_id`` joins a file to its agent's prefixes, so a stored path resolves;
* downgrade restores ``analytics_files`` to its 095 shape and drops the view, and upgrade is repeatable.

Documentation of every column is enforced by ``tests/shared/test_analytics_read_only_docs.py``.
"""

# View names below are module constants; DDL/identifiers cannot be bound.
# ruff: noqa: S608

import asyncio
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .analytics_contract import EXPECTED_COLUMNS
from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


_ROLE = "analytics_ro"
_TEST_ONLY_PASSWORD = "analytics-ro-test-only"
_VIEW = "analytics_agent_path_prefixes"


def _ro_url() -> str:
    return make_url(MIGRATIONS_TEST_DATABASE_URL).set(username=_ROLE, password=_TEST_ONLY_PASSWORD).render_as_string(hide_password=False)


@pytest_asyncio.fixture
async def seeded() -> AsyncGenerator[tuple[AsyncEngine, AsyncEngine]]:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "head")
    owner = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    ro = create_async_engine(_ro_url())
    try:
        async with owner.begin() as conn:
            await conn.execute(text(f"ALTER ROLE {_ROLE} PASSWORD '{_TEST_ONLY_PASSWORD}'"))
            await conn.execute(
                text(
                    "INSERT INTO agents (id, name, kind, scan_roots, token_hash, revoked_at, created_at, updated_at) VALUES "
                    "('store-a', 'Store A', 'fileserver', '[\"/data/music\", \"/data/video\"]'::jsonb, 'secret-hash', NULL, NOW(), NOW()), "
                    "('store-old', 'Store Old', 'fileserver', '[\"/data/old\"]'::jsonb, 'secret-hash', NOW(), NOW(), NOW()), "
                    "('lane-c', 'Lane C', 'compute', '[]'::jsonb, 'secret-hash', NULL, NOW(), NOW())"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
                    "VALUES (gen_random_uuid(), 'h1', '/data/music/set.mp3', 'set.mp3', '/data/music/set.mp3', 'mp3', 7, 'store-a')"
                )
            )
        yield owner, ro
    finally:
        await ro.dispose()
        await owner.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


async def test_the_role_reads_one_row_per_agent_and_prefix(seeded: tuple[AsyncEngine, AsyncEngine]) -> None:
    _, ro = seeded
    async with ro.connect() as conn:
        result = await conn.execute(text(f"SELECT * FROM {_VIEW} ORDER BY agent_id, prefix_ordinal"))
        assert list(result.keys()) == EXPECTED_COLUMNS[_VIEW]
        rows = [tuple(r) for r in result.all()]
    assert rows == [
        ("store-a", "Store A", "fileserver", True, 1, "/data/music"),
        ("store-a", "Store A", "fileserver", True, 2, "/data/video"),
        ("store-old", "Store Old", "fileserver", False, 1, "/data/old"),
    ]


async def test_no_credential_column_is_projected(seeded: tuple[AsyncEngine, AsyncEngine]) -> None:
    owner, _ = seeded
    async with owner.connect() as conn:
        columns = (await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = :v"), {"v": _VIEW})).scalars().all()
    assert not {"token_hash", "last_status", "scan_roots"} & set(columns)


async def test_a_stored_path_resolves_through_the_published_mapping(seeded: tuple[AsyncEngine, AsyncEngine]) -> None:
    _, ro = seeded
    async with ro.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT f.current_path, p.path_prefix FROM analytics_files f "
                    f"JOIN {_VIEW} p ON p.agent_id = f.agent_id AND p.active AND starts_with(f.current_path, p.path_prefix)"
                )
            )
        ).one()
    assert tuple(row) == ("/data/music/set.mp3", "/data/music")


async def test_the_role_holds_select_on_the_new_view_and_still_cannot_read_agents(seeded: tuple[AsyncEngine, AsyncEngine]) -> None:
    owner, ro = seeded
    async with owner.connect() as conn:
        granted = (
            (
                await conn.execute(
                    text("SELECT privilege_type FROM information_schema.role_table_grants WHERE grantee = :r AND table_name = :v"),
                    {"r": _ROLE, "v": _VIEW},
                )
            )
            .scalars()
            .all()
        )
    assert granted == ["SELECT"]
    async with ro.connect() as conn:
        with pytest.raises(DBAPIError) as excinfo:
            await conn.execute(text("SELECT token_hash FROM agents"))
    assert "permission denied" in str(excinfo.value)


async def test_downgrade_restores_the_095_shape_and_upgrade_is_repeatable() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    cols = "SELECT column_name FROM information_schema.columns WHERE table_name = 'analytics_files' ORDER BY ordinal_position"
    exists = "SELECT count(*) FROM pg_views WHERE schemaname = 'public' AND viewname = :v"
    grant = "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = :r AND table_name = 'analytics_files'"
    try:
        for _ in range(2):
            await asyncio.to_thread(upgrade_to, cfg, "head")
            async with engine.connect() as conn:
                assert (await conn.execute(text(cols))).scalars().all()[-1] == "agent_id"
                assert (await conn.execute(text(exists), {"v": _VIEW})).scalar_one() == 1
            await asyncio.to_thread(downgrade_to, cfg, "095")
            async with engine.connect() as conn:
                assert "agent_id" not in (await conn.execute(text(cols))).scalars().all()
                assert (await conn.execute(text(exists), {"v": _VIEW})).scalar_one() == 0
                assert (await conn.execute(text(grant), {"r": _ROLE})).scalar_one() == 1
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
