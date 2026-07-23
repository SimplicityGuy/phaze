"""Tests for the idempotent `original_filename_repaired` backfill (phaze-x4ux).

Bucket: ``discovery`` (owns FileRecord ingestion). DB-backed -- exercises the real
``files.original_filename_repaired`` column added by migration 045.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.models.file import FileRecord
from phaze.services.text_repair_backfill import backfill_repaired_filenames


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _make_file(session: AsyncSession, original_filename: str) -> FileRecord:
    file_record = FileRecord(
        id=uuid.uuid4(),
        agent_id="test-fileserver",
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{original_filename}",
        original_filename=original_filename,
        current_path=f"/music/{original_filename}",
        file_type="mp3",
        file_size=1_000,
    )
    session.add(file_record)
    await session.commit()
    await session.refresh(file_record)
    return file_record


@pytest.mark.asyncio
async def test_backfill_repairs_mojibake_row(session: AsyncSession) -> None:
    garbled = "Carl Cox, Umek, Dj Rush, Chris Liebing, Sven VÃƒÂ¤th - LIVE @ Timewarp 2003.mp3"
    file_record = await _make_file(session, garbled)
    assert file_record.original_filename_repaired is None

    visited = await backfill_repaired_filenames(session)

    assert visited == 1
    await session.refresh(file_record)
    assert file_record.original_filename == garbled  # untouched, byte-faithful
    assert file_record.original_filename_repaired == "Carl Cox, Umek, Dj Rush, Chris Liebing, Sven Väth - LIVE @ Timewarp 2003.mp3"


@pytest.mark.asyncio
async def test_backfill_sets_clean_rows_to_their_own_value(session: AsyncSession) -> None:
    """A row with no mojibake still gets `original_filename_repaired` populated (equal to the
    original) so it is never re-selected -- see the module docstring on idempotency."""
    file_record = await _make_file(session, "Carl Cox - Live.mp3")

    visited = await backfill_repaired_filenames(session)

    assert visited == 1
    await session.refresh(file_record)
    assert file_record.original_filename_repaired == "Carl Cox - Live.mp3"


@pytest.mark.asyncio
async def test_backfill_is_idempotent_second_run_visits_nothing(session: AsyncSession) -> None:
    await _make_file(session, "Sven VÃƒÂ¤th.mp3")

    first = await backfill_repaired_filenames(session)
    second = await backfill_repaired_filenames(session)

    assert first == 1
    assert second == 0


@pytest.mark.asyncio
async def test_backfill_never_touches_already_backfilled_rows(session: AsyncSession) -> None:
    """A row that already carries `original_filename_repaired` (e.g. set at ingest by
    agent_files.py) is never re-selected or re-written."""
    file_record = await _make_file(session, "Sven VÃƒÂ¤th.mp3")
    file_record.original_filename_repaired = "already set by ingest"
    await session.commit()

    visited = await backfill_repaired_filenames(session)

    assert visited == 0
    await session.refresh(file_record)
    assert file_record.original_filename_repaired == "already set by ingest"


@pytest.mark.asyncio
async def test_backfill_processes_in_batches(session: AsyncSession) -> None:
    """Multiple NULL rows are all visited even when batch_size is smaller than the row count."""
    for i in range(5):
        await _make_file(session, f"Sven VÃƒÂ¤th {i}.mp3")

    visited = await backfill_repaired_filenames(session, batch_size=2)

    assert visited == 5
    result = await session.execute(select(FileRecord).where(FileRecord.original_filename_repaired.is_(None)))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_backfill_returns_zero_when_no_files(session: AsyncSession) -> None:
    visited = await backfill_repaired_filenames(session)
    assert visited == 0
