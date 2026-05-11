"""Tests for migration 012: agents table + backfill.

Covers VALIDATION.md verification rows #08-#16 plus the DATA-01 SQL-level CHECK
constraint behavior:

  - Agents table column inventory + JSONB type for scan_roots
  - CHECK constraint rejects hostile slugs (case, hyphen edges, underscore)
  - token_hash is nullable
  - legacy-application-server born revoked (token_hash NULL, revoked_at NOT NULL)
  - SCAN_PATH env var flows into scan_roots; fallback is /data/music
  - LIVE sentinel scan_batch exists for legacy agent with scan_path '<watcher>'
  - Partial unique index rejects a second LIVE row but allows multiple non-LIVE
  - Pre-existing files / scan_batches rows backfilled to the legacy agent slug

Operator pre-condition: the database ``phaze_migrations_test`` must exist on
``localhost:5432`` (see ``tests/test_migrations/conftest.py``).
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migrations.conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    downgrade_to,
    upgrade_to,
)


# ---------------------------------------------------------------------------
# Tests that consume the ``migrated_engine`` fixture (already at head revision).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_table_columns(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-01: agents table has the expected column set after head upgrade."""
    expected = {"id", "name", "token_hash", "scan_roots", "last_seen_at", "revoked_at", "created_at", "updated_at"}
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'agents'"))
        columns = {row.column_name for row in result.all()}
    assert columns == expected


@pytest.mark.asyncio
async def test_scan_roots_is_jsonb(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-01: agents.scan_roots is JSONB."""
    async with migrated_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT data_type FROM information_schema.columns WHERE table_name = 'agents' AND column_name = 'scan_roots'")
        )
        row = result.one()
    assert row.data_type == "jsonb"


@pytest.mark.asyncio
async def test_id_charset_check(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-01 / T-V5-01: CHECK constraint rejects hostile slugs (case, hyphen edges, underscore)."""
    hostile = ["UPPER", "--double", "-leading", "trailing-", "under_score"]
    for bad in hostile:
        with pytest.raises(IntegrityError):
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO agents (id, name, scan_roots, created_at, updated_at) VALUES (:id, :name, CAST('[]' AS jsonb), NOW(), NOW())"),
                    {"id": bad, "name": "test"},
                )
    # Sanity: a valid slug succeeds.
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO agents (id, name, scan_roots, created_at, updated_at) VALUES (:id, :name, CAST('[]' AS jsonb), NOW(), NOW())"),
            {"id": "valid-slug-123", "name": "valid"},
        )


@pytest.mark.asyncio
async def test_token_hash_nullable(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-01 / T-V6-01: agents.token_hash is nullable so legacy agent can be born revoked."""
    async with migrated_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'agents' AND column_name = 'token_hash'")
        )
        row = result.one()
    assert row.is_nullable == "YES"


@pytest.mark.asyncio
async def test_legacy_agent_born_revoked(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-04 / T-V4-01: legacy-application-server has token_hash NULL and revoked_at NOT NULL."""
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("SELECT token_hash, revoked_at FROM agents WHERE id = 'legacy-application-server'"))
        row = result.one()
    assert row.token_hash is None
    assert row.revoked_at is not None


@pytest.mark.asyncio
async def test_sentinel_scan_path_literal(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-04: legacy agent's LIVE sentinel has scan_path '<watcher>' (literal angle brackets)."""
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("SELECT scan_path FROM scan_batches WHERE agent_id = 'legacy-application-server' AND status = 'live'"))
        row = result.one()
    assert row.scan_path == "<watcher>"


@pytest.mark.asyncio
async def test_legacy_sentinel_exists(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-03: exactly one LIVE sentinel exists for the legacy agent after upgrade."""
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) AS n FROM scan_batches WHERE agent_id = 'legacy-application-server' AND status = 'live'"))
        row = result.one()
    assert row.n == 1


@pytest.mark.asyncio
async def test_partial_uq_rejects_dup_live(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-03: partial unique index rejects a second LIVE scan_batch for the same agent."""
    with pytest.raises(IntegrityError):
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO scan_batches (id, agent_id, scan_path, status, total_files, processed_files, "
                    "created_at, updated_at) "
                    "VALUES (:id, :agent_id, '<watcher>', 'live', 0, 0, NOW(), NOW())"
                ),
                {"id": uuid.uuid4(), "agent_id": "legacy-application-server"},
            )


@pytest.mark.asyncio
async def test_partial_uq_allows_multiple_non_live(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-03: partial unique index only enforces uniqueness when status = 'live'; multiple non-LIVE rows allowed."""
    async with migrated_engine.begin() as conn:
        for _ in range(2):
            await conn.execute(
                text(
                    "INSERT INTO scan_batches (id, agent_id, scan_path, status, total_files, processed_files, "
                    "created_at, updated_at) "
                    "VALUES (:id, :agent_id, '/music', 'running', 0, 0, NOW(), NOW())"
                ),
                {"id": uuid.uuid4(), "agent_id": "legacy-application-server"},
            )
    # If both inserts committed, the partial UQ correctly ignored non-LIVE rows.
    async with migrated_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) AS n FROM scan_batches WHERE agent_id = 'legacy-application-server' AND status = 'running'")
        )
        row = result.one()
    assert row.n == 2


# ---------------------------------------------------------------------------
# Tests that drive their own upgrade sequence (env-var control + backfill of
# pre-existing rows). These deliberately avoid the ``migrated_engine`` fixture
# which would pre-upgrade to head before the test could observe revision 011.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_agent_scan_roots_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """DATA-04: with SCAN_PATH absent, legacy agent's scan_roots == ['/data/music']."""
    monkeypatch.delenv("SCAN_PATH", raising=False)
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    # Establish a clean revision-base before driving the upgrade.
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "012")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT scan_roots FROM agents WHERE id = 'legacy-application-server'"))
            row = result.one()
        assert row.scan_roots == ["/data/music"]
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")


@pytest.mark.asyncio
async def test_legacy_agent_scan_roots_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """DATA-04: SCAN_PATH env var flows into legacy agent's scan_roots JSONB array."""
    monkeypatch.setenv("SCAN_PATH", "/custom/path")
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "012")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT scan_roots FROM agents WHERE id = 'legacy-application-server'"))
            row = result.one()
        assert row.scan_roots == ["/custom/path"]
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")


@pytest.mark.asyncio
async def test_backfill_files() -> None:
    """DATA-04 / T-V5-02: pre-existing files rows from revision 011 get agent_id = legacy-application-server after 012."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "011")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    file_id = uuid.uuid4()
    sentinel_path = f"/music/x-{file_id.hex[:8]}.mp3"
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, "
                    "file_type, file_size, state, created_at, updated_at) "
                    "VALUES (:id, :hash, :path, :name, :path, 'mp3', 1000, 'discovered', NOW(), NOW())"
                ),
                {"id": file_id, "hash": "a" * 64, "path": sentinel_path, "name": "x.mp3"},
            )
        await asyncio.to_thread(upgrade_to, cfg, "012")
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT agent_id FROM files WHERE id = :id"),
                {"id": file_id},
            )
            row = result.one()
        assert row.agent_id == "legacy-application-server"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")


@pytest.mark.asyncio
async def test_backfill_scan_batches() -> None:
    """DATA-04: pre-existing scan_batches rows get agent_id = legacy-application-server after 012."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "011")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    batch_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO scan_batches (id, scan_path, status, total_files, processed_files, "
                    "created_at, updated_at) "
                    "VALUES (:id, '/music', 'running', 0, 0, NOW(), NOW())"
                ),
                {"id": batch_id},
            )
        await asyncio.to_thread(upgrade_to, cfg, "012")
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT agent_id FROM scan_batches WHERE id = :id"),
                {"id": batch_id},
            )
            row = result.one()
        assert row.agent_id == "legacy-application-server"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
