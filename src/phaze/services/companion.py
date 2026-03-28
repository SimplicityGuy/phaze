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


async def associate_companions(session: AsyncSession) -> int:
    """Link unlinked companion files to media files in the same directory.

    Finds all companion FileRecords not yet present in file_companions,
    groups them by directory, and creates FileCompanion links to every
    media file in that same directory. Idempotent: running twice produces
    no duplicate links.

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

    # Group companions by parent directory
    dir_groups: dict[str, list[FileRecord]] = {}
    for comp in unlinked_companions:
        parent = str(PurePosixPath(comp.original_path).parent)
        dir_groups.setdefault(parent, []).append(comp)

    count = 0
    for directory, companions in dir_groups.items():
        # Find media files in the same directory (not subdirs)
        media_stmt = select(FileRecord).where(
            FileRecord.file_type.in_(MEDIA_TYPES),
            FileRecord.original_path.like(f"{directory}/%"),
            ~FileRecord.original_path.like(f"{directory}/%/%"),
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
