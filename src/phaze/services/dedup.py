"""Duplicate detection service: finds files sharing the same SHA256 hash."""

from typing import Any
import uuid as uuid_mod

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata


TAG_FIELDS = ["artist", "title", "album", "year", "genre", "track_number"]


def tag_completeness(file_dict: dict[str, Any]) -> tuple[str, int, int]:
    """Return (label, filled_count, total_count) for tag completeness.

    Label is "Full" if all 6 fields present, "Partial" if 1-5, "None" if 0.
    """
    total = len(TAG_FIELDS)
    filled = sum(1 for field in TAG_FIELDS if file_dict.get(field) is not None)
    if filled == total:
        label = "Full"
    elif filled > 0:
        label = "Partial"
    else:
        label = "None"
    return label, filled, total


def score_group(group: dict[str, Any]) -> None:
    """Select canonical file and generate rationale string.

    Ranking: highest bitrate -> most complete tags -> shortest path.
    Mutates group in-place, setting canonical_id and rationale.
    """
    files = group["files"]

    def sort_key(f: dict[str, Any]) -> tuple[int, int, int]:
        bitrate = f.get("bitrate") or 0
        tag_count = f.get("tag_filled", 0)
        path_len = len(f.get("original_path", ""))
        # Negate path_len so shorter paths sort first (higher value = better)
        return (bitrate, tag_count, -path_len)

    files.sort(key=sort_key, reverse=True)
    winner = files[0]
    group["canonical_id"] = winner["id"]

    winner_bitrate = winner.get("bitrate") or 0
    winner_tags = winner.get("tag_filled", 0)
    winner_tag_total = winner.get("tag_total", len(TAG_FIELDS))

    # Determine what actually differentiated the winner from the runner-up
    runner_up = files[1] if len(files) > 1 else None
    runner_bitrate = (runner_up.get("bitrate") or 0) if runner_up else 0
    runner_tags = (runner_up.get("tag_filled", 0)) if runner_up else 0

    if winner_bitrate > 0 and winner_bitrate > runner_bitrate:
        group["rationale"] = f"highest bitrate ({winner_bitrate}kbps)"
    elif winner_tags > 0 and winner_tags > runner_tags:
        group["rationale"] = f"most complete tags ({winner_tags}/{winner_tag_total})"
    else:
        group["rationale"] = "shortest path"


async def find_duplicate_groups(session: AsyncSession, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Find groups of files sharing the same SHA256 hash.

    Returns a paginated list of duplicate groups, each containing the
    shared hash, member count, and file details (id, path, size, type).
    Excludes files with state DUPLICATE_RESOLVED.
    """
    # Subquery: hashes that appear more than once (excluding resolved files)
    dup_hashes = (
        select(FileRecord.sha256_hash)
        .where(FileRecord.state != FileState.DUPLICATE_RESOLVED)
        .group_by(FileRecord.sha256_hash)
        .having(func.count(FileRecord.id) > 1)
        .limit(limit)
        .offset(offset)
        .subquery()
    )

    # Main query: all non-resolved files matching those hashes
    stmt = (
        select(FileRecord)
        .where(FileRecord.sha256_hash.in_(select(dup_hashes.c.sha256_hash)))
        .where(FileRecord.state != FileState.DUPLICATE_RESOLVED)
        .order_by(FileRecord.sha256_hash, FileRecord.original_path)
    )
    result = await session.execute(stmt)
    files = result.scalars().all()

    # Group by hash
    groups_map: dict[str, list[dict[str, Any]]] = {}
    for f in files:
        groups_map.setdefault(f.sha256_hash, []).append(
            {
                "id": str(f.id),
                "original_path": f.original_path,
                "file_size": f.file_size,
                "file_type": f.file_type,
            }
        )

    return [
        {
            "sha256_hash": h,
            "count": len(members),
            "files": members,
        }
        for h, members in groups_map.items()
    ]


async def find_duplicate_groups_with_metadata(session: AsyncSession, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Find duplicate groups with metadata fields included.

    Like find_duplicate_groups but outer-joins FileMetadata to include
    bitrate, duration, artist, title, album, genre, year, track_number
    and tag completeness info in each file dict.
    """
    # Subquery: hashes that appear more than once (excluding resolved files)
    dup_hashes = (
        select(FileRecord.sha256_hash)
        .where(FileRecord.state != FileState.DUPLICATE_RESOLVED)
        .group_by(FileRecord.sha256_hash)
        .having(func.count(FileRecord.id) > 1)
        .limit(limit)
        .offset(offset)
        .subquery()
    )

    # Main query with outerjoin to metadata
    stmt = (
        select(FileRecord, FileMetadata)
        .outerjoin(FileMetadata, FileRecord.id == FileMetadata.file_id)
        .where(FileRecord.sha256_hash.in_(select(dup_hashes.c.sha256_hash)))
        .where(FileRecord.state != FileState.DUPLICATE_RESOLVED)
        .order_by(FileRecord.sha256_hash, FileRecord.original_path)
    )
    result = await session.execute(stmt)
    rows = result.all()

    # Group by hash
    groups_map: dict[str, list[dict[str, Any]]] = {}
    for file_record, metadata in rows:
        file_dict: dict[str, Any] = {
            "id": str(file_record.id),
            "original_path": file_record.original_path,
            "file_size": file_record.file_size,
            "file_type": file_record.file_type,
            "bitrate": metadata.bitrate if metadata else None,
            "duration": metadata.duration if metadata else None,
            "artist": metadata.artist if metadata else None,
            "title": metadata.title if metadata else None,
            "album": metadata.album if metadata else None,
            "genre": metadata.genre if metadata else None,
            "year": metadata.year if metadata else None,
            "track_number": metadata.track_number if metadata else None,
        }
        label, filled, total = tag_completeness(file_dict)
        file_dict["tag_label"] = label
        file_dict["tag_filled"] = filled
        file_dict["tag_total"] = total
        groups_map.setdefault(file_record.sha256_hash, []).append(file_dict)

    return [
        {
            "sha256_hash": h,
            "count": len(members),
            "files": members,
        }
        for h, members in groups_map.items()
    ]


async def count_duplicate_groups(session: AsyncSession) -> int:
    """Count the total number of duplicate groups (hashes with >1 file).

    Returns the number of distinct SHA256 hashes that have more than one file.
    Excludes files with state DUPLICATE_RESOLVED.
    """
    subq = (
        select(FileRecord.sha256_hash)
        .where(FileRecord.state != FileState.DUPLICATE_RESOLVED)
        .group_by(FileRecord.sha256_hash)
        .having(func.count(FileRecord.id) > 1)
        .subquery()
    )
    stmt = select(func.count()).select_from(subq)
    result = await session.execute(stmt)
    return result.scalar_one()


async def get_duplicate_stats(session: AsyncSession) -> dict[str, Any]:
    """Return duplicate statistics: groups, total_files, recoverable_bytes.

    recoverable_bytes = total size of all duplicate files minus the largest
    file per group (i.e., what could be reclaimed by keeping only one per group).
    """
    groups = await count_duplicate_groups(session)

    # Subquery: hashes with >1 file (excluding resolved)
    dup_hashes = (
        select(FileRecord.sha256_hash)
        .where(FileRecord.state != FileState.DUPLICATE_RESOLVED)
        .group_by(FileRecord.sha256_hash)
        .having(func.count(FileRecord.id) > 1)
        .subquery()
    )

    # Stats: total files and total size across duplicate groups
    stats_stmt = select(
        func.count(FileRecord.id).label("total_files"),
        func.sum(FileRecord.file_size).label("total_size"),
    ).where(
        FileRecord.sha256_hash.in_(select(dup_hashes.c.sha256_hash)),
        FileRecord.state != FileState.DUPLICATE_RESOLVED,
    )
    stats_result = await session.execute(stats_stmt)
    stats_row = stats_result.one()
    total_files = stats_row.total_files or 0
    total_size = stats_row.total_size or 0

    # Max file size per group (what we keep) -- use subquery to avoid nested aggregates
    max_per_group_subq = (
        select(
            func.max(FileRecord.file_size).label("max_size"),
        )
        .where(
            FileRecord.sha256_hash.in_(select(dup_hashes.c.sha256_hash)),
            FileRecord.state != FileState.DUPLICATE_RESOLVED,
        )
        .group_by(FileRecord.sha256_hash)
        .subquery()
    )
    max_per_group_stmt = select(func.sum(max_per_group_subq.c.max_size).label("kept_size"))
    max_result = await session.execute(max_per_group_stmt)
    kept_size = max_result.scalar_one() or 0

    return {
        "groups": groups,
        "total_files": total_files,
        "recoverable_bytes": total_size - kept_size,
    }


async def resolve_group(session: AsyncSession, group_hash: str, canonical_id: uuid_mod.UUID) -> tuple[int, list[dict[str, Any]]]:
    """Mark non-canonical files in a duplicate group as DUPLICATE_RESOLVED.

    Returns (count_resolved, [{id, previous_state}]) for undo tracking.
    """
    # Find all files in this group except the canonical one
    stmt = select(FileRecord).where(
        FileRecord.sha256_hash == group_hash,
        FileRecord.id != canonical_id,
        FileRecord.state != FileState.DUPLICATE_RESOLVED,
    )
    result = await session.execute(stmt)
    files = result.scalars().all()

    file_states: list[dict[str, Any]] = []
    for f in files:
        file_states.append({"id": str(f.id), "previous_state": f.state})
        f.state = FileState.DUPLICATE_RESOLVED

    await session.flush()
    return len(file_states), file_states


async def undo_resolve(session: AsyncSession, file_states: list[dict[str, Any]]) -> int:
    """Restore file states from a saved state list.

    Accepts list of {id, previous_state} dicts. Returns count restored.
    """
    count = 0
    for entry in file_states:
        file_id = uuid_mod.UUID(entry["id"]) if isinstance(entry["id"], str) else entry["id"]
        stmt = update(FileRecord).where(FileRecord.id == file_id).values(state=entry["previous_state"])
        await session.execute(stmt)
        count += 1
    await session.flush()
    return count
