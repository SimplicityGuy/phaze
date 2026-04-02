"""Pipeline orchestration service -- stage counts and file queries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from phaze.models.file import FileRecord, FileState


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# The pipeline stages in order, for display
PIPELINE_STAGES = [
    FileState.DISCOVERED,
    FileState.METADATA_EXTRACTED,
    FileState.FINGERPRINTED,
    FileState.ANALYZED,
    FileState.PROPOSAL_GENERATED,
    FileState.APPROVED,
    FileState.DUPLICATE_RESOLVED,
    FileState.EXECUTED,
]


async def get_pipeline_stats(session: AsyncSession) -> dict[str, int]:
    """Get file counts per pipeline stage.

    Returns dict mapping state name to count, e.g.:
    {"discovered": 42, "analyzed": 10, "proposal_generated": 5, ...}
    """
    stmt = select(FileRecord.state, func.count(FileRecord.id)).group_by(FileRecord.state)
    result = await session.execute(stmt)
    counts: dict[str, int] = {row[0]: row[1] for row in result.all()}
    # Ensure all stages are present (default 0)
    return {stage.value: counts.get(stage.value, 0) for stage in PIPELINE_STAGES}


async def get_files_by_state(session: AsyncSession, state: FileState) -> list[FileRecord]:
    """Get all files in a given pipeline state.

    Args:
        session: Async database session.
        state: The FileState to filter by.

    Returns:
        List of FileRecord objects in the given state.
    """
    stmt = select(FileRecord).where(FileRecord.state == state)
    result = await session.execute(stmt)
    return list(result.scalars().all())
