"""D-21 regression test: `RETURNING (xmax = 0) AS inserted` distinguishes INSERT from UPDATE.

Guards RESEARCH Pitfall 2 + Assumption A1: no triggers exist on `files` that
would set `xmax` non-zero on a fresh INSERT. Will fire if a future migration
adds a trigger to `files`, or on a Postgres major version bump that changes
MVCC HOT-update semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.models.file import FileRecord, FileState


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


@pytest.mark.asyncio
async def test_xmax_inserted_flag(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """D-21: brand-new (agent_id, original_path) -> inserted=True; same key -> inserted=False."""
    agent, _ = seed_test_agent

    record = {
        "id": uuid.uuid4(),
        "agent_id": agent.id,
        "sha256_hash": "0" * 64,
        "original_path": "/test/music/x.mp3",
        "original_filename": "x.mp3",
        "current_path": "/test/music/x.mp3",
        "file_type": "mp3",
        "file_size": 100,
        "state": FileState.DISCOVERED,
    }

    # 1. First UPSERT: brand-new key -> inserted=True
    stmt = (
        pg_insert(FileRecord)
        .values([record])
        .on_conflict_do_update(
            index_elements=["agent_id", "original_path"],
            set_={"file_size": pg_insert(FileRecord).excluded.file_size},
        )
        .returning(FileRecord.id, literal_column("(xmax = 0)").label("inserted"))
    )
    rows = (await session.execute(stmt)).all()
    await session.commit()
    assert len(rows) == 1
    assert rows[0].inserted is True, f"Expected inserted=True for fresh INSERT, got {rows[0].inserted}"

    # 2. Second UPSERT: same natural key, new id, bumped file_size -> inserted=False
    record["id"] = uuid.uuid4()
    record["file_size"] = 200
    stmt = (
        pg_insert(FileRecord)
        .values([record])
        .on_conflict_do_update(
            index_elements=["agent_id", "original_path"],
            set_={"file_size": pg_insert(FileRecord).excluded.file_size},
        )
        .returning(FileRecord.id, literal_column("(xmax = 0)").label("inserted"))
    )
    rows = (await session.execute(stmt)).all()
    await session.commit()
    assert len(rows) == 1
    assert rows[0].inserted is False, f"Expected inserted=False for UPDATE replay, got {rows[0].inserted}"
