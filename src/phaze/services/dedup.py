"""Duplicate detection service: finds files sharing the same SHA256 hash."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord


async def find_duplicate_groups(session: AsyncSession, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Find groups of files sharing the same SHA256 hash.

    Returns a paginated list of duplicate groups, each containing the
    shared hash, member count, and file details (id, path, size, type).
    """
    # Subquery: hashes that appear more than once
    dup_hashes = (
        select(FileRecord.sha256_hash).group_by(FileRecord.sha256_hash).having(func.count(FileRecord.id) > 1).limit(limit).offset(offset).subquery()
    )

    # Main query: all files matching those hashes
    stmt = (
        select(FileRecord)
        .where(FileRecord.sha256_hash.in_(select(dup_hashes.c.sha256_hash)))
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


async def count_duplicate_groups(session: AsyncSession) -> int:
    """Count the total number of duplicate groups (hashes with >1 file).

    Returns the number of distinct SHA256 hashes that have more than one file.
    """
    subq = select(FileRecord.sha256_hash).group_by(FileRecord.sha256_hash).having(func.count(FileRecord.id) > 1).subquery()
    stmt = select(func.count()).select_from(subq)
    result = await session.execute(stmt)
    return result.scalar_one()
