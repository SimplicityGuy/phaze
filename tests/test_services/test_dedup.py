"""Tests for duplicate detection service."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord, FileState
from phaze.services.dedup import count_duplicate_groups, find_duplicate_groups


def _make_file(
    original_path: str,
    file_type: str,
    sha256_hash: str,
    file_size: int = 1000,
) -> FileRecord:
    """Helper to create a FileRecord with explicit hash."""
    filename = original_path.rsplit("/", 1)[-1]
    return FileRecord(
        id=uuid.uuid4(),
        sha256_hash=sha256_hash,
        original_path=original_path,
        original_filename=filename,
        current_path=original_path,
        file_type=file_type,
        file_size=file_size,
        state=FileState.DISCOVERED,
    )


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


@pytest.mark.asyncio
async def test_three_files_same_hash_one_group(session: AsyncSession) -> None:
    """3 files with same sha256_hash -> 1 group with 3 members."""
    f1 = _make_file("/dir/file1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/file2.mp3", "mp3", HASH_A)
    f3 = _make_file("/other/file3.mp3", "mp3", HASH_A)
    session.add_all([f1, f2, f3])
    await session.flush()

    groups = await find_duplicate_groups(session)

    assert len(groups) == 1
    assert groups[0]["sha256_hash"] == HASH_A
    assert groups[0]["count"] == 3
    assert len(groups[0]["files"]) == 3


@pytest.mark.asyncio
async def test_unique_files_no_groups(session: AsyncSession) -> None:
    """Unique files (all different hashes) -> 0 groups."""
    f1 = _make_file("/dir/file1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/file2.mp3", "mp3", HASH_B)
    f3 = _make_file("/dir/file3.mp3", "mp3", HASH_C)
    session.add_all([f1, f2, f3])
    await session.flush()

    groups = await find_duplicate_groups(session)

    assert len(groups) == 0


@pytest.mark.asyncio
async def test_two_separate_duplicate_groups(session: AsyncSession) -> None:
    """2 separate duplicate groups -> 2 groups returned."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    f3 = _make_file("/dir/b1.flac", "flac", HASH_B)
    f4 = _make_file("/dir/b2.flac", "flac", HASH_B)
    f5 = _make_file("/dir/unique.ogg", "ogg", HASH_C)
    session.add_all([f1, f2, f3, f4, f5])
    await session.flush()

    groups = await find_duplicate_groups(session)

    assert len(groups) == 2
    hashes = {g["sha256_hash"] for g in groups}
    assert hashes == {HASH_A, HASH_B}


@pytest.mark.asyncio
async def test_pagination_limit(session: AsyncSession) -> None:
    """Pagination (limit=1) returns only 1 group."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    f3 = _make_file("/dir/b1.mp3", "mp3", HASH_B)
    f4 = _make_file("/dir/b2.mp3", "mp3", HASH_B)
    session.add_all([f1, f2, f3, f4])
    await session.flush()

    groups = await find_duplicate_groups(session, limit=1)

    assert len(groups) == 1


@pytest.mark.asyncio
async def test_count_duplicate_groups_correct(session: AsyncSession) -> None:
    """count_duplicate_groups returns correct total count."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    f3 = _make_file("/dir/b1.mp3", "mp3", HASH_B)
    f4 = _make_file("/dir/b2.mp3", "mp3", HASH_B)
    f5 = _make_file("/dir/c1.mp3", "mp3", HASH_C)
    f6 = _make_file("/dir/c2.mp3", "mp3", HASH_C)
    f7 = _make_file("/dir/unique.mp3", "mp3", HASH_D)
    session.add_all([f1, f2, f3, f4, f5, f6, f7])
    await session.flush()

    total = await count_duplicate_groups(session)

    assert total == 3
