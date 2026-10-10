"""Companion association: re-derives every companion's links with the linking chain over stored content features (phaze-rmhfr).

The decisions are ``services/companion_linking.py``'s (pure, cited rule by rule to
``docs/spikes/phaze-9aker-companion-matching-accuracy.md``); this module does the IO around them --
which companions to decide, the agent's media they are decided against, and the writes.

- **Every link is re-derived.** Operator decision 2026-10-07, "Re-derive every link (Recommended)"
  (dispatch session; durable record: bead phaze-rmhfr comment). Each run decides every companion that
  carries CURRENT features, linked or not, and REPLACES its links with exactly what the chain returns:
  links the chain no longer returns -- a junk companion's, the retired parent-folder rule's, an
  over-link to every file of a collection folder -- are deleted, missing ones inserted, the rest kept.
  ``file_companions`` records derivation only when this chain actually runs; historical links remain unknown. This module is its only writer, so no link is
  operator-made and none is exempt.
- **Only companions with CURRENT content features are decided.** A companion with no
  ``companion_content_features`` row, or whose stored fingerprint no longer equals
  ``files.sha256_hash``, keeps whatever links it has, untouched, and is counted as
  ``awaiting_features``: without its features the junk veto and the reference step cannot run, and
  linking by location alone is the rule the spike measured linking 17 of 20 junk files (§4.3). So the
  deploy order is the features backfill (``phaze backfill companion-features``) first, association
  second.
- **Per agent.** ``original_path`` is unique only per agent (uq_files_agent_id_original_path), so
  every read, the media index and every link stay on one agent: two fileserver agents holding the
  identical path never pair files from unrelated recordings (phaze-vpig).
- **Companions beside media first, and duplicates judged on THIS run's links.** The duplicate veto asks
  whether a byte-identical copy is linked. Deciding every companion beside media before any without,
  and answering from the links this run has derived so far (never from links an earlier run left on a
  companion not yet re-derived), makes the outcome a function of the archive alone: a re-run derives
  the same links, whatever the ingest order or the previous state.
- **Stamp verdicts are re-decided first, on the media stored now.** A content stamped while none of
  its folders held media is no stamp once media lands beside it, so each agent's run starts with
  :func:`phaze.services.companion_content.refresh_agent_stamps` (phaze-4x319.5). "Does this folder
  hold media" is one answer everywhere: the media index here and
  :func:`phaze.services.companion_content.media_in_folders` read the same rows with the same folder
  identity.
- **Head-only junk verdicts do not veto.** A per-file class (empty, all-NUL, site ad) the agent reached
  on a TRUNCATED read judged only the first ``MAX_FEATURE_BYTES``; a large file whose head looks like an
  advert may be a real tracklist, so it does not veto a link. A known stamp is a whole-file
  fingerprint verdict and still does (the phaze-bk5jp detector applies the same guard).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
import uuid

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.services.bulk_insert import chunk_rows
from phaze.services.companion_availability import available_companion_clause, count_unavailable_companions
from phaze.services.companion_content import (
    COMPANION_FILE_TYPES,
    MEDIA_FILE_TYPES,
    agent_media_rows,
    effective_junk_class,
    media_folder,
    refresh_agent_stamps,
)
from phaze.services.companion_linking import AgentMediaIndex, LinkInput, LinkStep, link_companion


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


COMPANION_TYPES: frozenset[str] = COMPANION_FILE_TYPES
"""The companion types association decides: the ingestible ones, the only ones that carry content features."""
MEDIA_TYPES: frozenset[str] = MEDIA_FILE_TYPES

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
    links_kept: int = 0
    awaiting_features: int = 0
    unavailable_missing: int = 0
    unavailable_ambiguous: int = 0
    decided: dict[LinkStep, int] = field(default_factory=dict)
    """Companions decided this run, by the chain step that decided them."""


_HEAD_ONLY_JUNK: frozenset[str] = frozenset({"empty", "all_nul", "site_ad"})
"""Per-file junk classes, judged by the agent on the bytes it read: on a truncated read, only the head."""


@dataclass(frozen=True)
class _Companion:
    """One companion with current stored features, as the chain needs it."""

    id: uuid.UUID
    path: str
    fingerprint: str
    junk_class: str | None
    is_tracklist: bool
    references: tuple[tuple[str, str], ...]


def linking_junk_class(junk_class: str | None, *, truncated: bool) -> str | None:
    """The junk class the linking veto honours: a per-file class from a truncated read is not a whole-file verdict."""
    return None if truncated and junk_class in _HEAD_ONLY_JUNK else junk_class


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
        statement = agent_media_rows(agent_id).order_by(FileRecord.original_path).limit(page_size)
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


async def _companion_page(
    session: AsyncSession, agent_id: str, *, after: uuid.UUID | None, limit: int, stamps: tuple[set[str], set[str]] | None = None
) -> list[_Companion]:
    """One keyset page of ``agent_id``'s companions that carry current features, linked or not, by ``FileRecord.id``.

    ``stamps`` is :func:`refresh_agent_stamps`' ``(re-decided, stamps)``: a re-decided content takes
    the verdict decided this run, which a dry run has not written (an applied run has, so the two
    agree there).
    """
    rechecked, stamped = stamps or (set(), set())
    statement = (
        select(
            FileRecord.id,
            FileRecord.original_path,
            CompanionContentFeatures.fingerprint,
            CompanionContentFeatures.junk_class,
            CompanionContentFeatures.content_junk_class,
            CompanionContentFeatures.truncated,
            CompanionContentFeatures.is_tracklist,
            CompanionContentFeatures.media_references,
        )
        .join(CompanionContentFeatures, _current_features())
        .where(FileRecord.agent_id == agent_id, FileRecord.file_type.in_(COMPANION_TYPES), available_companion_clause())
        .order_by(FileRecord.id)
        .limit(limit)
    )
    if after is not None:
        statement = statement.where(FileRecord.id > after)
    return [
        _Companion(
            id=row.id,
            path=row.original_path,
            fingerprint=row.fingerprint,
            junk_class=linking_junk_class(
                effective_junk_class(row.content_junk_class, stamp=row.fingerprint in stamped) if row.fingerprint in rechecked else row.junk_class,
                truncated=row.truncated,
            ),
            is_tracklist=row.is_tracklist,
            references=tuple((str(reference["name"]), str(reference["source"])) for reference in row.media_references),
        )
        for row in (await session.execute(statement)).all()
    ]


async def _agents_with_features(session: AsyncSession) -> list[str]:
    """Every agent holding stored companion features: the only agents association has anything to decide on."""
    result = await session.execute(select(CompanionContentFeatures.agent_id).distinct().order_by(CompanionContentFeatures.agent_id))
    return list(result.scalars())


async def _count_awaiting_features(session: AsyncSession, agent_id: str | None) -> int:
    """Companions association cannot decide yet (no stored features, or features of older bytes); their links stay as they are."""
    statement = (
        select(func.count())
        .select_from(FileRecord)
        .outerjoin(CompanionContentFeatures, _current_features())
        .where(FileRecord.file_type.in_(COMPANION_TYPES), CompanionContentFeatures.file_id.is_(None), available_companion_clause())
    )
    if agent_id is not None:
        statement = statement.where(FileRecord.agent_id == agent_id)
    return int((await session.execute(statement)).scalar_one())


def _decide_page(
    index: AgentMediaIndex,
    page: Sequence[_Companion],
    linked: set[str],
    outcome: AssociationOutcome,
    derivations: dict[uuid.UUID, tuple[str, str, list[str]]] | None = None,
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Run the chain over one page; returns ``companion id -> media ids`` for EVERY companion of the page (empty: no links).

    ``linked`` holds the fingerprints this run has linked so far on this agent; it is the duplicate
    veto's answer and is updated as companions link, so two copies on one page cannot both slip past
    it. PURE: no IO.
    """
    targets: dict[uuid.UUID, list[uuid.UUID]] = {}
    for companion in page:
        decision = link_companion(
            LinkInput(
                path=companion.path,
                references=companion.references,
                junk_class=companion.junk_class,
                is_tracklist=companion.is_tracklist,
                identical_copy_linked=companion.fingerprint in linked,
            ),
            index,
        )
        outcome.decided[decision.step] = outcome.decided.get(decision.step, 0) + 1
        targets[companion.id] = list(decision.media_ids)
        if derivations is not None:
            derivations[companion.id] = (
                decision.step,
                companion.fingerprint,
                [f"linking-chain:{decision.step}", f"features:sha256:{companion.fingerprint}"],
            )
        if decision.media_ids:
            linked.add(companion.fingerprint)
    return targets


async def _existing_links(session: AsyncSession, companion_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, dict[uuid.UUID, uuid.UUID]]:
    """``companion id -> {media id: link id}`` for the page's companions: one read, one bind per companion (a page is <= batch_size)."""
    result = await session.execute(
        select(FileCompanion.id, FileCompanion.companion_id, FileCompanion.media_id).where(FileCompanion.companion_id.in_(companion_ids))
    )
    existing: dict[uuid.UUID, dict[uuid.UUID, uuid.UUID]] = {}
    for link_id, companion_id, media_id in result.all():
        existing.setdefault(companion_id, {})[media_id] = link_id
    return existing


_DELETE_CHUNK = 10_000
"""Link ids per DELETE statement: one bind each, well under the 32,767 cap."""


async def _replace_links(
    session: AsyncSession,
    targets: dict[uuid.UUID, list[uuid.UUID]],
    outcome: AssociationOutcome,
    *,
    apply: bool,
    derivations: dict[uuid.UUID, tuple[str, str, list[str]]] | None = None,
) -> None:
    """Make each companion's links exactly its target set: delete the rest, insert the missing. Does NOT commit.

    Runs inside the page's transaction, so every companion's replacement lands whole or not at all.
    Without ``apply`` nothing is written: the links that would be removed and created are counted
    from the same read, so a dry run reports exactly what a run would do (phaze-spd83).
    """
    if not targets:
        return
    existing = await _existing_links(session, list(targets))
    if apply and derivations is not None:
        # Selection/import/delete all take inventory before companion pairs. Include old targets
        # before globally sorting; changed links outside this set require a fresh derivation.
        file_ids = set(targets) | {media for ids in targets.values() for media in ids} | {media for held in existing.values() for media in held}
        locked = (
            await session.scalars(
                select(FileRecord)
                .where(FileRecord.id.in_(file_ids))
                .order_by(FileRecord.original_path, FileRecord.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        fresh_existing = await _existing_links(session, list(targets))
        if any(media not in file_ids for held in fresh_existing.values() for media in held):
            raise ValueError("Companion links changed during derivation; retry association")
        existing = fresh_existing
        current = {file.id: file for file in locked}
        targets = {
            companion: media_ids
            for companion, media_ids in targets.items()
            if companion in current
            and current[companion].sha256_hash == derivations[companion][1]
            and current[companion].missing_at is None
            and current[companion].companion_ambiguous_at is None
            and all(media in current and current[media].agent_id == current[companion].agent_id for media in media_ids)
        }
    stale: list[uuid.UUID] = []
    missing: dict[uuid.UUID, list[uuid.UUID]] = {}
    for companion_id, media_ids in targets.items():
        held = existing.get(companion_id, {})
        wanted = set(media_ids)
        stale.extend(link_id for media_id, link_id in held.items() if media_id not in wanted)
        outcome.links_kept += len(wanted & held.keys())
        if absent := [media_id for media_id in media_ids if media_id not in held]:
            missing[companion_id] = absent
    if not apply:
        outcome.links_removed += len(stale)
        outcome.links_created += sum(len(media_ids) for media_ids in missing.values())
        return
    for start in range(0, len(stale), _DELETE_CHUNK):
        deleted = cast(
            "CursorResult[Any]", await session.execute(delete(FileCompanion).where(FileCompanion.id.in_(stale[start : start + _DELETE_CHUNK])))
        )
        outcome.links_removed += deleted.rowcount
    if rows := _link_rows(missing):
        outcome.links_created += await _insert_links(session, rows)
    if derivations is not None:
        for companion_id in targets:
            method, revision, evidence = derivations[companion_id]
            await session.execute(
                update(FileCompanion)
                .where(FileCompanion.companion_id == companion_id)
                .values(
                    derivation_method=method,
                    derivation_revision=revision,
                    derivation_evidence=evidence,
                    derived_at=datetime.now(UTC),
                )
            )


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

    The existing-link read in the caller is a snapshot: a concurrent run computes the same pairs, and
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
#   companion page read            The keyset walk's cursor. Page N+1's ``id > after`` bound is not
#     (_companion_page)            known until page N has been read. Sequencing IS the algorithm;
#                                  the whole point of phaze-yiwq5 was to STOP reading this in one
#                                  unbounded shot.
#   media index read               phaze-rmhfr: the agent's media, read ONCE per agent in keyset
#     (_media_index)               pages, before the first page is decided. Each page's cursor is the
#                                  previous page's last path, so it is sequential by data dependence.
#                                  It replaced the per-page own-folder read (phaze-vu88k.3) and the
#                                  phaze-ehryj parent read: the chain resolves references and names
#                                  across the whole agent, which no per-folder read can answer.
#   existing-link read + delete    phaze-rmhfr: one read of the page's current links, then the stale
#     (_replace_links)             ones deleted in id chunks. The delete needs the read's result, and
#                                  both must share the page's transaction (atomic replacement).
#   chunked insert                 One statement per bind-parameter chunk (32767 cap, phaze-p3qr).
#     (_insert_links)              Chunk count is driven by the page's cross-product size, not by
#                                  row count; serial execution is what keeps every chunk in one
#                                  transaction. See _insert_links.
#   per-page commit                The page's commit boundary, and load-bearing for correctness, not
#     (in _associate_agent)        just durability: it is the atomicity boundary of the page's link
#                                  replacements, so a failure never leaves half a companion's link set.
#
# nested_loop_with_io is the one finding this bead changed rather than merely ruled on. It read the
# chunked insert as a database call inside a nested loop (O(n*m) round trips). The O(companions x
# media) nest is now :func:`_link_rows`, a PURE function containing no IO at all, and the insert
# loop is a single flat level over chunks in :func:`_insert_links`. The shape the finding described
# no longer exists.


async def _associate_agent(session: AsyncSession, agent_id: str, *, batch_size: int, outcome: AssociationOutcome, apply: bool) -> None:
    """Re-derive every current-features companion of one agent: those beside media in a first keyset walk, the rest in a second."""
    # Stamp verdicts first, on the media stored NOW: media that arrived since a group was decided can
    # un-stamp it, and the veto below reads the verdict (phaze-4x319.5).
    stamps = await refresh_agent_stamps(session, agent_id, apply=apply)
    if apply:
        await session.commit()
    index: AgentMediaIndex | None = None
    linked: set[str] = set()
    for beside_media in (True, False):
        after: uuid.UUID | None = None
        while True:
            page = await _companion_page(session, agent_id, after=after, limit=batch_size, stamps=stamps)
            if not page:
                break
            after = page[-1].id
            if index is None:
                index = await _media_index(session, agent_id)
            this_pass = [companion for companion in page if bool(index.in_folder(media_folder(companion.path))) is beside_media]
            derivations: dict[uuid.UUID, tuple[str, str, list[str]]] = {}
            targets = _decide_page(index, this_pass, linked, outcome, derivations)
            await _replace_links(session, targets, outcome, apply=apply, derivations=derivations)
            if apply:
                # This PAGE's commit boundary: every replacement on the page lands whole (_replace_links).
                await session.commit()
            if len(page) < batch_size:
                break
        if index is None:
            return


async def associate_companions(
    session: AsyncSession, *, agent_id: str | None = None, batch_size: int = DEFAULT_ASSOCIATE_BATCH_SIZE, apply: bool = True
) -> AssociationOutcome:
    """Re-derive the links of every companion with current content features (module docstring).

    ``agent_id`` limits the run, and its ``awaiting_features`` count, to that one agent: the unit the
    automatic run (``tasks/companion_association.py``) and ``phaze backfill companion-links`` work in
    (phaze-spd83). A run over one agent decides exactly what a run over all agents decides for it,
    because nothing here crosses agents. ``apply=False`` writes and commits nothing and reports the
    links a run would remove, create and keep.

    Per agent holding stored features, every such companion is decided by
    :func:`phaze.services.companion_linking.link_companion` against that agent's media -- companions
    beside media first, then the rest -- and its links are replaced by exactly the chain's answer.
    Companions without current features are counted, never decided, and keep their links.

    Idempotent: a second run derives the same links, so it deletes and inserts nothing (pinned by the
    tests). Under CONCURRENT runs (e.g. an HTMX double-submit of POST /associate) the insert is ON
    CONFLICT DO NOTHING against uq_file_companions_pair, so a pair the other run committed first is
    skipped instead of raising IntegrityError, and deleting a link the other run already deleted is a
    no-op.

    PAGING (phaze-yiwq5): companions are read in keyset pages of *batch_size*, ordered by
    ``FileRecord.id`` -- never materialized in one unbounded ``.scalars().all()``. Each page commits
    its replacements before the next page is read. A failure mid-sweep leaves earlier pages committed,
    which is safe because the function is idempotent. Peak memory is one page's companions plus the
    agent's :class:`AgentMediaIndex` (:func:`_media_index` gives the measured size) and the set of
    fingerprints linked this run, all linear in that agent's files and independent of the backlog.

    SERIAL AWAITS (phaze-bk9el.25): every ``await`` below is deliberately serial and none may be
    converted to ``asyncio.gather``. Every read, write and commit runs on ONE ``AsyncSession``, whose
    concurrent use upstream does not support and which, measured, does not overlap statements anyway
    -- see the RULING block directly above for the numbers and for why "it raises" is the wrong reason
    to give. Beyond that, the loop is a keyset walk: page N+1's ``id > after`` bound is not known until
    page N has been read. The serialization IS the paging.
    """
    outcome = AssociationOutcome()
    agents = await _agents_with_features(session)
    if agent_id is not None:
        agents = [agent for agent in agents if agent == agent_id]
    for agent in agents:
        await _associate_agent(session, agent, batch_size=batch_size, outcome=outcome, apply=apply)
    outcome.awaiting_features = await _count_awaiting_features(session, agent_id)
    unavailable = await count_unavailable_companions(session, agent_id)
    outcome.unavailable_missing = unavailable.missing
    outcome.unavailable_ambiguous = unavailable.ambiguous
    return outcome
