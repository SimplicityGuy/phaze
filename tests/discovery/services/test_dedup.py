"""Tests for duplicate detection service."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.dedup import (
    count_duplicate_groups,
    find_duplicate_groups,
    find_duplicate_groups_by_hashes,
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
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=sha256_hash,
        original_path=original_path,
        original_filename=filename,
        current_path=original_path,
        file_type=file_type,
        file_size=file_size,
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


def test_dup_hash_subquery_orders_by_hash_before_limit_offset() -> None:
    """The paginated hash-selection subquery must ORDER BY sha256_hash before LIMIT/OFFSET.

    Without this, Postgres's ``GROUP BY ... HAVING`` aggregate output order is unspecified and
    plan-dependent, so LIMIT/OFFSET pagination over it can select a DIFFERENT set of hashes per call --
    silently repeating or skipping duplicate groups in the review UI (acceptance: stable page membership
    across repeated requests). No DB round-trip needed: this inspects the compiled SQL directly.
    """
    from phaze.services.dedup import _dup_hash_subquery

    compiled = str(_dup_hash_subquery(limit=20, offset=0).compile(compile_kwargs={"literal_binds": True}))
    order_by_idx = compiled.upper().find("ORDER BY")
    limit_idx = compiled.upper().find("LIMIT")

    assert order_by_idx != -1, "hash-selection subquery has no ORDER BY -- LIMIT/OFFSET pagination is unstable"
    assert limit_idx != -1
    assert order_by_idx < limit_idx, "ORDER BY must precede LIMIT so the paginated window is deterministic"


@pytest.mark.asyncio
async def test_pagination_is_stable_and_non_overlapping(session: AsyncSession) -> None:
    """Paging with LIMIT/OFFSET must not repeat or skip groups (regression for the missing ORDER BY).

    The hash-selection subquery paginates a ``GROUP BY ... HAVING`` with LIMIT/OFFSET but (pre-fix) no
    ORDER BY -- Postgres aggregate output order is unspecified and plan-dependent, so two identical calls,
    or two adjacent pages, can select a DIFFERENT set of hashes: a group shown twice, or never shown at
    all. Hashes are inserted out of lexical order so a stable fix (ORDER BY sha256_hash) is distinguishable
    from "insertion order happened to already look sorted".
    """
    for h in (HASH_D, HASH_A, HASH_C, HASH_B):
        session.add_all([_make_file(f"/dir/{h[0]}1.mp3", "mp3", h), _make_file(f"/dir/{h[0]}2.mp3", "mp3", h)])
    await session.flush()

    # Two independent calls with the SAME limit/offset must select the identical set of hashes.
    first = await find_duplicate_groups(session, limit=4, offset=0)
    second = await find_duplicate_groups(session, limit=4, offset=0)
    assert {g["sha256_hash"] for g in first} == {g["sha256_hash"] for g in second}

    # Adjacent pages (limit=2) must partition the 4 groups with NO overlap and NO gap -- every hash
    # appears on exactly one page, so a caller paging through sees each group exactly once.
    page1 = await find_duplicate_groups(session, limit=2, offset=0)
    page2 = await find_duplicate_groups(session, limit=2, offset=2)
    page1_hashes = {g["sha256_hash"] for g in page1}
    page2_hashes = {g["sha256_hash"] for g in page2}

    assert len(page1_hashes) == 2
    assert len(page2_hashes) == 2
    assert page1_hashes | page2_hashes == {HASH_A, HASH_B, HASH_C, HASH_D}
    assert not (page1_hashes & page2_hashes), "adjacent pages overlapped -- a group was shown twice"


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
    """Files carrying a dedup_resolution marker are excluded from grouping (marker is authority)."""
    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/resolved.mp3", "mp3", HASH_A)
    # Post-cutover the readers key on the marker (~dedup_resolved_clause()) alone -- the
    # dedup_resolution row is the sole authority; there is no scalar state to set.
    session.add_all([f1, f2])
    await session.flush()
    session.add(DedupResolution(file_id=f2.id, canonical_file_id=f1.id))
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
async def test_find_duplicate_groups_by_hashes_returns_only_requested_hashes(session: AsyncSession) -> None:
    """find_duplicate_groups_by_hashes returns EXACTLY the caller-supplied hashes, ignoring others.

    This is what ``bulk_resolve`` uses to act on the group hashes the operator was actually shown,
    instead of re-deriving "the current page" (see phaze-81bu): a group NOT in the requested hash set
    must never appear, even though it independently qualifies as a duplicate group.
    """
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    f3 = _make_file("/dir/b1.mp3", "mp3", HASH_B)
    f4 = _make_file("/dir/b2.mp3", "mp3", HASH_B)
    session.add_all([f1, f2, f3, f4])
    await session.flush()

    groups = await find_duplicate_groups_by_hashes(session, [HASH_A])

    assert len(groups) == 1
    assert groups[0]["sha256_hash"] == HASH_A
    assert groups[0]["count"] == 2


@pytest.mark.asyncio
async def test_find_duplicate_groups_by_hashes_empty_list_returns_empty(session: AsyncSession) -> None:
    """An empty hash list returns an empty list without hitting the database with an empty IN (...)."""
    f1 = _make_file("/dir/a1.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/a2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()

    groups = await find_duplicate_groups_by_hashes(session, [])

    assert groups == []


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
    """resolve_group writes the DedupResolution marker for non-canonical files (Phase 90 D-09: no files.state write)."""
    f1 = _make_file("/dir/keep.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/dup1.mp3", "mp3", HASH_A)
    f3 = _make_file("/dir/dup2.mp3", "mp3", HASH_A)
    session.add_all([f1, f2, f3])
    await session.flush()

    count, file_states = await resolve_group(session, HASH_A, f1.id)

    assert count == 2
    assert len(file_states) == 2
    # Phase 90 (D-09): the returned payload is id-only (no previous_state).
    assert all(set(entry.keys()) == {"id"} for entry in file_states)
    # The DedupResolution marker (the sole derived authority) was written for the two non-canonical files;
    # the canonical file has none.
    marker_ids = set((await session.execute(select(DedupResolution.file_id))).scalars().all())
    assert marker_ids == {f2.id, f3.id}


@pytest.mark.asyncio
async def test_undo_resolve(session: AsyncSession) -> None:
    """undo_resolve DELETEs the markers keyed on the payload id-set -- the sole undo authority (D-05/D-06)."""
    f1 = _make_file("/dir/a.mp3", "mp3", HASH_A)
    f2 = _make_file("/dir/b.mp3", "mp3", HASH_A)
    session.add_all([f1, f2])
    await session.flush()
    # Undo is a marker DELETE...RETURNING CAS: the ids must carry a marker to be undone.
    session.add_all([DedupResolution(file_id=f1.id), DedupResolution(file_id=f2.id)])
    await session.flush()

    # Phase 90 (D-09): the payload is id-only; no previous_state is captured or restored.
    file_states = [{"id": str(f1.id)}, {"id": str(f2.id)}]

    count = await undo_resolve(session, file_states)

    assert count == 2
    # Both markers were deleted -> the files derive ~dedup_resolved_clause() again.
    remaining = (await session.execute(select(DedupResolution.file_id))).scalars().all()
    assert remaining == []
