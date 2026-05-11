"""Tests for migration 013: NOT NULL + composite unique swap.

Covers VALIDATION.md verification rows #17-#21 (DATA-02 + DATA-03 NOT NULL half
plus the uniqueness swap from single-column to composite):

  - files.agent_id is NOT NULL after head upgrade
  - scan_batches.agent_id is NOT NULL after head upgrade
  - Same original_path under two different agent_id values both succeed
    (composite UQ allows it; this is success criterion #2 from ROADMAP)
  - Same (agent_id, original_path) pair is rejected by the composite UQ
  - Legacy uq_files_original_path index is gone; uq_files_agent_id_original_path
    is in its place

Operator pre-condition: the database ``phaze_migrations_test`` must exist on
``localhost:5432`` (see ``tests/test_migrations/conftest.py``).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.mark.asyncio
async def test_files_agent_id_not_null(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-02: files.agent_id has is_nullable = 'NO' after head (013) upgrade."""
    async with migrated_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'files' AND column_name = 'agent_id'")
        )
        row = result.one()
    assert row.is_nullable == "NO"


@pytest.mark.asyncio
async def test_scan_batches_agent_id_not_null(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-03: scan_batches.agent_id has is_nullable = 'NO' after head (013) upgrade."""
    async with migrated_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'scan_batches' AND column_name = 'agent_id'")
        )
        row = result.one()
    assert row.is_nullable == "NO"


@pytest.mark.asyncio
async def test_same_path_different_agent(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-02 (SC #2): same original_path under two different agent_id values both succeed."""
    async with migrated_engine.begin() as conn:
        # Create a second agent so we can attribute the second file row to it.
        await conn.execute(
            text("INSERT INTO agents (id, name, scan_roots, created_at, updated_at) VALUES ('agent-b', 'agent-b', CAST('[]' AS jsonb), NOW(), NOW())")
        )
        # Same path under the legacy (pre-seeded) agent.
        await conn.execute(
            text(
                "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
                "file_type, file_size, state, created_at, updated_at) "
                "VALUES (:id, 'legacy-application-server', :hash, '/music/shared.mp3', 'shared.mp3', "
                "'/music/shared.mp3', 'mp3', 1000, 'discovered', NOW(), NOW())"
            ),
            {"id": uuid.uuid4(), "hash": "a" * 64},
        )
        # Same path under agent-b -- this is the row that would have been rejected before 013.
        await conn.execute(
            text(
                "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
                "file_type, file_size, state, created_at, updated_at) "
                "VALUES (:id, 'agent-b', :hash, '/music/shared.mp3', 'shared.mp3', '/music/shared.mp3', "
                "'mp3', 2000, 'discovered', NOW(), NOW())"
            ),
            {"id": uuid.uuid4(), "hash": "b" * 64},
        )

    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) AS n FROM files WHERE original_path = '/music/shared.mp3'"))
        row = result.one()
    assert row.n == 2


@pytest.mark.asyncio
async def test_composite_unique_rejects_dup(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-02: same (agent_id, original_path) twice is rejected by the composite unique index."""
    with pytest.raises(IntegrityError):
        async with migrated_engine.begin() as conn:
            for _ in range(2):
                await conn.execute(
                    text(
                        "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
                        "file_type, file_size, state, created_at, updated_at) "
                        "VALUES (:id, 'legacy-application-server', :hash, '/music/dup.mp3', 'dup.mp3', "
                        "'/music/dup.mp3', 'mp3', 1000, 'discovered', NOW(), NOW())"
                    ),
                    {"id": uuid.uuid4(), "hash": "c" * 64},
                )


@pytest.mark.asyncio
async def test_old_unique_dropped(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """DATA-02: uq_files_original_path is gone; uq_files_agent_id_original_path is in its place."""
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'files'"))
        names = {r.indexname for r in result.all()}
    assert "uq_files_original_path" not in names
    assert "uq_files_agent_id_original_path" in names
