"""Tests for the pipeline orchestration service."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.services.pipeline import get_files_by_state, get_pipeline_stats


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_get_pipeline_stats_empty(session: AsyncSession):
    """Empty database returns zero counts for all stages."""
    stats = await get_pipeline_stats(session)
    assert stats["discovered"] == 0
    assert stats["metadata_extracted"] == 0
    assert stats["analyzed"] == 0
    assert stats["proposal_generated"] == 0
    assert stats["approved"] == 0
    assert stats["executed"] == 0


@pytest.mark.asyncio
async def test_get_pipeline_stats_counts(session: AsyncSession):
    """Stats reflect actual file counts per state."""
    for i in range(3):
        f = FileRecord(
            id=uuid.uuid4(),
            sha256_hash=f"abc{i:064d}"[:64],
            original_path=f"/music/test{i}.mp3",
            original_filename=f"test{i}.mp3",
            current_path=f"/music/test{i}.mp3",
            file_type="mp3",
            file_size=1000,
            state=FileState.DISCOVERED,
        )
        session.add(f)
    session.add(
        FileRecord(
            id=uuid.uuid4(),
            sha256_hash="xyz0" + "0" * 60,
            original_path="/music/done.mp3",
            original_filename="done.mp3",
            current_path="/music/done.mp3",
            file_type="mp3",
            file_size=1000,
            state=FileState.ANALYZED,
        )
    )
    await session.commit()
    stats = await get_pipeline_stats(session)
    assert stats["discovered"] == 3
    assert stats["analyzed"] == 1


@pytest.mark.asyncio
async def test_get_pipeline_stats_includes_metadata_extracted(session: AsyncSession):
    """Stats include METADATA_EXTRACTED state count."""
    f = FileRecord(
        id=uuid.uuid4(),
        sha256_hash="m" * 64,
        original_path="/music/tagged.mp3",
        original_filename="tagged.mp3",
        current_path="/music/tagged.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.METADATA_EXTRACTED,
    )
    session.add(f)
    await session.commit()
    stats = await get_pipeline_stats(session)
    assert stats["metadata_extracted"] == 1


@pytest.mark.asyncio
async def test_get_files_by_state(session: AsyncSession):
    """get_files_by_state returns only files in the requested state."""
    f1 = FileRecord(
        id=uuid.uuid4(),
        sha256_hash="a" * 64,
        original_path="/music/a.mp3",
        original_filename="a.mp3",
        current_path="/music/a.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.DISCOVERED,
    )
    f2 = FileRecord(
        id=uuid.uuid4(),
        sha256_hash="b" * 64,
        original_path="/music/b.mp3",
        original_filename="b.mp3",
        current_path="/music/b.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.ANALYZED,
    )
    session.add_all([f1, f2])
    await session.commit()
    discovered = await get_files_by_state(session, FileState.DISCOVERED)
    assert len(discovered) == 1
    assert discovered[0].id == f1.id
