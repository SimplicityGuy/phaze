"""Companion association: runs the linking chain over stored content features and writes the links (phaze-rmhfr).

The decisions are ``services/companion_linking.py``'s (pure, cited rule by rule to
``docs/spikes/phaze-9aker-companion-matching-accuracy.md``); this module does the IO around them --
which companions to decide, the agent's media they are decided against, and the writes.

- **Only companions with CURRENT content features are decided.** A companion with no
  ``companion_content_features`` row, or whose stored fingerprint no longer equals
  ``files.sha256_hash``, is left unlinked and counted as ``awaiting_features``: without its features
  the junk veto and the reference step cannot run, and linking it by location alone is exactly the
  rule the spike measured linking 17 of 20 junk files (§4.3). It is decided on the first run after
  its features land (``phaze backfill companion-features`` reads the backlog).
- **Per agent.** ``original_path`` is unique only per agent (uq_files_agent_id_original_path), so
  every read, the media index and every link stay on one agent: two fileserver agents holding the
  identical path never pair files from unrelated recordings (phaze-vpig).
- **Companions beside media first.** The duplicate veto asks whether a byte-identical copy is linked;
  deciding every companion that has media of its own before any that has none makes that answer the
  same whatever order the rows were ingested in.
- **Junk links are removed.** A companion whose current features say junk keeps no link, including
  one an earlier rule wrote (the shipped own-folder rule linked 17 of 20 junk files, spike §4.3).
  Nothing else already linked is re-decided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, cast
import uuid

from sqlalchemy import CursorResult, delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.services.bulk_insert import chunk_rows
from phaze.services.companion_content import COMPANION_FILE_TYPES, linked_copy_fingerprints
from phaze.services.companion_linking import AgentMediaIndex, LinkInput, LinkStep, link_companion


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


MEDIA_CATEGORIES: set[FileCategory] = {FileCategory.MUSIC, FileCategory.VIDEO}
COMPANION_TYPES: frozenset[str] = COMPANION_FILE_TYPES
"""The companion types association decides: the ingestible ones, the only ones that carry content features."""
MEDIA_TYPES: set[str] = {ext.lstrip(".") for ext, cat in EXTENSION_MAP.items() if cat in MEDIA_CATEGORIES}

DEFAULT_ASSOCIATE_BATCH_SIZE = 2_000
"""Pending companions read per keyset page in :func:`associate_companions` (phaze-yiwq5). The pre-fix
query materialized the WHOLE unlinked-companion corpus with a single unbounded ``.scalars().all()``
-- on an archive with a large never-associated backlog that is an unbounded allocation in the api
process, reachable from an operator-triggered ``POST /associate``. Paging by ``FileRecord.id``
bounds each page's companions regardless of how large the total backlog is."""

MEDIA_INDEX_PAGE_SIZE = 10_000
"""Media rows read per keyset page while building one agent's :class:`AgentMediaIndex`."""


@dataclass
class AssociationOutcome:
    """What one :func:`associate_companions` run did."""

    links_created: int = 0
    links_removed: int = 0
    awaiting_features: int = 0
    decided: dict[LinkStep, int] = field(default_factory=dict)
    """Companions decided this run, by the chain step that decided them."""


@dataclass(frozen=True)
class _Pending:
    """One undecided companion and its current stored features."""

    id: uuid.UUID
    path: str
    fingerprint: str
    junk_class: str | None
    is_tracklist: bool
    references: tuple[tuple[str, str], ...]


async def _media_index(session: AsyncSession, agent_id: str, *, page_size: int = MEDIA_INDEX_PAGE_SIZE) -> AgentMediaIndex:
    """Every media row of ``agent_id``, read in keyset pages on ``(agent_id, original_path)``.

    Reads only ``id`` and ``original_path``, never whole rows. Each page is ``page_size`` rows and
    four binds plus the media types, so no read grows with the archive; the index itself is linear in
    the agent's media count (measured on the archive's 104,247 media rows on one agent: 47 MiB, and
    104 MiB once step 6's close-name index is built) and is dropped before the next agent.
    """
    index = AgentMediaIndex()
    after: str | None = None
    while True:
        statement = (
            select(FileRecord.id, FileRecord.original_path)
            .where(FileRecord.agent_id == agent_id, FileRecord.file_type.in_(MEDIA_TYPES))
            .order_by(FileRecord.original_path)
            .limit(page_size)
        )
        if after is not None:
            statement = statement.where(FileRecord.original_path > after)
        rows = (await session.execute(statement)).all()
        for media_id, path in rows:
            index.add(media_id, path)
        if len(rows) < page_size:
            return index
        after = rows[-1].original_path


def _current_features() -> Any:
    """Join condition: the companion's stored features, only while they describe its current bytes."""
    return (CompanionContentFeatures.file_id == FileRecord.id) & (CompanionContentFeatures.fingerprint == FileRecord.sha256_hash)


def _unlinked() -> Any:
    return ~exists().where(FileCompanion.companion_id == FileRecord.id)


async def _pending_page(session: AsyncSession, agent_id: str, *, after: uuid.UUID | None, limit: int) -> list[_Pending]:
    """One keyset page of ``agent_id``'s unlinked companions that carry current features, by ``FileRecord.id``."""
    statement = (
        select(
            FileRecord.id,
            FileRecord.original_path,
            CompanionContentFeatures.fingerprint,
            CompanionContentFeatures.junk_class,
            CompanionContentFeatures.is_tracklist,
            CompanionContentFeatures.media_references,
        )
        .join(CompanionContentFeatures, _current_features())
        .where(FileRecord.agent_id == agent_id, FileRecord.file_type.in_(COMPANION_TYPES), _unlinked())
        .order_by(FileRecord.id)
        .limit(limit)
    )
    if after is not None:
        statement = statement.where(FileRecord.id > after)
    return [
        _Pending(
            id=row.id,
            path=row.original_path,
            fingerprint=row.fingerprint,
            junk_class=row.junk_class,
            is_tracklist=row.is_tracklist,
            references=tuple((str(reference["name"]), str(reference["source"])) for reference in row.media_references),
        )
        for row in (await session.execute(statement)).all()
    ]


async def _agents_with_features(session: AsyncSession) -> list[str]:
    """Every agent holding stored companion features: the only agents association has anything to decide on."""
    result = await session.execute(select(CompanionContentFeatures.agent_id).distinct().order_by(CompanionContentFeatures.agent_id))
    return list(result.scalars())


async def _count_awaiting_features(session: AsyncSession) -> int:
    """Unlinked companions association cannot decide yet: no stored features, or features of older bytes."""
    result = await session.execute(
        select(func.count())
        .select_from(FileRecord)
        .outerjoin(CompanionContentFeatures, _current_features())
        .where(FileRecord.file_type.in_(COMPANION_TYPES), CompanionContentFeatures.file_id.is_(None), _unlinked())
    )
    return int(result.scalar_one())


async def _remove_junk_links(session: AsyncSession, agent_id: str) -> int:
    """Delete every link held by a companion of ``agent_id`` whose current features say junk. Does NOT commit.

    One statement and one bind: the junk companions are selected server-side, so the delete never
    carries their ids. Bounded by the junk links that exist, which only an older rule could write.
    """
    junk = (
        select(FileRecord.id)
        .join(CompanionContentFeatures, _current_features())
        .where(CompanionContentFeatures.agent_id == agent_id, CompanionContentFeatures.junk_class.is_not(None))
    )
    result = cast("CursorResult[Any]", await session.execute(delete(FileCompanion).where(FileCompanion.companion_id.in_(junk))))
    return result.rowcount


async def _decide_page(
    session: AsyncSession, agent_id: str, index: AgentMediaIndex, page: Sequence[_Pending], outcome: AssociationOutcome
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Run the chain over one page; returns ``companion id -> media ids`` for the companions that link.

    The duplicate veto reads which fingerprints already have a linked copy -- committed by an earlier
    page or run (one bounded read) or linked earlier in THIS page (tracked here), so two copies on one
    page cannot both slip past it.
    """
    orphans = [pending for pending in page if pending.junk_class is None and not index.in_folder(str(PurePosixPath(pending.path).parent))]
    linked = await linked_copy_fingerprints(session, agent_id, {pending.fingerprint for pending in orphans}) if orphans else set()
    targets: dict[uuid.UUID, list[uuid.UUID]] = {}
    for pending in page:
        decision = link_companion(
            LinkInput(
                path=pending.path,
                references=pending.references,
                junk_class=pending.junk_class,
                is_tracklist=pending.is_tracklist,
                identical_copy_linked=pending.fingerprint in linked,
            ),
            index,
        )
        outcome.decided[decision.step] = outcome.decided.get(decision.step, 0) + 1
        if decision.media_ids:
            targets[pending.id] = list(decision.media_ids)
            linked.add(pending.fingerprint)
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
# The mechanical bar first, because it disposes of the whole class: every await runs on ONE
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
#   pending-companion page read    The keyset walk's cursor. Page N+1's ``id > after`` bound is not
#     (_pending_page)              known until page N has been read. Sequencing IS the algorithm;
#                                  the whole point of phaze-yiwq5 was to STOP reading this in one
#                                  unbounded shot.
#   media index read               phaze-rmhfr: the agent's media, read ONCE per agent in keyset
#     (_media_index)               pages, before the first page is decided. Each page's cursor is the
#                                  previous page's last path, so it is sequential by data dependence.
#                                  It replaced the per-page own-folder read (phaze-vu88k.3) and the
#                                  phaze-ehryj parent read: the chain resolves references and names
#                                  across the whole agent, which no per-folder read can answer.
#   linked-copy read               phaze-rmhfr: at most one read per page of fingerprints, only for
#     (linked_copy_fingerprints)   that page's media-less companions. It must see the previous page's
#                                  committed links, so it is sequential by data dependence.
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


async def _associate_agent(session: AsyncSession, agent_id: str, *, batch_size: int, outcome: AssociationOutcome) -> None:
    """Decide every pending companion of one agent: those beside media in a first keyset walk, the rest in a second."""
    index: AgentMediaIndex | None = None
    for beside_media in (True, False):
        after: uuid.UUID | None = None
        while True:
            page = await _pending_page(session, agent_id, after=after, limit=batch_size)
            if not page:
                break
            after = page[-1].id
            if index is None:
                index = await _media_index(session, agent_id)
            this_pass = [pending for pending in page if bool(index.in_folder(str(PurePosixPath(pending.path).parent))) is beside_media]
            if this_pass and (rows := _link_rows(await _decide_page(session, agent_id, index, this_pass, outcome))):
                outcome.links_created += await _insert_links(session, rows)
            # This PAGE's commit boundary -- see _insert_links for what it does and does not make
            # atomic, and associate_companions' PAGING note for why each page commits before the next.
            await session.commit()
            if len(page) < batch_size:
                break
        if index is None:
            return


async def associate_companions(session: AsyncSession, *, batch_size: int = DEFAULT_ASSOCIATE_BATCH_SIZE) -> AssociationOutcome:
    """Link every unlinked companion the linking chain resolves, and remove the links of junk companions.

    Per agent holding stored features: junk links are removed (one statement, committed), then every
    unlinked companion with current features is decided by
    :func:`phaze.services.companion_linking.link_companion` against that agent's media -- companions
    beside media first, then the rest (module docstring) -- and the links it returns are inserted.
    Companions without current features are counted, never decided.

    Idempotent: running twice produces no duplicate links, including under CONCURRENT invocations (e.g.
    an HTMX double-submit of POST /associate) -- the insert is ON CONFLICT DO NOTHING against
    uq_file_companions_pair, so a pair the other request already committed is silently skipped instead
    of raising IntegrityError and rolling back the whole page. A companion the chain leaves unlinked is
    re-decided on the next run, which is what lets media that arrives later pick it up.

    PAGING (phaze-yiwq5): pending companions are read in keyset pages of *batch_size*, ordered by
    ``FileRecord.id`` -- never materialized in one unbounded ``.scalars().all()``. Each page commits
    its own links before the next page is read, so a companion an earlier page linked is excluded from
    later pages by both the ``NOT EXISTS`` link test and the ``id > cursor`` bound. A failure mid-sweep
    leaves whatever earlier pages committed in place, which is safe because the function is idempotent.
    Peak memory is one page's companions plus the agent's :class:`AgentMediaIndex`
    (:func:`_media_index` gives the measured size), which is linear in that agent's media and
    independent of the backlog.

    SERIAL AWAITS (phaze-bk9el.25): every ``await`` below is deliberately serial and none may be
    converted to ``asyncio.gather``. Every read, insert and commit runs on ONE ``AsyncSession``, whose
    concurrent use upstream does not support and which, measured, does not overlap statements anyway
    -- see the RULING block directly above for the numbers and for why "it raises" is the wrong reason
    to give. Beyond that, the loop is a keyset walk: page N+1's ``id > after`` bound is not known until
    page N has been read, and page N's ``commit()`` is what makes its links visible to page N+1's link
    test and to the duplicate veto's read. The serialization IS the paging.
    """
    outcome = AssociationOutcome()
    for agent_id in await _agents_with_features(session):
        outcome.links_removed += await _remove_junk_links(session, agent_id)
        await session.commit()
        await _associate_agent(session, agent_id, batch_size=batch_size, outcome=outcome)
    outcome.awaiting_features = await _count_awaiting_features(session)
    return outcome
