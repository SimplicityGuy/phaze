"""Companion association service: links companion files to media files in the same directory."""

from pathlib import PurePosixPath
from typing import Any, cast
import uuid

from sqlalchemy import CursorResult, and_, or_, select
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

DEFAULT_ASSOCIATE_BATCH_SIZE = 2_000
"""Unlinked companion FileRecords read per keyset page in :func:`associate_companions`
(phaze-yiwq5). The pre-fix query materialized the WHOLE unlinked-companion corpus with a single
unbounded ``.scalars().all()`` and built the full per-directory link cross-product from it in one
shot -- on an archive with a large never-associated backlog that is an unbounded allocation in the
api process, reachable from an operator-triggered ``POST /associate``. Paging by
``FileRecord.id`` bounds peak memory to one page's companions plus the media cross-product for the
directories that page touches, regardless of how large the total backlog is."""


def _escape_like(value: str) -> str:
    """Escape LIKE metacharacters (backslash, %, _) so a filesystem path can be used
    safely as a literal prefix in a SQL LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def associate_companions(session: AsyncSession, *, batch_size: int = DEFAULT_ASSOCIATE_BATCH_SIZE) -> int:
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

    PAGING (phaze-yiwq5): unlinked companions are read in keyset pages of *batch_size*, ordered
    by ``FileRecord.id`` -- never materialized in one unbounded ``.scalars().all()`` -- so peak
    memory is bounded by one page's companions plus the media cross-product for the directories
    that page happens to touch, regardless of the total backlog size. Each page commits its own
    links before the next page is read, so a companion a prior page already linked is excluded
    from later pages by BOTH the ``NOT IN`` subquery and the ``id > cursor`` bound. This trades the
    single-run all-or-nothing atomicity the earlier unbounded version had for a bounded memory
    footprint: a failure mid-sweep leaves whatever earlier pages already committed in place rather
    than rolling back the whole run, which is safe precisely because the function is idempotent --
    a retry (or the next scheduled run) picks up wherever the backlog was left.

    Returns the number of new links created across every page.
    """
    already_linked_subq = select(FileCompanion.companion_id)

    count = 0
    after: uuid.UUID | None = None
    while True:
        # Keyset-paged unlinked-companion read: LIMIT batch_size, ordered by id, resuming past
        # the last id this call has already consumed. Bounded regardless of backlog size.
        stmt = (
            select(FileRecord)
            .where(
                FileRecord.file_type.in_(COMPANION_TYPES),
                FileRecord.id.notin_(already_linked_subq),
            )
            .order_by(FileRecord.id)
            .limit(batch_size)
        )
        if after is not None:
            stmt = stmt.where(FileRecord.id > after)
        result = await session.execute(stmt)
        unlinked_companions = result.scalars().all()

        if not unlinked_companions:
            break
        after = unlinked_companions[-1].id

        # Group this page's companions by (agent, parent directory) -- the directory string
        # alone is ambiguous across agents.
        dir_groups: dict[tuple[str, str], list[FileRecord]] = {}
        for comp in unlinked_companions:
            parent = str(PurePosixPath(comp.original_path).parent)
            dir_groups.setdefault((comp.agent_id, parent), []).append(comp)

        # phaze-vu88k.3: ONE query for every (agent, directory) group in this page, instead of one
        # per group. A page holds at most `batch_size` companions, so at most that many distinct
        # groups -- 4 bind params per OR'd clause, well under asyncpg's 32767 cap. Media rows are
        # re-bucketed below by their OWN (agent_id, parent directory), computed the same way
        # `dir_groups` was, rather than by which OR clause matched them: the LIKE/NOT-LIKE pair
        # only matches rows whose parent IS that literal directory, so recomputing the parent from
        # each returned row reconstructs the exact same grouping the one-query-per-group form did.
        media_conditions = [
            and_(
                FileRecord.agent_id == agent_id,
                FileRecord.file_type.in_(MEDIA_TYPES),
                # Escape LIKE metacharacters in the directory so '_'/'%'/'\' in a real
                # path (e.g. "Coachella_2024") are matched literally rather than as wildcards.
                FileRecord.original_path.like(f"{_escape_like(directory)}/%", escape=_LIKE_ESCAPE_CHAR),
                ~FileRecord.original_path.like(f"{_escape_like(directory)}/%/%", escape=_LIKE_ESCAPE_CHAR),
            )
            for agent_id, directory in dir_groups
        ]
        media_result = await session.execute(select(FileRecord).where(or_(*media_conditions)))
        media_by_group: dict[tuple[str, str], list[FileRecord]] = {}
        for media in media_result.scalars().all():
            parent = str(PurePosixPath(media.original_path).parent)
            media_by_group.setdefault((media.agent_id, parent), []).append(media)

        rows: list[dict[str, uuid.UUID]] = []
        for (agent_id, directory), companions in dir_groups.items():
            media_files = media_by_group.get((agent_id, directory))
            if not media_files:
                continue

            for comp in companions:
                for media in media_files:
                    # Explicit id: pg_insert bypasses FileCompanion.id's Python-side
                    # default=uuid.uuid4 (dedup.resolve_group precedent).
                    rows.append({"id": uuid.uuid4(), "companion_id": comp.id, "media_id": media.id})

        if rows:
            # phaze-p3qr: CHUNKED, because an explicit multi-row VALUES binds
            # `len(rows) * params_per_row` parameters in ONE statement and PostgreSQL's Bind
            # message caps that at int16 (32767) -- 10,922 rows at this model's 3 parameters
            # (id/companion_id/media_id), a threshold a conventional album directory (folder art +
            # a couple of scene-release sidecars against a dozen tracks) clears in roughly 230
            # directories on the large personal archive this project targets. `chunk_rows` derives
            # the split from the rows' actual parameter count (services/bulk_insert.py), so adding a
            # column to FileCompanion cannot silently reintroduce the break. A single PAGE's cross
            # product can still cross this bound even with batch_size bounding the companion count,
            # so chunking stays regardless of the outer paging.
            #
            # The unlinked read above is a snapshot: a concurrent run computes the same pairs, and
            # whichever commits second would violate uq_file_companions_pair. ON CONFLICT DO NOTHING
            # makes that first-writer-wins; summing each chunk's rowcount keeps the return value
            # honest under races. An INSERT returns a CursorResult at runtime (exposing rowcount);
            # the async stubs type it as the base Result, so cast (agent_push.py precedent).
            #
            # ATOMICITY: every chunk executes on THIS session inside the SAME transaction
            # (bulk_insert.py's "atomicity is the caller's job" rule) -- the `session.commit()`
            # below is this PAGE's commit boundary, so a mid-page failure rolls back every chunk in
            # THIS page (never a partial link set for one page), while earlier pages already
            # committed stay committed (see the paging note in the docstring above).
            for chunk in chunk_rows(rows):
                insert_stmt = pg_insert(FileCompanion).values(chunk).on_conflict_do_nothing(constraint="uq_file_companions_pair")
                insert_result = cast("CursorResult[Any]", await session.execute(insert_stmt))
                count += insert_result.rowcount

        await session.commit()

        if len(unlinked_companions) < batch_size:
            break

    return count
