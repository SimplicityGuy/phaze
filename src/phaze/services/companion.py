"""Companion association service: links companion files to media files in the same directory."""

from pathlib import PurePosixPath
from typing import Any, cast
import uuid

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.services.bulk_insert import chunk_rows


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
    running twice produces no duplicate links, including under CONCURRENT
    invocations (e.g. an HTMX double-submit of POST /associate) — the insert
    is ON CONFLICT DO NOTHING against uq_file_companions_pair, so a pair the
    other request already committed is silently skipped instead of raising
    IntegrityError and rolling back the whole batch.

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

    rows: list[dict[str, uuid.UUID]] = []
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
                # Explicit id: pg_insert bypasses FileCompanion.id's Python-side
                # default=uuid.uuid4 (dedup.resolve_group precedent).
                rows.append({"id": uuid.uuid4(), "companion_id": comp.id, "media_id": media.id})

    count = 0
    if rows:
        # phaze-p3qr: CHUNKED, because an explicit multi-row VALUES binds
        # `len(rows) * params_per_row` parameters in ONE statement and PostgreSQL's Bind
        # message caps that at int16 (32767) -- 10,922 rows at this model's 3 parameters
        # (id/companion_id/media_id), a threshold a conventional album directory (folder art +
        # a couple of scene-release sidecars against a dozen tracks) clears in roughly 230
        # directories on the large personal archive this project targets. `chunk_rows` derives
        # the split from the rows' actual parameter count (services/bulk_insert.py), so adding a
        # column to FileCompanion cannot silently reintroduce the break.
        #
        # The unlinked read above is a snapshot: a concurrent run computes the same pairs, and
        # whichever commits second would violate uq_file_companions_pair. ON CONFLICT DO NOTHING
        # makes that first-writer-wins; summing each chunk's rowcount keeps the return value
        # honest under races. An INSERT returns a CursorResult at runtime (exposing rowcount);
        # the async stubs type it as the base Result, so cast (agent_push.py precedent).
        #
        # ATOMICITY: every chunk executes on THIS session inside the SAME transaction (bulk_insert.py's
        # "atomicity is the caller's job" rule) -- the single `session.commit()` below is still the
        # only commit boundary, so a mid-loop failure rolls back every chunk, never a partial link set.
        for chunk in chunk_rows(rows):
            insert_stmt = pg_insert(FileCompanion).values(chunk).on_conflict_do_nothing(constraint="uq_file_companions_pair")
            result = cast("CursorResult[Any]", await session.execute(insert_stmt))
            count += result.rowcount

    await session.commit()
    return count
