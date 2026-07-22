"""Duplicate detection service: finds files sharing the same SHA256 hash."""

from collections.abc import Iterable, Sequence
from typing import Any
import uuid as uuid_mod

from sqlalchemy import Subquery, delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.stage_status import dedup_resolved_clause


TAG_FIELDS = ["artist", "title", "album", "year", "genre", "track_number"]


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


def score_group(group: dict[str, Any]) -> None:
    """Select canonical file and generate rationale string.

    Ranking: highest bitrate -> most complete tags -> shortest path.
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

    winner_bitrate = winner.get("bitrate") or 0
    winner_tags = winner.get("tag_filled", 0)
    winner_tag_total = winner.get("tag_total", len(TAG_FIELDS))

    # Determine what actually differentiated the winner from the runner-up
    runner_up = files[1] if len(files) > 1 else None
    runner_bitrate = (runner_up.get("bitrate") or 0) if runner_up else 0
    runner_tags = (runner_up.get("tag_filled", 0)) if runner_up else 0

    if winner_bitrate > 0 and winner_bitrate > runner_bitrate:
        group["rationale"] = f"highest bitrate ({winner_bitrate}kbps)"
    elif winner_tags > 0 and winner_tags > runner_tags:
        group["rationale"] = f"most complete tags ({winner_tags}/{winner_tag_total})"
    else:
        group["rationale"] = "shortest path"


def _dup_hash_subquery(limit: int, offset: int) -> Subquery:
    """Build the paginated "hashes with >1 file" subquery, ORDERED so LIMIT/OFFSET is deterministic.

    Postgres's ``GROUP BY ... HAVING`` aggregate output order is unspecified and plan-dependent --
    different LIMIT/OFFSET values are likely to produce different plans/orders. Without an explicit
    ORDER BY here, two calls for "the same page" (or two adjacent pages) can select a DIFFERENT set of
    hashes, so the review UI can silently show a duplicate group twice while another is never shown.
    Ordering by ``sha256_hash`` before LIMIT/OFFSET makes the selected page of hashes stable and
    reproducible across calls (the outer queries below additionally ORDER BY sha256_hash for display,
    but that sorts only the hashes THIS subquery already selected -- it can't fix an unstable selection).
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


def _build_metadata_groups(rows: Iterable[Sequence[Any]]) -> list[dict[str, Any]]:
    """Group ``(FileRecord, FileMetadata | None)`` rows into the duplicate-group dict shape.

    Shared by :func:`find_duplicate_groups_with_metadata` (paginated) and
    :func:`find_duplicate_groups_by_hashes` (exact hash set) so both build identical file dicts.
    """
    groups_map: dict[str, list[dict[str, Any]]] = {}
    for file_record, metadata in rows:
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
        groups_map.setdefault(file_record.sha256_hash, []).append(file_dict)

    return [
        {
            "sha256_hash": h,
            "count": len(members),
            "files": members,
        }
        for h, members in groups_map.items()
    ]


async def find_duplicate_groups(session: AsyncSession, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
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


async def find_duplicate_groups_with_metadata(session: AsyncSession, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Find duplicate groups with metadata fields included.

    Like find_duplicate_groups but outer-joins FileMetadata to include
    bitrate, duration, artist, title, album, genre, year, track_number
    and tag completeness info in each file dict.
    """
    dup_hashes = _dup_hash_subquery(limit, offset)

    # Main query with outerjoin to metadata
    stmt = (
        select(FileRecord, FileMetadata)
        .outerjoin(FileMetadata, FileRecord.id == FileMetadata.file_id)
        .where(FileRecord.sha256_hash.in_(select(dup_hashes.c.sha256_hash)))
        .where(~dedup_resolved_clause())
        .order_by(FileRecord.sha256_hash, FileRecord.original_path)
    )
    result = await session.execute(stmt)
    return _build_metadata_groups(result.all())


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

    stmt = (
        select(FileRecord, FileMetadata)
        .outerjoin(FileMetadata, FileRecord.id == FileMetadata.file_id)
        .where(FileRecord.sha256_hash.in_(hashes))
        .where(~dedup_resolved_clause())
        .order_by(FileRecord.sha256_hash, FileRecord.original_path)
    )
    result = await session.execute(stmt)
    return _build_metadata_groups(result.all())


async def find_duplicate_group_by_hash(session: AsyncSession, group_hash: str) -> dict[str, Any] | None:
    """Return the ONE unresolved duplicate group for ``group_hash``, or ``None`` if there isn't one.

    This is a LOOKUP, not a paged read, and it deliberately does not touch
    :mod:`phaze.services.pagination` (phaze-m7ya). Locating a single known group is an indexed
    ``WHERE sha256_hash = :group_hash`` query whose cost and correctness are independent of where that
    group happens to sort among all the others. Paging a lookup would preserve the original defect in a
    nicer costume: ``compare_group`` and ``undo_resolve_endpoint`` used to fetch a hardcoded first 1000
    groups and linear-scan them, so any group past that arbitrary boundary reported "Group not found"
    and could never be reviewed or resolved through the UI -- even though the list page happily rendered
    it with a Compare button. Widening the cap, or walking pages until the hash turns up, would only
    move the boundary; removing the scan removes the class of bug. It also drops an O(all groups)
    metadata fetch that ran on EVERY single card expand.

    Returns ``None`` for a hash that is unknown, already resolved, or no longer a duplicate (down to a
    single remaining file) -- all of which are ordinary states a stale card can ask about, not errors.
    The ``count > 1`` check mirrors the ``HAVING count(id) > 1`` in :func:`_dup_hash_subquery` so a
    lookup agrees with the list about what counts as a group.
    """
    stmt = (
        select(FileRecord, FileMetadata)
        .outerjoin(FileMetadata, FileRecord.id == FileMetadata.file_id)
        .where(FileRecord.sha256_hash == group_hash)
        .where(~dedup_resolved_clause())
        .order_by(FileRecord.original_path)
    )
    result = await session.execute(stmt)
    groups = _build_metadata_groups(result.all())
    if not groups or groups[0]["count"] <= 1:
        return None
    return groups[0]


async def count_duplicate_groups(session: AsyncSession) -> int:
    """Count the total number of duplicate groups (hashes with >1 file).

    Returns the number of distinct SHA256 hashes that have more than one file.
    Excludes files carrying a dedup_resolution marker (marker-existence is authority, not FileRecord.state).
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


async def resolve_group(session: AsyncSession, group_hash: str, canonical_id: uuid_mod.UUID) -> tuple[int, list[dict[str, Any]]]:
    """Mark non-canonical files in a duplicate group as resolved via the durable DedupResolution marker.

    Returns (count_resolved, [{id}]) for undo tracking. Phase 90 (D-09): the DUPLICATE_RESOLVED
    files.state dual-write was removed -- the DedupResolution marker (dedup_resolved_clause) is the sole
    derived authority, so the returned payload no longer carries a previous_state.

    phaze-xasy: ``canonical_id`` is caller-supplied (a browser Form field) and is NOT trusted to
    actually be a member of ``group_hash`` -- the only DB backstop on ``canonical_file_id`` is a
    plain FK to ``files.id``, which is satisfied by ANY live file, group member or not. Without this
    membership check, a stale/mismatched ``canonical_id`` (e.g. a replayed form after the original
    keeper was deleted and re-scanned under a new UUID) makes ``FileRecord.id != canonical_id`` below
    exclude NO group member, so every copy -- including the intended keeper -- gets a
    ``DedupResolution`` marker pointing at an unrelated file, and the group silently vanishes with
    zero surviving canonical. Verify ``canonical_id`` names a currently-unresolved member of THIS
    group first; a mismatch is a no-op (0 resolved), not a resolve against the wrong file.
    """
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
    result = await session.execute(stmt)
    files = result.scalars().all()

    # Phase 90 (D-09): the per-file DUPLICATE_RESOLVED files.state write (and its previous_state capture)
    # was removed as a matched set; the DedupResolution marker below is the sole resolution authority.
    # phaze-btix: the payload also echoes THIS call's ``canonical_id`` per entry, so ``undo_resolve``
    # can scope its DELETE to the (file_id, canonical_file_id) pair a marker was actually written
    # with -- see undo_resolve's docstring for why file_id alone is not a safe CAS anchor.
    file_states: list[dict[str, Any]] = [{"id": str(f.id), "canonical_id": str(canonical_id)} for f in files]

    # D-01/D-02/D-03/D-07: the go-forward dedup_resolution writer that has not existed since 032's
    # one-shot backfill. One bulk pg_insert for every non-canonical file in the group, ON CONFLICT
    # (file_id) DO NOTHING (idempotent under an HTMX double-submit — first-writer-wins), inside the
    # caller-owned txn (flush, never commit). Each row stamps the operator's actual canonical_id
    # (strictly better than 032's ORDER BY c.id LIMIT 1 guess) and an explicit id — pg_insert bypasses
    # DedupResolution.id's Python-side default=uuid.uuid4, so omitting it is a NULL-PK failure
    # (agent_analysis.py:204 precedent). resolved_at rides its server_default.
    if files:
        rows = [{"id": uuid_mod.uuid4(), "file_id": f.id, "canonical_file_id": canonical_id} for f in files]
        await session.execute(pg_insert(DedupResolution).values(rows).on_conflict_do_nothing(index_elements=["file_id"]))

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
