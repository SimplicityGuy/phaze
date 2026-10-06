"""Companion association service: links companion files to media in their own directory, or name-matched parent media."""

from collections.abc import Collection, Sequence
from pathlib import PurePosixPath
from typing import Any, cast
import uuid

from sqlalchemy import ColumnElement, CursorResult, and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.constants import EXTENSION_MAP, FileCategory, companion_match_key
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


def _group_by_directory(files: Sequence[FileRecord]) -> dict[tuple[str, str], list[FileRecord]]:
    """Bucket ``files`` by ``(agent_id, parent directory)``.

    The directory string ALONE is ambiguous across agents: ``original_path`` is unique only per
    agent (uq_files_agent_id_original_path), so two fileserver agents can hold files at the
    identical path. Keying on the pair is what stops a companion linking to media on every agent
    that happens to share a directory path, pairing files from unrelated recordings.

    Used for both sides of the association -- this page's companions and the media rows fetched for
    them -- so both sides are bucketed by an identically-computed key.
    """
    groups: dict[tuple[str, str], list[FileRecord]] = {}
    for record in files:
        parent = str(PurePosixPath(record.original_path).parent)
        groups.setdefault((record.agent_id, parent), []).append(record)
    return groups


async def _media_in_directories(session: AsyncSession, dir_groups: Collection[tuple[str, str]]) -> dict[tuple[str, str], list[FileRecord]]:
    """Fetch every media file sitting directly in one of the ``(agent_id, directory)`` keys, bucketed the same way.

    phaze-vu88k.3: ONE query for every (agent, directory) group in this page, instead of one per
    group. A page holds at most ``batch_size`` companions, so at most that many distinct groups.

    phaze-6igef: the media-type predicate is invariant across every group and MUST stay outside the
    per-directory OR. Repeating its 16 values in each of a default page's 2,000 clauses rendered
    38,000 bind parameters and crossed asyncpg's 32,767-parameter cap. Factoring it out preserves
    the same conjunction while rendering only 16 media-type binds plus 3 binds per group (6,016 at
    the default page size).

    Media rows are re-bucketed by their OWN (agent_id, parent directory), computed the same way
    ``dir_groups`` was, rather than by which OR clause matched them: the LIKE/NOT-LIKE pair only
    matches rows whose parent IS that literal directory, so recomputing the parent from each
    returned row reconstructs the exact same grouping the one-query-per-group form did.
    """
    media_result = await session.execute(select(FileRecord).where(_media_directly_in(dir_groups)))
    return _group_by_directory(media_result.scalars().all())


def _media_directly_in(dir_groups: Collection[tuple[str, str]]) -> ColumnElement[bool]:
    """WHERE clause: a media file sitting directly in one of the ``(agent_id, directory)`` keys.

    The media-type predicate sits OUTSIDE the per-directory OR (phaze-6igef): 16 binds, plus 3 per key.
    """
    directory_conditions = [
        and_(
            FileRecord.agent_id == agent_id,
            # Escape LIKE metacharacters in the directory so '_'/'%'/'\' in a real
            # path (e.g. "Coachella_2024") are matched literally rather than as wildcards.
            FileRecord.original_path.like(f"{_escape_like(directory)}/%", escape=_LIKE_ESCAPE_CHAR),
            ~FileRecord.original_path.like(f"{_escape_like(directory)}/%/%", escape=_LIKE_ESCAPE_CHAR),
        )
        for agent_id, directory in dir_groups
    ]
    return and_(FileRecord.file_type.in_(MEDIA_TYPES), or_(*directory_conditions))


async def _parent_media_by_name(session: AsyncSession, parent_keys: Collection[tuple[str, str]]) -> dict[tuple[str, str], dict[str, list[uuid.UUID]]]:
    """Index the media directly in each parent directory by :func:`companion_match_key` of its stem.

    Reads only ``id``/``agent_id``/``original_path``, never whole rows: a parent may be a flat dump
    folder holding tens of thousands of media files (18,535 in one production parent, measured
    2026-10-06), and only their names and ids are needed. The index is built once per page, so each
    companion's match is a dict lookup, never a scan of the parent. Same WHERE clause, and so the
    same bind budget, as :func:`_media_in_directories`.
    """
    result = await session.execute(select(FileRecord.id, FileRecord.agent_id, FileRecord.original_path).where(_media_directly_in(parent_keys)))
    index: dict[tuple[str, str], dict[str, list[uuid.UUID]]] = {}
    for media_id, agent_id, original_path in result.all():
        path = PurePosixPath(original_path)
        if key := companion_match_key(path.stem):
            index.setdefault((agent_id, str(path.parent)), {}).setdefault(key, []).append(media_id)
    return index


def _parent_key(group_key: tuple[str, str]) -> tuple[str, str] | None:
    """The same agent's PARENT directory of ``group_key``, or None at the filesystem root."""
    agent_id, directory = group_key
    parent = str(PurePosixPath(directory).parent)
    return None if parent == directory else (agent_id, parent)


async def _link_targets(session: AsyncSession, dir_groups: dict[tuple[str, str], list[FileRecord]]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Resolve each companion in this page to the media ids it links to.

    A companion beside media links to every media file in its own directory -- the long-standing
    rule. A companion with NO media in its own directory (phaze-ehryj) links only to media directly
    in its PARENT directory, on the same agent, whose stem matches the companion's sub-folder name
    or the companion's own stem under :func:`phaze.constants.companion_match_key` (operator decision
    2026-10-06, "Match by name"; bead phaze-ehryj), or matches that parent directory's OWN name -- a
    release folder holding its media beside an "info" sub-folder of download notices (operator
    decision 2026-10-06, "Add parent-folder name"; bead phaze-ehryj). An unmatched companion stays
    unlinked even when its parent holds media: a flat dump folder's unrelated sets must never be
    paired by proximity alone.

    Own-directory media WINS -- a group with any is never matched against its parent -- and the
    fallback is exactly one level. ``tasks/scan.py`` admits a companion by the same key, so the scan
    ingests exactly the companions this can link.

    Two reads, never one widened read: the parent read runs only for the groups the first left
    without media, and each stays within :func:`_media_directly_in`'s per-key bind budget, so the
    phaze-6igef bound still holds for a maximally dispersed page.
    """
    own_media = await _media_in_directories(session, dir_groups)
    targets = {
        companion.id: [media.id for media in own_media[group_key]]
        for group_key, companions in dir_groups.items()
        if group_key in own_media
        for companion in companions
    }
    fallback = {group_key: parent_key for group_key in dir_groups if group_key not in own_media and (parent_key := _parent_key(group_key))}
    if not fallback:
        return targets
    parent_index = await _parent_media_by_name(session, set(fallback.values()))
    for group_key, parent_key in fallback.items():
        by_name = parent_index.get(parent_key, {})
        folder_matches = [
            *by_name.get(companion_match_key(PurePosixPath(group_key[1]).name), []),
            *by_name.get(companion_match_key(PurePosixPath(parent_key[1]).name), []),
        ]
        for companion in dir_groups[group_key]:
            stem_matches = by_name.get(companion_match_key(PurePosixPath(companion.original_path).stem), [])
            if matched := list(dict.fromkeys([*folder_matches, *stem_matches])):
                targets[companion.id] = matched
    return targets


def _link_rows(targets: dict[uuid.UUID, list[uuid.UUID]]) -> list[dict[str, uuid.UUID]]:
    """Build the companion x media cross-product rows for one page. PURE -- no IO.

    This is the O(companions * media) nest, and it is deliberately a pure function so the nesting
    carries no database round trips: the reads that feed it already ran (:func:`_link_targets`), and
    the write it feeds runs once per bind-parameter chunk. A static "nested loop with IO" reading of
    :func:`associate_companions` is measuring the shape of the OLD one-query-per-directory form; the
    only thing nested here is dict/list traversal.

    Explicit id: pg_insert bypasses ``FileCompanion.id``'s Python-side ``default=uuid.uuid4``
    (dedup.resolve_group precedent).
    """
    return [
        {"id": uuid.uuid4(), "companion_id": companion_id, "media_id": media_id}
        for companion_id, media_ids in targets.items()
        for media_id in media_ids
    ]


async def _insert_links(session: AsyncSession, rows: list[dict[str, uuid.UUID]]) -> int:
    """Insert one page's links, chunked to the bind-parameter cap. Returns links actually created.

    phaze-p3qr: CHUNKED, because an explicit multi-row VALUES binds ``len(rows) * params_per_row``
    parameters in ONE statement and PostgreSQL's Bind message caps that at int16 (32767) -- 10,922
    rows at this model's 3 parameters (id/companion_id/media_id), a threshold a conventional album
    directory (folder art + a couple of scene-release sidecars against a dozen tracks) clears in
    roughly 230 directories on the large personal archive this project targets. ``chunk_rows``
    derives the split from the rows' actual parameter count (services/bulk_insert.py), so adding a
    column to FileCompanion cannot silently reintroduce the break. A single PAGE's cross product can
    still cross this bound even with batch_size bounding the companion count, so chunking stays
    regardless of the outer paging.

    The chunks MUST run serially, and gathering them would buy nothing: they all execute on ONE
    AsyncSession, whose concurrent use upstream does not support and which -- measured on the
    pinned 2.0.52 -- does not overlap statements anyway. The RULING block below, immediately before
    :func:`associate_companions`, carries the measurement and is explicit that gather does NOT
    raise here. Serial execution is also what keeps every chunk inside one transaction, which is
    the atomicity guarantee below.

    The unlinked read in the caller is a snapshot: a concurrent run computes the same pairs, and
    whichever commits second would violate uq_file_companions_pair. ON CONFLICT DO NOTHING makes
    that first-writer-wins; summing each chunk's rowcount keeps the return value honest under races.
    An INSERT returns a CursorResult at runtime (exposing rowcount); the async stubs type it as the
    base Result, so cast (agent_push.py precedent).

    ATOMICITY: every chunk executes on THIS session inside the SAME transaction (bulk_insert.py's
    "atomicity is the caller's job" rule) -- the caller's ``session.commit()`` is the PAGE's commit
    boundary, so a mid-page failure rolls back every chunk in THIS page (never a partial link set for
    one page), while earlier pages already committed stay committed.
    """
    created = 0
    for chunk in chunk_rows(rows):
        insert_stmt = pg_insert(FileCompanion).values(chunk).on_conflict_do_nothing(constraint="uq_file_companions_pair")
        insert_result = cast("CursorResult[Any]", await session.execute(insert_stmt))
        created += insert_result.rowcount
    return created


# phaze-bk9el.25 -- RULING on this module's io_in_loop / serial_await_in_loop / nested_loop_with_io
# findings, recorded per site so a later pass does not "fix" one of them into a defect. Every await
# below is inside a loop by construction; the question a static finding cannot answer is whether the
# loop is the unit of work, and here it always is.
#
# The mechanical bar first, because it disposes of the whole class: all four awaits run on ONE
# AsyncSession, and no await here is convertible to a concurrent one without giving each branch its
# own session -- which would break the transaction the page's atomicity depends on.
#
# BE PRECISE ABOUT WHY, because the obvious phrasing is FALSE and stood in this very comment until
# phaze-bk9el.25's review caught it. It is tempting to write that ``asyncio.gather`` over these
# raises "another operation is in progress". IT DOES NOT. Measured 2026-08-22 against this repo's
# harness on the pinned SQLAlchemy 2.0.52: six ``select pg_sleep(0.1)`` on one AsyncSession took
# 0.636 s serially and 0.628 s under ``asyncio.gather`` -- 1.01x, where genuine overlap would be
# ~6x -- and NO exception was raised. gather SILENTLY SERIALIZES. Reproduced independently twice
# more the same day: dev/w3-26 on phaze-bk9el.26 (0.745 s serial / 0.659 s gathered, plus eight
# concurrent ORM ``session.execute`` calls returning eight results and no error) and the dispatcher
# seat on this worktree's own database.
#
# Two things are established, and BOTH are reasons not to gather:
#   * SQLAlchemy DOCUMENTS AsyncSession as unsafe for concurrent use. That is upstream's stated
#     contract, it has not changed, and it is sufficient on its own.
#   * On 2.0.52 the observed behaviour is silent serialization, so a gather is not even a failed
#     optimisation -- it is a no-op that LOOKS like one. That is worse than an exception, because
#     the "fix" appears to have worked while buying nothing.
# The second must NOT be leant on: silent serialization is undocumented behaviour upstream is free
# to change, and a release that started raising would turn any code depending on it into a live
# defect. "Unsupported" and "does not raise today" are both true; neither licenses a gather.
#
# Related trap, since it is what routes beads to this module in the first place: a static
# serial_await_in_loop finding carrying ``dataflow_verified: true`` is NOT a green light. Absence of
# a data dependence is NECESSARY AND NEVER SUFFICIENT for a gather -- the flag says nothing about
# session sharing, which is the binding constraint here. phaze-4tch9 discharged that: the general
# form, the four-seat measurement table and the tree-wide sweep now live in
# docs/design/0015-shared-session-gather.md, and the same false premise in proposal.py's ``store_proposals`` has
# been amended to agree with this block.
#
#   unlinked-companion page read   The keyset walk's cursor. Page N+1's ``id > after`` bound is not
#     (in associate_companions)    known until page N has been read. Sequencing IS the algorithm;
#                                  the whole point of phaze-yiwq5 was to STOP reading this in one
#                                  unbounded shot.
#   media read                     Already ONE query per page for every (agent, directory) group in
#     (_media_in_directories)      it -- phaze-vu88k.3 collapsed the one-query-per-group form. Once
#                                  per page is the floor; it cannot be hoisted out of the paging
#                                  loop because it is scoped to that page's directories.
#   parent media read              phaze-ehryj: at most one more query per page, only for the groups
#     (_parent_media_by_name)      the first read left without media. It depends on that read's
#                                  result, so it is sequential by data dependence, not just session.
#   chunked insert                 One statement per bind-parameter chunk (32767 cap, phaze-p3qr).
#     (_insert_links)              Chunk count is driven by the page's cross-product size, not by
#                                  row count; serial execution is what keeps every chunk in one
#                                  transaction. See _insert_links.
#   per-page commit                The page's commit boundary, and load-bearing for correctness, not
#     (in associate_companions)    just durability: page N's links must be visible to page N+1's
#                                  ``NOT IN`` subquery. Deferring or batching commits would make the
#                                  next page recompute pairs it already inserted.
#
# nested_loop_with_io is the one finding this bead changed rather than merely ruled on. It read the
# chunked insert as a database call inside a nested loop (O(n*m) round trips). The O(companions x
# media) nest is now :func:`_link_rows`, a PURE function containing no IO at all, and the insert
# loop is a single flat level over chunks in :func:`_insert_links`. The shape the finding described
# no longer exists.


async def associate_companions(session: AsyncSession, *, batch_size: int = DEFAULT_ASSOCIATE_BATCH_SIZE) -> int:
    """Link unlinked companion files to media in the same directory, or else name-matched parent media.

    Finds all companion FileRecords not yet present in file_companions,
    groups them by (agent, directory), and creates FileCompanion links to
    every media file in that same directory ON THE SAME AGENT -- or, when that
    directory has none, to the media directly in its parent directory on the same
    agent whose name it matches (phaze-ehryj; see :func:`_link_targets`). Idempotent:
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

    SERIAL AWAITS (phaze-bk9el.25): every ``await`` in the loop below is deliberately serial and
    none may be converted to ``asyncio.gather``. The page read, the media read, the chunked insert
    and the commit all run on ONE ``AsyncSession``, whose concurrent use upstream does not support
    and which, measured, does not overlap statements anyway -- see the RULING block directly above
    for the numbers and for why "it raises" is the wrong reason to give.
    Beyond that, the loop is a keyset walk: page N+1's ``id > after`` bound is not known until page
    N has been read, and page N's ``commit()`` is what makes its links visible to page N+1's
    ``NOT IN`` subquery. The serialization IS the paging.

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

        dir_groups = _group_by_directory(unlinked_companions)
        rows = _link_rows(await _link_targets(session, dir_groups))

        if rows:
            count += await _insert_links(session, rows)

        # This PAGE's commit boundary -- see _insert_links for what it does and does not make atomic,
        # and the docstring's PAGING note for why each page commits before the next is read.
        await session.commit()

        if len(unlinked_companions) < batch_size:
            break

    return count
