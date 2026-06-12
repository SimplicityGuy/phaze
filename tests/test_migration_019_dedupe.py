"""Tests for migration 019: dedupe PENDING proposals + partial unique index (D-04).

Drives its own downgrade/upgrade sequence (mirroring test_migrations/test_migration_018.py)
rather than the ``migrated_engine`` fixture. The test:

  - upgrades to 018 (the partial unique index does NOT exist yet),
  - seeds >=2 PENDING proposals for one file plus an APPROVED proposal for it,
  - upgrades to 019 and asserts the dedupe collapsed PENDING to exactly one row
    (keeping the most-recent created_at) WHILE the APPROVED row is untouched,
  - asserts the partial unique index ``uq_proposals_file_id_pending`` exists and
    actually rejects a second PENDING insert for the same file_id,
  - asserts a second APPROVED row for the same file is allowed (outside the index),
  - downgrades to 018 and asserts the index is dropped,
  - cleans up via downgrade_to('base') in a finally block.

Static-string assertions (revision/down_revision) run without a DB so the
bare-number contract is checked even where Postgres is absent.

Operator pre-condition: the database ``phaze_migrations_test`` must exist (see
``tests/test_migrations/conftest.py``). This module lives at the tests/ root (not
under tests/test_migrations/), so it is marked ``integration`` explicitly rather
than via the path rule in ``tests/conftest.py``.
"""

import asyncio
from datetime import datetime
import importlib.util
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migrations.conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    downgrade_to,
    upgrade_to,
)


pytestmark = pytest.mark.integration


def _load_migration_019() -> object:
    """Load the 019 migration module by path (its name starts with a digit)."""
    migration_path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "019_add_proposals_pending_unique_index.py"
    spec = importlib.util.spec_from_file_location("migration_019", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_INDEX_NAMES_SQL = "SELECT indexname FROM pg_indexes WHERE tablename = 'proposals'"

# A minimal files row (FK target). agent_id 'legacy-application-server' is created by migration 012's backfill.
_INSERT_FILE_SQL = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
    "file_type, file_size, state, created_at, updated_at) "
    "VALUES (:id, 'legacy-application-server', :hash, :path, :name, :path, 'mp3', 1000, 'discovered', NOW(), NOW())"
)
_INSERT_PROPOSAL_SQL = (
    "INSERT INTO proposals (id, file_id, proposed_filename, proposed_path, confidence, status, created_at, updated_at) "
    "VALUES (:id, :file_id, :filename, :path, :conf, :status, :created_at, NOW())"
)


def test_revision_identifiers_are_bare_numbers() -> None:
    """019 chains off 018 using bare-number strings (no long migration names)."""
    migration_019 = _load_migration_019()
    assert migration_019.revision == "019"  # type: ignore[attr-defined]
    assert migration_019.down_revision == "018"  # type: ignore[attr-defined]
    assert migration_019.branch_labels is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_upgrade_019_dedupes_pending_and_creates_partial_unique_index() -> None:
    """019 collapses duplicate PENDING rows (keep newest), preserves APPROVED, and enforces the partial unique index."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "018")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    file_id = uuid.uuid4()
    newest_pending_id = uuid.uuid4()
    older_pending_id = uuid.uuid4()
    oldest_pending_id = uuid.uuid4()
    approved_id = uuid.uuid4()
    try:
        # Pre-condition: the partial unique index is absent at revision 018.
        async with engine.connect() as conn:
            names_before = {r.indexname for r in (await conn.execute(text(_INDEX_NAMES_SQL))).all()}
            assert "uq_proposals_file_id_pending" not in names_before, "partial unique index must not exist before 019"

        # Seed: 3 PENDING rows for one file (distinct created_at) + 1 APPROVED row.
        async with engine.begin() as conn:
            await conn.execute(text(_INSERT_FILE_SQL), {"id": file_id, "hash": "d" * 64, "path": "/music/dup.mp3", "name": "dup.mp3"})
            await conn.execute(
                text(_INSERT_PROPOSAL_SQL),
                {
                    "id": oldest_pending_id,
                    "file_id": file_id,
                    "filename": "oldest.mp3",
                    "path": "a",
                    "conf": 0.1,
                    "status": "pending",
                    "created_at": datetime(2026, 1, 1),
                },
            )
            await conn.execute(
                text(_INSERT_PROPOSAL_SQL),
                {
                    "id": older_pending_id,
                    "file_id": file_id,
                    "filename": "older.mp3",
                    "path": "b",
                    "conf": 0.5,
                    "status": "pending",
                    "created_at": datetime(2026, 2, 1),
                },
            )
            await conn.execute(
                text(_INSERT_PROPOSAL_SQL),
                {
                    "id": newest_pending_id,
                    "file_id": file_id,
                    "filename": "newest.mp3",
                    "path": "c",
                    "conf": 0.9,
                    "status": "pending",
                    "created_at": datetime(2026, 3, 1),
                },
            )
            await conn.execute(
                text(_INSERT_PROPOSAL_SQL),
                {
                    "id": approved_id,
                    "file_id": file_id,
                    "filename": "approved.mp3",
                    "path": "z",
                    "conf": 1.0,
                    "status": "approved",
                    "created_at": datetime(2026, 1, 15),
                },
            )

        # Apply the migration under test.
        await asyncio.to_thread(upgrade_to, cfg, "019")

        async with engine.connect() as conn:
            # Exactly one PENDING row remains for the file, and it is the most-recent one.
            pending_ids = [
                r.id for r in (await conn.execute(text("SELECT id FROM proposals WHERE file_id = :f AND status = 'pending'"), {"f": file_id})).all()
            ]
            assert pending_ids == [newest_pending_id], "dedupe must keep exactly the most-recent PENDING row"

            # The APPROVED row is untouched.
            approved_still = (await conn.execute(text("SELECT id FROM proposals WHERE id = :i"), {"i": approved_id})).scalar_one_or_none()
            assert approved_still == approved_id, "the APPROVED row must never be touched by the dedupe"

            # The partial unique index now exists.
            names_after = {r.indexname for r in (await conn.execute(text(_INDEX_NAMES_SQL))).all()}
            assert "uq_proposals_file_id_pending" in names_after, "partial unique index must exist after 019"

        # The index actually rejects a SECOND pending insert for the same file.
        with pytest.raises(Exception):  # noqa: B017 — asyncpg UniqueViolationError surfaces as a broad IntegrityError
            async with engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_PROPOSAL_SQL),
                    {
                        "id": uuid.uuid4(),
                        "file_id": file_id,
                        "filename": "second.mp3",
                        "path": "x",
                        "conf": 0.3,
                        "status": "pending",
                        "created_at": datetime(2026, 4, 1),
                    },
                )

        # A second APPROVED row is allowed (falls outside the partial index).
        async with engine.begin() as conn:
            await conn.execute(
                text(_INSERT_PROPOSAL_SQL),
                {
                    "id": uuid.uuid4(),
                    "file_id": file_id,
                    "filename": "approved2.mp3",
                    "path": "y",
                    "conf": 0.8,
                    "status": "approved",
                    "created_at": datetime(2026, 5, 1),
                },
            )
        async with engine.connect() as conn:
            approved_count = (
                await conn.execute(text("SELECT count(*) FROM proposals WHERE file_id = :f AND status = 'approved'"), {"f": file_id})
            ).scalar_one()
            assert approved_count == 2, "approved rows are outside the partial unique index"

        # Downgrade drops the index.
        await asyncio.to_thread(downgrade_to, cfg, "018")
        async with engine.connect() as conn:
            names_post_downgrade = {r.indexname for r in (await conn.execute(text(_INDEX_NAMES_SQL))).all()}
            assert "uq_proposals_file_id_pending" not in names_post_downgrade, "downgrade to 018 must drop the partial unique index"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
