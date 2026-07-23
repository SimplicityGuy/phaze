"""Companion association service: links companion files to media files in the same directory."""

from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion


MEDIA_CATEGORIES: set[FileCategory] = {FileCategory.MUSIC, FileCategory.VIDEO}
COMPANION_TYPES: set[str] = {ext.lstrip(".") for ext, cat in EXTENSION_MAP.items() if cat == FileCategory.COMPANION}
MEDIA_TYPES: set[str] = {ext.lstrip(".") for ext, cat in EXTENSION_MAP.items() if cat in MEDIA_CATEGORIES}

_LIKE_ESCAPE_CHAR = "\\"


def _escape_like(value: str) -> str:
    """Escape LIKE metacharacters (backslash, %, _) so a filesystem path can be used
    safely as a literal prefix in a SQL LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def associate_companions(session: AsyncSession) -> int:
    """Link unlinked companion files to media files in the same directory.

    Finds all companion FileRecords not yet present in file_companions,
    groups them by (agent, directory), and creates FileCompanion links to
    every media file in that same directory ON THE SAME AGENT. Idempotent:
    running twice produces no duplicate links.

    original_path is only unique per agent (uq_files_agent_id_original_path),
    so two fileserver agents can hold files at the identical path; without the
    agent scoping a companion would link to media on every agent sharing the
    directory path, pairing files from unrelated recordings.

    Returns the number of new links created.
    """
    # Find companion file IDs that are already linked
    already_linked_subq = select(FileCompanion.companion_id)

    # Query unlinked companions
    stmt = select(FileRecord).where(
        FileRecord.file_type.in_(COMPANION_TYPES),
        FileRecord.id.notin_(already_linked_subq),
    )
    result = await session.execute(stmt)
    unlinked_companions = result.scalars().all()

    if not unlinked_companions:
        return 0

    # Group companions by (agent, parent directory) -- the directory string alone
    # is ambiguous across agents.
    dir_groups: dict[tuple[str, str], list[FileRecord]] = {}
    for comp in unlinked_companions:
        parent = str(PurePosixPath(comp.original_path).parent)
        dir_groups.setdefault((comp.agent_id, parent), []).append(comp)

    count = 0
    for (agent_id, directory), companions in dir_groups.items():
        # Find media files in the same directory (not subdirs) on the same agent.
        # Escape LIKE metacharacters in the directory so '_'/'%'/'\' in a real
        # path (e.g. "Coachella_2024") are matched literally rather than as wildcards.
        escaped_directory = _escape_like(directory)
        media_stmt = select(FileRecord).where(
            FileRecord.agent_id == agent_id,
            FileRecord.file_type.in_(MEDIA_TYPES),
            FileRecord.original_path.like(f"{escaped_directory}/%", escape=_LIKE_ESCAPE_CHAR),
            ~FileRecord.original_path.like(f"{escaped_directory}/%/%", escape=_LIKE_ESCAPE_CHAR),
        )
        media_result = await session.execute(media_stmt)
        media_files = media_result.scalars().all()

        if not media_files:
            continue

        for comp in companions:
            for media in media_files:
                link = FileCompanion(companion_id=comp.id, media_id=media.id)
                session.add(link)
                count += 1

    await session.commit()
    return count
