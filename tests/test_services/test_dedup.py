"""Tests for duplicate detection service."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.services.dedup import (
    count_duplicate_groups,
    find_duplicate_groups,
    find_duplicate_groups_with_metadata,
    get_duplicate_stats,
    resolve_group,
    score_group,
    tag_completeness,
    undo_resolve,
)


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


# --- Helpers for new tests ---


def _make_metadata(file_id: uuid.UUID, **kwargs) -> FileMetadata:
    """Helper to create a FileMetadata with given fields."""
    return FileMetadata(
        id=uuid.uuid4(),
        file_id=file_id,
        **kwargs,
    )


# --- Scoring tests ---


def test_score_group_bitrate_wins() -> None:
    """Group with files at 128, 192, 320kbps -> canonical_id is the 320kbps file."""
    id_128 = str(uuid.uuid4())
    id_192 = str(uuid.uuid4())
    id_320 = str(uuid.uuid4())
    group = {
        "sha256_hash": HASH_A,
        "count": 3,
        "files": [
            {
                "id": id_128,
                "original_path": "/a/low.mp3",
                "file_size": 1000,
                "file_type": "mp3",
                "bitrate": 128,
                "artist": None,
                "title": None,
                "album": None,
                "year": None,
                "genre": None,
                "track_number": None,
                "duration": None,
                "tag_label": "None",
                "tag_filled": 0,
                "tag_total": 6,
            },
            {
                "id": id_192,
                "original_path": "/a/mid.mp3",
                "file_size": 1000,
                "file_type": "mp3",
                "bitrate": 192,
                "artist": None,
                "title": None,
                "album": None,
                "year": None,
                "genre": None,
                "track_number": None,
                "duration": None,
                "tag_label": "None",
                "tag_filled": 0,
                "tag_total": 6,
            },
            {
                "id": id_320,
                "original_path": "/a/high.mp3",
                "file_size": 1000,
                "file_type": "mp3",
                "bitrate": 320,
                "artist": None,
                "title": None,
                "album": None,
                "year": None,
                "genre": None,
                "track_number": None,
                "duration": None,
                "tag_label": "None",
                "tag_filled": 0,
                "tag_total": 6,
            },
        ],
    }

    score_group(group)

    assert group["canonical_id"] == id_320
    assert "highest bitrate (320kbps)" in group["rationale"]


def test_score_group_tag_tiebreak() -> None:
    """All files same bitrate (320), one has 5/6 tags, another 3/6 -> canonical is 5/6 file."""
    id_5tags = str(uuid.uuid4())
    id_3tags = str(uuid.uuid4())
    group = {
        "sha256_hash": HASH_A,
        "count": 2,
        "files": [
            {
                "id": id_3tags,
                "original_path": "/a/few.mp3",
                "file_size": 1000,
                "file_type": "mp3",
                "bitrate": 320,
                "artist": "Art",
                "title": "T",
                "album": "A",
                "year": None,
                "genre": None,
                "track_number": None,
                "duration": None,
                "tag_label": "Partial",
                "tag_filled": 3,
                "tag_total": 6,
            },
            {
                "id": id_5tags,
                "original_path": "/a/many.mp3",
                "file_size": 1000,
                "file_type": "mp3",
                "bitrate": 320,
                "artist": "Art",
                "title": "T",
                "album": "A",
                "year": 2020,
                "genre": "Rock",
                "track_number": None,
                "duration": None,
                "tag_label": "Partial",
                "tag_filled": 5,
                "tag_total": 6,
            },
        ],
    }

    score_group(group)

    assert group["canonical_id"] == id_5tags
    assert "most complete tags (5/6)" in group["rationale"]


def test_score_group_path_tiebreak() -> None:
    """All files same bitrate (320), same tag count (3/6), paths differ -> canonical is shorter path."""
    id_short = str(uuid.uuid4())
    id_long = str(uuid.uuid4())
    group = {
        "sha256_hash": HASH_A,
        "count": 2,
        "files": [
            {
                "id": id_long,
                "original_path": "/a/b/c/d.mp3",
                "file_size": 1000,
                "file_type": "mp3",
                "bitrate": 320,
                "artist": "A",
                "title": "T",
                "album": "Al",
                "year": None,
                "genre": None,
                "track_number": None,
                "duration": None,
                "tag_label": "Partial",
                "tag_filled": 3,
                "tag_total": 6,
            },
            {
                "id": id_short,
                "original_path": "/a/b.mp3",
                "file_size": 1000,
                "file_type": "mp3",
                "bitrate": 320,
                "artist": "A",
                "title": "T",
                "album": "Al",
                "year": None,
                "genre": None,
                "track_number": None,
                "duration": None,
                "tag_label": "Partial",
                "tag_filled": 3,
                "tag_total": 6,
            },
        ],
    }

    score_group(group)

    assert group["canonical_id"] == id_short
    assert "shortest path" in group["rationale"]


def test_score_group_no_metadata() -> None:
    """All files have None for bitrate and tags -> canonical is shortest path."""
    id_short = str(uuid.uuid4())
    id_long = str(uuid.uuid4())
    group = {
        "sha256_hash": HASH_A,
        "count": 2,
        "files": [
            {
                "id": id_long,
                "original_path": "/very/long/path/file.mp3",
                "file_size": 1000,
                "file_type": "mp3",
                "bitrate": None,
                "artist": None,
                "title": None,
                "album": None,
                "year": None,
                "genre": None,
                "track_number": None,
                "duration": None,
                "tag_label": "None",
                "tag_filled": 0,
                "tag_total": 6,
            },
            {
                "id": id_short,
                "original_path": "/a/b.mp3",
                "file_size": 1000,
                "file_type": "mp3",
                "bitrate": None,
                "artist": None,
                "title": None,
                "album": None,
                "year": None,
                "genre": None,
                "track_number": None,
                "duration": None,
                "tag_label": "None",
                "tag_filled": 0,
                "tag_total": 6,
            },
        ],
    }

    score_group(group)

    assert group["canonical_id"] == id_short
    assert "shortest path" in group["rationale"]


# --- Tag completeness tests ---


def test_tag_completeness_full() -> None:
    """File with all 6 tag fields -> ('Full', 6, 6)."""
    file_dict = {
        "artist": "Art",
        "title": "T",
        "album": "A",
        "year": 2020,
        "genre": "Rock",
        "track_number": 1,
    }
    label, filled, total = tag_completeness(file_dict)
    assert label == "Full"
    assert filled == 6
    assert total == 6


def test_tag_completeness_partial() -> None:
    """File with 3 tag fields -> ('Partial', 3, 6)."""
    file_dict = {
        "artist": "Art",
        "title": "T",
        "album": "A",
        "year": None,
        "genre": None,
        "track_number": None,
    }
    label, filled, total = tag_completeness(file_dict)
    assert label == "Partial"
    assert filled == 3
    assert total == 6


def test_tag_completeness_none() -> None:
    """File with no metadata -> ('None', 0, 6)."""
    file_dict = {
        "artist": None,
        "title": None,
        "album": None,
        "year": None,
        "genre": None,
        "track_number": None,
    }
    label, filled, total = tag_completeness(file_dict)
    assert label == "None"
    assert filled == 0
    assert total == 6


# --- Database-dependent tests ---


@pytest.mark.asyncio
async def test_find_duplicate_groups_excludes_resolved(session: AsyncSession) -> None:
    """Files with state=DUPLICATE_RESOLVED are excluded from grouping."""
    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/resolved.mp3", "mp3", HASH_A)
    f2.state = FileState.DUPLICATE_RESOLVED
    session.add_all([f1, f2])
    await session.flush()

    groups = await find_duplicate_groups(session)

    # Only 1 non-resolved file with this hash -> no duplicate group
    assert len(groups) == 0


@pytest.mark.asyncio
async def test_find_duplicate_groups_with_metadata_includes_bitrate(session: AsyncSession) -> None:
    """Returned file dicts include bitrate, duration, artist, title, album, genre, year, track_number keys."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    m1 = _make_metadata(f1.id, bitrate=320, duration=180.0, artist="Artist", title="Title", album="Album", year=2020, genre="Rock", track_number=1)
    session.add(m1)
    await session.flush()

    groups = await find_duplicate_groups_with_metadata(session)

    assert len(groups) == 1
    file_dicts = groups[0]["files"]
    # Check that metadata keys exist on all files
    expected_keys = {"bitrate", "duration", "artist", "title", "album", "genre", "year", "track_number"}
    for fd in file_dicts:
        assert expected_keys.issubset(fd.keys()), f"Missing keys: {expected_keys - fd.keys()}"

    # The file with metadata should have populated values
    f1_dict = next(fd for fd in file_dicts if fd["id"] == str(f1.id))
    assert f1_dict["bitrate"] == 320
    assert f1_dict["artist"] == "Artist"


@pytest.mark.asyncio
async def test_get_duplicate_stats(session: AsyncSession) -> None:
    """get_duplicate_stats returns group count, total file count, and recoverable bytes."""
    # Group A: 2 files, sizes 1000 and 2000 -> recoverable = 1000
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A, file_size=1000)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A, file_size=2000)
    # Group B: 3 files, sizes 500, 500, 1500 -> recoverable = 500 + 500 = 1000
    f3 = _make_file("/dir/b1.mp3", "mp3", HASH_B, file_size=500)
    f4 = _make_file("/dir/b2.mp3", "mp3", HASH_B, file_size=500)
    f5 = _make_file("/dir/b3.mp3", "mp3", HASH_B, file_size=1500)
    # Unique file: not counted
    f6 = _make_file("/dir/unique.mp3", "mp3", HASH_C, file_size=9999)
    session.add_all([f1, f2, f3, f4, f5, f6])
    await session.flush()

    stats = await get_duplicate_stats(session)

    assert stats["groups"] == 2
    assert stats["total_files"] == 5
    assert stats["recoverable_bytes"] == 2000


@pytest.mark.asyncio
async def test_resolve_group(session: AsyncSession) -> None:
    """resolve_group marks non-canonical files as DUPLICATE_RESOLVED."""
    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup1.mp3", "mp3", HASH_A)
    f3 = _make_file("/dir/dup2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2, f3])
    await session.flush()

    count, file_states = await resolve_group(session, HASH_A, f1.id)

    assert count == 2
    assert len(file_states) == 2
    # Check states changed
    await session.refresh(f2)
    await session.refresh(f3)
    assert f2.state == FileState.DUPLICATE_RESOLVED
    assert f3.state == FileState.DUPLICATE_RESOLVED
    # Canonical file untouched
    await session.refresh(f1)
    assert f1.state == FileState.DISCOVERED


@pytest.mark.asyncio
async def test_undo_resolve(session: AsyncSession) -> None:
    """undo_resolve restores file states from saved state list."""
    f1 = _make_file("/dir/a.mp3", "mp3", HASH_A)
    f1.state = FileState.DUPLICATE_RESOLVED
    f2 = _make_file("/dir/b.mp3", "mp3", HASH_A)
    f2.state = FileState.DUPLICATE_RESOLVED
    session.add_all([f1, f2])
    await session.flush()

    file_states = [
        {"id": str(f1.id), "previous_state": FileState.DISCOVERED},
        {"id": str(f2.id), "previous_state": FileState.METADATA_EXTRACTED},
    ]

    count = await undo_resolve(session, file_states)

    assert count == 2
    await session.refresh(f1)
    await session.refresh(f2)
    assert f1.state == FileState.DISCOVERED
    assert f2.state == FileState.METADATA_EXTRACTED
