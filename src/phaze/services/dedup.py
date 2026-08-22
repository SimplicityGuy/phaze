"""Duplicate detection service: finds files sharing the same SHA256 hash."""

from collections.abc import Iterable, Sequence
from typing import Any
import uuid as uuid_mod

from sqlalchemy import ColumnElement, Subquery, delete, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.bulk_insert import chunk_rows
from phaze.services.stage_status import dedup_resolved_clause


TAG_FIELDS = ["artist", "title", "album", "year", "genre", "track_number"]

# phaze-z4p5q: display cap on how many MEMBERS of a single duplicate group get materialized by the
# three per-group readers below (find_duplicate_group_by_hash, find_duplicate_groups_with_metadata,
# find_duplicate_groups_by_hashes). None of them previously had a per-member LIMIT -- only
# find_duplicate_groups_with_metadata capped the number of GROUPS (via _dup_hash_subquery's
# limit/offset) -- so a pathological group (e.g. a directory of byte-identical placeholder media
# sharing one SHA256) materialized every member, and every member's FileMetadata dict, into the API
# process. 500 is generous for the real single-user-archive use case (typical duplicate groups are a
# handful of copies of the same track/set) while bounding the worst case to a fixed, small multiple
# of one card's normal size.
_MAX_GROUP_MEMBERS = 500

# phaze-4iq5t: the page size for the OTHER cap this module owns -- the number of GROUPS
# find_duplicate_groups / find_duplicate_groups_with_metadata return per call (via
# _dup_hash_subquery's limit/offset), distinct from _MAX_GROUP_MEMBERS above (members WITHIN one
# group). This used to be an unlabeled bare ``100`` default with a live caller
# (services/review.get_dedupe_groups) that never passed an offset override, so a duplicate-hash
# group past the hundredth in sort order never rendered on the Dedupe workspace -- permanently and
# silently, with no "showing N of M" signal anywhere (measured on a synthetic corpus at the
# archive's documented current scale, docs/spikes/phaze-ytgo.7-verdict.md's 11,428 files: even a
# conservative 5% duplicate-file rate produced 220 real groups against this 100-group cap, i.e. 120
# groups -- 55% -- silently dropped). Named here so services/review.get_dedupe_groups, the
# /duplicates/groups "Load more" endpoint, and the Dedupe workspace template's button label all
# derive the SAME number from ONE place rather than three independently-drifting literals.
#
# Pagination over this cap was deleted once already (phaze-y4s6, the Phase-62 /duplicates/ cutover
# documented in routers/duplicates.py's list_duplicates()) on the reasoning that there was no live
# caller left for it -- correct at the time. GET /duplicates/groups (routers/duplicates.py) is that
# caller now; do not delete this again on the same reasoning without checking for one.
GROUP_PAGE_SIZE = 100


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


def _canonical_rationale(winner: dict[str, Any], runner_up: dict[str, Any] | None) -> str:
    """Name the criterion on which ``winner`` beat the next-best file in its duplicate group.

    Mirrors :func:`score_group`'s ranking order (highest bitrate -> most complete tags -> shortest
    path) and reports the FIRST criterion on which the winner strictly beat the runner-up. When it
    beat the runner-up on neither, the sort was decided by the remaining key, so "shortest path" is
    the honest label -- including for a single-file group, where ``runner_up`` is ``None`` and both
    runner-up values read as 0. ``bitrate`` is stored in BITS per second (phaze-iw2k); the division
    by 1000 here is display-only and does not affect the ranking, which is unit-independent because
    every stored value shares the same unit.
    """
    winner_bitrate = winner.get("bitrate") or 0
    winner_tags = winner.get("tag_filled", 0)
    winner_tag_total = winner.get("tag_total", len(TAG_FIELDS))

    runner_bitrate = (runner_up.get("bitrate") or 0) if runner_up else 0
    runner_tags = (runner_up.get("tag_filled", 0)) if runner_up else 0

    if winner_bitrate > 0 and winner_bitrate > runner_bitrate:
        return f"highest bitrate ({winner_bitrate // 1000}kbps)"
    if winner_tags > 0 and winner_tags > runner_tags:
        return f"most complete tags ({winner_tags}/{winner_tag_total})"
    return "shortest path"


def score_group(group: dict[str, Any]) -> None:
    """Select canonical file and generate rationale string.

    Ranking: highest bitrate -> most complete tags -> shortest path. ``bitrate`` is stored in
    BITS per second (phaze-iw2k); the ranking itself is unaffected by unit (every stored value
    shares the same unit, so ordering is preserved) but the rationale string divides by 1000 to
    render kbps for display.
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

    # Determine what actually differentiated the winner from the runner-up
    group["rationale"] = _canonical_rationale(winner, files[1] if len(files) > 1 else None)


def _dup_hash_subquery(limit: int, offset: int) -> Subquery:
    """Build the paginated "hashes with >1 file" subquery, ORDERED so LIMIT/OFFSET is deterministic.

    Postgres's ``GROUP BY ... HAVING`` aggregate output order is unspecified and plan-dependent --
    different LIMIT/OFFSET values are likely to produce different plans/orders. Without an explicit
    ORDER BY here, two calls for "the same page" (or two adjacent pages) can select a DIFFERENT set of
    hashes, so the review UI can silently show a duplicate group twice while another is never shown.
    Ordering by ``sha256_hash`` before LIMIT/OFFSET makes the selected page of hashes stable and
    reproducible across calls (the outer queries below additionally ORDER BY sha256_hash for display,
    but that sorts only the hashes THIS subquery already selected -- it can't fix an unstable selection).

    phaze-4iq5t REJECTED ALTERNATIVE, recorded so it is not naively re-attempted: sorting by member
    COUNT instead of (or before) ``sha256_hash`` -- "biggest groups land on the first page" -- was
    tried and reverted during this same bead's review. ``sha256_hash`` is provably unique per row
    here AND immutable (a group's hash never changes); member count is neither -- it changes every
    time a scan adds/removes a duplicate file. Sorting by a MUTABLE key under LIMIT/OFFSET is the
    classic offset-pagination bug: a group that crosses the page boundary between an operator's
    initial load and their "Load more" click (because a concurrent scan changed its count) is either
    skipped entirely or rendered twice -- silently, which is exactly the class of bug this bead
    exists to eliminate. Reintroducing it under the "operator-meaningful order" acceptance option
    (AC1(c)) would trade a static, always-reproducible cap for a dynamic, load-timing-dependent one.
    Since real pagination now exists (GET /duplicates/groups), every group is reachable regardless of
    hash order -- the ordering only decides what renders FIRST, not what is reachable at all -- so
    the "biggest groups first" benefit was judged not worth paying pagination correctness for. If a
    priority order is wanted later, it needs either a stable snapshot of the ordering key for the
    whole paging session or keyset pagination on an immutable column, not a plain LIMIT/OFFSET sort
    on a mutable one.
    """
    return (
        select(FileRecord.sha256_hash)
        .where(~dedup_resolved_clause())
        .group_by(FileRecord.sha256_hash)
        .having(func.count(FileRecord.id) > 1)
        .order_by(FileRecord.sha256_hash)
        .limit(limit)
        .offset(offset)
        .subquery()
    )


def _select_capped_group_members(hash_filter: ColumnElement[bool], cap: int = _MAX_GROUP_MEMBERS) -> Any:
    """Build the ``(FileRecord, FileMetadata)`` SELECT for ``hash_filter``, capped to ``cap`` + 1 rows PER
    ``sha256_hash`` group (phaze-z4p5q).

    Shared by all three per-member readers: :func:`find_duplicate_group_by_hash` (a single hash),
    :func:`find_duplicate_groups_with_metadata` (a page of group hashes), and
    :func:`find_duplicate_groups_by_hashes` (an exact hash set) -- all three previously ran this same
    join with no LIMIT at all, so a pathological group materialized every member.

    A plain ``.limit()`` on the outer query would cap the TOTAL row count across every group
    ``hash_filter`` matches, not each group independently -- for the two multi-hash callers that would
    starve whichever hashes happen to sort last. Instead, ``ROW_NUMBER() OVER (PARTITION BY
    sha256_hash ORDER BY original_path)`` ranks each group's members independently, and only ids with
    rank <= ``cap`` + 1 are kept. Fetching one row PAST the cap (rather than exactly ``cap``) is what
    lets :func:`_build_metadata_groups` tell "exactly ``cap`` members" apart from "more than ``cap``
    members" and set ``truncated`` accordingly, without a second COUNT query per group.
    """
    ranked = (
        select(
            FileRecord.id,
            func.row_number().over(partition_by=FileRecord.sha256_hash, order_by=FileRecord.original_path).label("rn"),
        )
        .where(hash_filter)
        .where(~dedup_resolved_clause())
    ).subquery()
    capped_ids = select(ranked.c.id).where(ranked.c.rn <= cap + 1)

    return (
        select(FileRecord, FileMetadata)
        .outerjoin(FileMetadata, FileRecord.id == FileMetadata.file_id)
        .where(FileRecord.id.in_(capped_ids))
        .order_by(FileRecord.sha256_hash, FileRecord.original_path)
    )


def _group_member_dict(file_record: FileRecord, metadata: FileMetadata | None) -> dict[str, Any]:
    """Build one duplicate-group member dict from a ``(FileRecord, FileMetadata | None)`` row.

    ``metadata`` is ``None`` for a file whose tag extraction has not run (the readers all
    ``outerjoin`` FileMetadata), so every metadata-derived key degrades to ``None`` rather than the
    row being dropped -- a file with no tags is still a duplicate and must still be rankable by
    :func:`score_group`, which treats a missing bitrate as 0. The three ``tag_*`` keys are derived
    from the dict itself via :func:`tag_completeness` so the "Full/Partial/None" label and the
    numeric tag count a group's ranking uses can never disagree.
    """
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
    return file_dict


def _build_metadata_groups(rows: Iterable[Sequence[Any]], cap: int | None = None) -> list[dict[str, Any]]:
    """Group ``(FileRecord, FileMetadata | None)`` rows into the duplicate-group dict shape.

    Shared by :func:`find_duplicate_group_by_hash` (single group), :func:`find_duplicate_groups_with_metadata`
    (paginated) and :func:`find_duplicate_groups_by_hashes` (exact hash set) so all three build identical
    file dicts.

    ``cap``, when given, marks a group ``"truncated": True`` if it received MORE than ``cap`` rows --
    the caller is expected to have fetched ``cap`` + 1 rows per group via
    :func:`_select_capped_group_members`, so an extra row here is exactly the truncation signal. The
    group's ``"files"``/``"count"`` are then trimmed back down to ``cap`` so the display never shows
    more members than the cap even though one extra was fetched. ``cap=None`` (the default) always
    reports ``"truncated": False`` -- no caller currently passes it, but it keeps this a normal,
    uncapped grouping helper if one needs it.
    """
    groups_map: dict[str, list[dict[str, Any]]] = {}
    for file_record, metadata in rows:
        groups_map.setdefault(file_record.sha256_hash, []).append(_group_member_dict(file_record, metadata))

    groups: list[dict[str, Any]] = []
    for h, members in groups_map.items():
        truncated = cap is not None and len(members) > cap
        if truncated:
            members = members[:cap]
        groups.append(
            {
                "sha256_hash": h,
                "count": len(members),
                "truncated": truncated,
                "files": members,
            }
        )
    return groups


async def find_duplicate_groups(session: AsyncSession, limit: int = GROUP_PAGE_SIZE, offset: int = 0) -> list[dict[str, Any]]:
    """Find groups of files sharing the same SHA256 hash.

    Returns a paginated list of duplicate groups, each containing the
    shared hash, member count, and file details (id, path, size, type).
    Excludes files carrying a dedup_resolution marker (marker-existence is authority, not FileRecord.state).
    """
    dup_hashes = _dup_hash_subquery(limit, offset)

    # Main query: all non-resolved files matching those hashes
    stmt = (
        select(FileRecord)
        .where(FileRecord.sha256_hash.in_(select(dup_hashes.c.sha256_hash)))
        .where(~dedup_resolved_clause())
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


async def find_duplicate_groups_with_metadata(session: AsyncSession, limit: int = GROUP_PAGE_SIZE, offset: int = 0) -> list[dict[str, Any]]:
    """Find duplicate groups with metadata fields included.

    Like find_duplicate_groups but outer-joins FileMetadata to include
    bitrate, duration, artist, title, album, genre, year, track_number
    and tag completeness info in each file dict.
    """
    dup_hashes = _dup_hash_subquery(limit, offset)

    # Main query with outerjoin to metadata -- phaze-z4p5q: capped to _MAX_GROUP_MEMBERS + 1 members
    # PER hash (this call can select many hashes' worth of members at once via the `limit` page of
    # GROUPS above; the per-member cap here is independent of that page size).
    stmt = _select_capped_group_members(FileRecord.sha256_hash.in_(select(dup_hashes.c.sha256_hash)))
    result = await session.execute(stmt)
    return _build_metadata_groups(result.all(), cap=_MAX_GROUP_MEMBERS)


async def find_duplicate_groups_by_hashes(session: AsyncSession, hashes: Sequence[str]) -> list[dict[str, Any]]:
    """Return duplicate groups (with metadata) for an EXACT, caller-supplied set of hashes.

    Used by ``bulk_resolve`` to act on the group hashes the operator was actually shown, instead of
    re-deriving "the current page" via a fresh ``find_duplicate_groups_with_metadata`` LIMIT/OFFSET call --
    which (even with a stable ORDER BY) can select a different set of groups than what was rendered if
    another resolve committed between the page render and this call (display set != write set). No
    LIMIT/OFFSET/HAVING here: the caller's hash set IS the selection, so there is nothing to re-derive.
    """
    if not hashes:
        return []

    # phaze-z4p5q: capped to _MAX_GROUP_MEMBERS + 1 members PER hash, independently of how many
    # hashes are in the caller's set.
    stmt = _select_capped_group_members(FileRecord.sha256_hash.in_(hashes))
    result = await session.execute(stmt)
    return _build_metadata_groups(result.all(), cap=_MAX_GROUP_MEMBERS)


async def find_duplicate_group_by_hash(session: AsyncSession, group_hash: str) -> dict[str, Any] | None:
    """Return the ONE unresolved duplicate group for ``group_hash``, or ``None`` if there isn't one.

    This is a LOOKUP, not a paged read, and it deliberately does not touch
    :mod:`phaze.services.pagination` (phaze-m7ya). Locating a single known group is an indexed
    ``WHERE sha256_hash = :group_hash`` query whose cost and correctness are independent of where that
    group happens to sort among all the others. Paging a lookup would preserve the original defect in a
    nicer costume: ``undo_resolve_endpoint`` (and the since-deleted ``compare_group``, phaze-ur8o3) used
    to fetch a hardcoded first 1000 groups and linear-scan them, so any group past that arbitrary
    boundary reported "Group not found" and could never be reviewed or resolved through the UI. Widening
    the cap, or walking pages until the hash turns up, would only move the boundary; removing the scan
    removes the class of bug. It also drops an O(all groups) metadata fetch that ran on EVERY mutation
    that has to re-render one group's card.

    Returns ``None`` for a hash that is unknown, already resolved, or no longer a duplicate (down to a
    single remaining file) -- all of which are ordinary states a stale card can ask about, not errors.
    The ``count > 1`` check mirrors the ``HAVING count(id) > 1`` in :func:`_dup_hash_subquery` so a
    lookup agrees with the list about what counts as a group.

    phaze-z4p5q: this used to fetch every unresolved member with no LIMIT at all, so an operator
    Compare/Undo click on a pathological group (tens of thousands of byte-identical members) would
    materialize all of them -- and one metadata dict each -- into the API process. Now capped to
    ``_MAX_GROUP_MEMBERS`` (with ``"truncated": True`` set on the returned dict if the real group is
    larger); the ``count > 1`` existence check above never needs the full membership either way.
    """
    stmt = _select_capped_group_members(FileRecord.sha256_hash == group_hash)
    result = await session.execute(stmt)
    groups = _build_metadata_groups(result.all(), cap=_MAX_GROUP_MEMBERS)
    if not groups or groups[0]["count"] <= 1:
        return None
    return groups[0]


async def count_duplicate_groups(session: AsyncSession) -> int:
    """Count the total number of duplicate groups (hashes with >1 file).

    Returns the number of distinct SHA256 hashes that have more than one file.
    Excludes files carrying a dedup_resolution marker (marker-existence is authority, not FileRecord.state).

    One aggregate query; no group members are materialized. That is what makes it the right answer to
    "how many duplicate groups are there" anywhere a corpus-wide number is wanted, and
    phaze-tzy6s.17 rewired the Execute preflight onto it for exactly that reason:
    ``len(await get_dedupe_groups(session))`` is NOT a corpus count and reads as one. That path goes
    through :func:`find_duplicate_groups_with_metadata`, whose ``limit`` defaults to
    :data:`GROUP_PAGE_SIZE` (**100**) and PAGES, so a corpus with 100 duplicate groups and one with
    100,000 both reported "100" -- on the final confirmation dialog before an irreversible batch, the
    one screen whose whole job is to be accurate about scale. Prefer this over a bigger page size; a
    page's length is never a total. (phaze-4iq5t: the Dedupe workspace ITSELF used to make this same
    mistake, unnoticed, because nothing paged past page 1 -- see :data:`GROUP_PAGE_SIZE`.)

    Counts the same population the paged reader selects, and deliberately carries no
    ``ORDER BY``/``LIMIT``/``OFFSET``: :func:`_dup_hash_subquery` needs them because a PAGE must be
    stable and bounded, whereas a COUNT over the same grouping is order-independent, and a limit here
    would be the very defect described above.
    """
    subq = (
        select(FileRecord.sha256_hash)
        .where(~dedup_resolved_clause())
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

    # Subquery: hashes with >1 file (excluding marker-resolved)
    dup_hashes = (
        select(FileRecord.sha256_hash)
        .where(~dedup_resolved_clause())
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
        ~dedup_resolved_clause(),
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
            ~dedup_resolved_clause(),
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


class StaleDuplicateReviewError(Exception):
    """The unresolved group no longer exactly matches its reviewed snapshot."""


async def _assert_reviewed_snapshot(
    session: AsyncSession,
    group_hash: str,
    canonical_id: uuid_mod.UUID,
    reviewed_member_ids: set[uuid_mod.UUID],
) -> None:
    """Freeze ``group_hash``'s membership and refuse the resolve unless it still matches the review.

    Raises :class:`StaleDuplicateReviewError` when ``canonical_id`` was not among the reviewed
    members, or when the group's currently-unresolved membership differs in ANY direction from
    ``reviewed_member_ids`` -- a member added or removed since the operator looked at the card means
    they approved a different group than the one about to be resolved.

    Duplicate review is rare and destructive, so the ``LOCK TABLE files IN SHARE MODE`` is taken
    BEFORE the read and held through the caller's COMMIT: SHARE permits concurrent readers but
    blocks files-table INSERT/UPDATE/DELETE writers, so a scan cannot insert an unseen member after
    the exact-set check nor delete a reviewed member before the markers are inserted. Called only on
    the reviewed path -- :func:`resolve_group` with ``reviewed_member_ids=None`` (every internal
    caller) never takes this lock.
    """
    await session.execute(text("LOCK TABLE files IN SHARE MODE"))
    current_result = await session.execute(
        select(FileRecord.id).where(FileRecord.sha256_hash == group_hash, ~dedup_resolved_clause()).order_by(FileRecord.id)
    )
    current_member_ids = set(current_result.scalars().all())
    if canonical_id not in reviewed_member_ids or current_member_ids != reviewed_member_ids:
        raise StaleDuplicateReviewError


async def _insert_resolution_markers(session: AsyncSession, files: Sequence[FileRecord], canonical_id: uuid_mod.UUID) -> bool:
    """Insert one ``DedupResolution`` marker per non-canonical ``files`` row. False on a lost race.

    D-01/D-02/D-03/D-07: the go-forward dedup_resolution writer that has not existed since 032's
    one-shot backfill. One bulk pg_insert per chunk, ON CONFLICT (file_id) DO NOTHING (idempotent
    under an HTMX double-submit -- first-writer-wins), inside the caller-owned txn (flush, never
    commit). Each row stamps the operator's actual ``canonical_id`` (strictly better than 032's
    ``ORDER BY c.id LIMIT 1`` guess) and an explicit id -- pg_insert bypasses ``DedupResolution.id``'s
    Python-side ``default=uuid.uuid4``, so omitting it is a NULL-PK failure (agent_analysis.py:204
    precedent). ``resolved_at`` rides its server_default.

    phaze-p3qr: chunk_rows guards the same 32767-bind-parameter cap as the companion.py sibling
    finding, even though a realistic byte-identical-duplicate group is nowhere near the >10,922-row
    break in practice; the guard is one line, so it costs nothing to apply here too rather than
    leave the shape unchunked (bulk_insert.py's atomicity rule holds -- every chunk lands on this
    session, and the caller still owns the single commit/flush).

    phaze-a3if1: the whole insert runs inside ONE SAVEPOINT (not per-chunk) so a mid-loop FK failure
    unwinds every chunk of THIS resolve atomically, never a partial marker set, and leaves the
    caller's outer transaction usable for the rest of the request (request_guards.py contract rule 5
    -- a nested rollback, never ``session.rollback()``, which would expire every already-loaded ORM
    object on the session). An ``IntegrityError`` means a member of ``files`` (or ``canonical_id``
    itself) was deleted by a concurrent scan-batch cascade between the caller's SELECT and this
    INSERT -- contract rule 4: catch the race, not the typo. Returning False lets the caller report
    the same 0-resolved shape every other no-op in :func:`resolve_group` already returns.
    """
    rows = [{"id": uuid_mod.uuid4(), "file_id": f.id, "canonical_file_id": canonical_id} for f in files]
    try:
        async with session.begin_nested():
            for chunk in chunk_rows(rows):
                await session.execute(pg_insert(DedupResolution).values(chunk).on_conflict_do_nothing(index_elements=["file_id"]))
    except IntegrityError:
        return False
    return True


async def resolve_group(
    session: AsyncSession,
    group_hash: str,
    canonical_id: uuid_mod.UUID,
    *,
    reviewed_member_ids: set[uuid_mod.UUID] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Mark non-canonical files in a duplicate group as resolved via the durable DedupResolution marker.

    Returns (count_resolved, [{id}]) for undo tracking. Phase 90 (D-09): the DUPLICATE_RESOLVED
    files.state dual-write was removed -- the DedupResolution marker (dedup_resolved_clause) is the sole
    derived authority, so the returned payload no longer carries a previous_state.

    phaze-xasy: ``canonical_id`` may be caller-supplied by internal callers and is NOT trusted to
    actually be a member of ``group_hash`` -- the only DB backstop on ``canonical_file_id`` is a
    plain FK to ``files.id``, which is satisfied by ANY live file, group member or not. Without this
    membership check, a stale/mismatched ``canonical_id`` (e.g. a replayed form after the original
    keeper was deleted and re-scanned under a new UUID) makes ``FileRecord.id != canonical_id`` below
    exclude NO group member, so every copy -- including the intended keeper -- gets a
    ``DedupResolution`` marker pointing at an unrelated file, and the group silently vanishes with
    zero surviving canonical. Verify ``canonical_id`` names a currently-unresolved member of THIS
    group first; a mismatch is a no-op (0 resolved), not a resolve against the wrong file.

    phaze-v0iy: the phaze-xasy guard above closed the WRONG-canonical path but not the CONCURRENT
    one. Two overlapping resolves of the SAME group picking DIFFERENT keepers (X and Y) each ran the
    membership check and the insert as a check-then-act on DIFFERENT rows: the guard reads row
    ``canonical_id``, the insert writes ``members \\ {canonical_id}``. Under READ COMMITTED, request A
    (canonical=X) and request B (canonical=Y) each see the full unresolved group in their own
    snapshot, both pass the membership check, A inserts markers for ``members \\ X`` (which includes
    Y) and B inserts markers for ``members \\ Y`` (which includes X). Those two insert sets share no
    row -- worst case is the common 2-member group, where they are completely disjoint -- so
    ``ON CONFLICT (file_id) DO NOTHING`` never fires for either keeper, and after both commits every
    file in the group (including both intended keepers) carries a marker: the group vanishes with
    ZERO surviving canonical. A ``pg_advisory_xact_lock`` keyed on the group hash serializes the two
    requests so the SECOND one's membership re-check runs against the FIRST one's already-committed
    result: it sees its own canonical already marked non-canonical and correctly no-ops (0 resolved)
    instead of minting a second, conflicting resolution. The lock is transaction-scoped (released at
    the caller's ``commit()``/``rollback()``), matching how both call sites (``resolve_group_endpoint``
    and the ``bulk_resolve`` loop) run inside a caller-owned transaction.

    phaze-a3if1: the membership/non-canonical reads above are plain SELECTs with no ``FOR SHARE``, so
    they do NOT block on ``delete_scan_cascade``'s ``SELECT ... FOR UPDATE`` -- a group member (or
    ``canonical_id`` itself) can be deleted by a concurrent scan-batch delete AFTER this function's own
    read sees it and BEFORE the ``DedupResolution`` INSERT below runs. ``delete_scan_cascade``'s own
    comment names the expected outcome: the losing writer "simply FK-fails against the already-deleted
    file". The INSERT therefore runs inside its own SAVEPOINT (request_guards.py contract rule 5) and
    an ``IntegrityError`` there is caught (rule 4 -- a genuine race no stricter signature could have
    rejected, not a validation bug), reporting 0 resolved instead of letting the FK violation escape
    uncaught. That return value is indistinguishable from every other 0-resolved shape this function
    already has (stale canonical, concurrent no-op), so the router's existing 0-resolved handling
    (phaze-ptzse) covers this race too without a separate branch.
    """
    # phaze-v0iy: serialize per group_hash BEFORE the membership check so a concurrent resolve of the
    # same group (any canonical) blocks here until the first resolve's transaction commits or rolls
    # back, then re-evaluates the check-then-act pair against that committed state. hashtext() is a
    # stable-within-a-session 32-bit hash of the group's sha256 hex string; pg_advisory_xact_lock takes
    # an implicit bigint widening of that int4 result. This is an xact lock: it is automatically
    # released at COMMIT/ROLLBACK, so it must not be taken with autocommit / outside the caller's txn.
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(group_hash))))

    if reviewed_member_ids is not None:
        await _assert_reviewed_snapshot(session, group_hash, canonical_id, reviewed_member_ids)

    canonical_membership_stmt = select(FileRecord.id).where(
        FileRecord.sha256_hash == group_hash,
        FileRecord.id == canonical_id,
        ~dedup_resolved_clause(),
    )
    canonical_membership = await session.execute(canonical_membership_stmt)
    if canonical_membership.scalar_one_or_none() is None:
        return 0, []

    # Find all files in this group except the canonical one
    stmt = select(FileRecord).where(
        FileRecord.sha256_hash == group_hash,
        FileRecord.id != canonical_id,
        ~dedup_resolved_clause(),
    )
    if reviewed_member_ids is not None:
        # Never archive a member that was not in the reviewed snapshot, even if one appears after
        # the exact-set check above through a concurrent scan.
        stmt = stmt.where(FileRecord.id.in_(reviewed_member_ids))
    result = await session.execute(stmt)
    files = result.scalars().all()

    # Phase 90 (D-09): the per-file DUPLICATE_RESOLVED files.state write (and its previous_state capture)
    # was removed as a matched set; the DedupResolution marker below is the sole resolution authority.
    # phaze-btix: the payload also echoes THIS call's ``canonical_id`` per entry, so ``undo_resolve``
    # can scope its DELETE to the (file_id, canonical_file_id) pair a marker was actually written
    # with -- see undo_resolve's docstring for why file_id alone is not a safe CAS anchor.
    file_states: list[dict[str, Any]] = [{"id": str(f.id), "canonical_id": str(canonical_id)} for f in files]

    if files and not await _insert_resolution_markers(session, files, canonical_id):
        # Lost the race with a concurrent scan-batch delete -- same 0-resolved shape as every other
        # no-op above. See _insert_resolution_markers for the full phaze-a3if1 rationale.
        return 0, []

    await session.flush()
    return len(file_states), file_states


async def undo_resolve(session: AsyncSession, file_states: list[Any]) -> int:
    """Undo a group resolution: DELETE the dedup markers, keyed on the payload's (file_id, canonical_id) pairs.

    Accepts the browser-held ``[{id, canonical_id}]`` payload. The element type is ``Any``, not
    ``dict``, ON PURPOSE (phaze-wkqk): this consumes an UNTRUSTED array whose elements the router
    deliberately does not validate, and a ``dict`` annotation would be a lie that hides the
    isinstance guard below from the type checker.

    phaze-btix: the marker DELETE is scoped by file_id ALONE, an earlier version of this docstring
    called that a "CAS anchor" and claimed "a stale-tab replay against a file re-resolved since finds
    no marker, returns zero rows, and no-ops" -- which was false. ``dedup_resolutions`` is one row
    per ``file_id`` (unique), and ``resolve_group`` never surfaced the marker's own random ``id`` to
    the browser, so a replayed undo COULD NOT tell its own marker apart from a different, later
    marker written for the same file (resolve -> undo -> re-resolve with a different canonical ->
    stale undo replay silently reverts the newer resolution). ``resolve_group`` now echoes the
    ``canonical_id`` it actually wrote alongside each file id, and the DELETE below requires BOTH to
    match the marker currently on file (``(file_id, canonical_file_id) IN (payload pairs)``). A
    marker written by a DIFFERENT resolution (undo + re-resolve since this payload was minted) has a
    different ``canonical_file_id``, so the pair no longer matches, the DELETE finds no row, and the
    stale replay genuinely no-ops -- making the documented CAS behavior real. Returns the count
    actually undone (callers do not use it for control flow).

    Threat mitigation (T-84-03-01/02 / T-90A-04): the DELETE is scoped to pairs the operator's own
    payload names, so it can only remove markers this call's own resolution actually wrote.

    NO HTTP 500 ON ANY PAYLOAD SHAPE (phaze-wkqk). This is the ELEMENT half of the untrusted-input
    contract (``routers/request_guards.py`` rule 2): an entry that is not a dict, or whose ``id`` or
    ``canonical_id`` is absent / not a UUID, is SKIPPED and the rest of the payload still undoes. It
    is not escalated -- one stale/legacy entry must not void an otherwise valid bulk undo, and the
    returned count is the authority on what actually happened. The ENVELOPE half (``file_states``
    unparseable, or valid JSON that is not an array) is rejected with 422 by the router before it
    reaches here. The claim in this paragraph is backed by tests (rule 6), not merely asserted --
    see ``tests/discovery/services/test_dedup.py`` and ``tests/review/routers/test_duplicates.py``.

    Note this docstring previously claimed the no-500 guarantee while covering only the id VALUE; a
    non-dict entry reached ``entry.get`` and raised ``AttributeError`` -> 500. That gap is the case
    study rule 6 was written from.
    """
    # (1) DERIVED AUTHORITY: build the DELETE (file_id, canonical_id) pair-set from each entry's
    #     ``id`` and ``canonical_id`` (accept UUID-or-str, drop non-UUIDs). A malformed or legacy
    #     (canonical_id-less) entry must not escape as an unhandled error, AND must not be treated as
    #     "match any canonical" -- that would resurrect the file_id-only CAS bypass this fixes.
    pairs: set[tuple[uuid_mod.UUID, uuid_mod.UUID]] = set()
    for entry in file_states:
        if not isinstance(entry, dict):
            # phaze-wkqk: valid JSON of the wrong SHAPE -- ``[1, 2]``, ``["a"]``, a list of nulls.
            # Parsing succeeded, so the router's envelope guard passed it through; without this the
            # ``entry.get`` below raises AttributeError and escapes as a 500.
            continue
        file_id = _coerce_uuid(entry.get("id"))
        canonical_id = _coerce_uuid(entry.get("canonical_id"))
        if file_id is not None and canonical_id is not None:
            pairs.add((file_id, canonical_id))

    # (2) The gate is pair-based -- an entry missing either half (malformed, or a legacy id-only
    #     payload from before phaze-btix) is skipped rather than matched loosely.
    if not pairs:
        return 0

    # (3) CAS: DELETE only markers whose CURRENT (file_id, canonical_file_id) matches a pair this
    #     payload's own resolution wrote, RETURNING the file_ids that actually held one
    #     (scan_deletion.py:119 async ORM-DELETE hygiene). This is the sole derived undo authority.
    result = await session.execute(
        delete(DedupResolution)
        .where(tuple_(DedupResolution.file_id, DedupResolution.canonical_file_id).in_(list(pairs)))
        .returning(DedupResolution.file_id)
        .execution_options(synchronize_session=False)
    )
    returned: set[uuid_mod.UUID] = set(result.scalars().all())
    await session.flush()
    return len(returned)


def _coerce_uuid(raw: Any) -> uuid_mod.UUID | None:
    """Best-effort UUID coercion for an untrusted payload field -- ``None`` on anything unusable.

    Shared by :func:`undo_resolve` for both the ``id`` and ``canonical_id`` halves of each entry.
    """
    if isinstance(raw, uuid_mod.UUID):
        return raw
    if isinstance(raw, str):
        try:
            return uuid_mod.UUID(raw)
        except ValueError:
            return None
    return None
