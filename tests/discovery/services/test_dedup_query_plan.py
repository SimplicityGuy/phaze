"""phaze-nrww1: the duplicate-group readers' plans must not depend on ``files`` statistics.

When ``files`` was last ANALYZEd holding 0-1 live rows, the member query used to plan as a Nested
Loop Semi Join whose inner side was the whole ``row_number()`` WindowAgg -- and, beneath it, the
GROUP BY/HAVING page aggregate -- re-evaluated once per outer row. Measured on 503 rows: 52-73 s
against 0.005 s, the aggregate executing 252,508 times. The suite hits that state whenever
autoanalyze catches a rolled-back table near-empty; a production table would hit it after any
truncate-and-reload or restore that analyzes before the rows land.

The plan-shape test forces that statistics state, runs each reader's REAL statement (captured at the
cursor, so the assertion is about what ships, not a re-built copy) under ``EXPLAIN (ANALYZE, FORMAT
JSON)``, and asserts every Aggregate and WindowAgg node executed exactly once. That is the invariant
the fix buys; a timing bound would only sample it.

The equivalence tests pin that the rewrite returns exactly what the previous query shape returned, over
mixed duplicate groups, by running the previous shape as an in-test oracle.

Side effect, deliberately accepted: ANALYZE's ``pg_class.reltuples``/``relpages`` update is in-place
and survives the test's rollback, so this seat DB's ``files`` is left looking empty to the planner --
exactly the state autoanalyze leaves it in during an ordinary suite run.
"""

from collections.abc import Awaitable, Callable
import json
from typing import Any
import uuid

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services import dedup
from phaze.services.stage_status import dedup_resolved_clause


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64

# Enough members that a per-row re-evaluation is unmistakable in the loop counts (62 outer rows), few
# enough that the pre-fix plan still finishes in well under a second.
_BIG_GROUP = 60
# Rolled-back filler rows: they only set ``relpages``. Measured: below ~200 the plain IN-subquery page
# filter still planned its aggregate once, so a smaller filler would leave find_duplicate_groups' case
# unable to fail on the pre-fix code.
_FILLER = 200


def _make_file(original_path: str, sha256_hash: str, file_type: str = "mp3") -> FileRecord:
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=sha256_hash,
        original_path=original_path,
        original_filename=original_path.rsplit("/", 1)[-1],
        current_path=original_path,
        file_type=file_type,
        file_size=1000,
    )


async def _force_near_empty_stats(session: AsyncSession) -> None:
    """Leave ``files`` ANALYZEd at zero live rows but with non-empty pages -- the state that flipped the plan.

    Rows inserted then rolled back leave dead tuples, so ``relpages`` is non-zero while ``reltuples`` is
    0. (A never-extended table, ``relpages = 0``, falls back to the planner's minimum-size heuristic and
    plans fast, which is why this is seeded rather than relying on whatever earlier tests left behind.)
    """
    session.add_all([_make_file(f"/filler/{i:03d}.mp3", HASH_E) for i in range(_FILLER)])
    await session.flush()
    await session.rollback()
    await session.execute(text("ANALYZE files, metadata, dedup_resolution"))
    reltuples, relpages = (await session.execute(text("SELECT reltuples, relpages FROM pg_class WHERE relname = 'files'"))).one()
    assert reltuples <= 1, f"precondition: files must look (near-)empty to the planner, got reltuples={reltuples}"
    assert relpages > 0, f"precondition: files must have pages on disk, got relpages={relpages}"


def _loops_by_node(plan: dict[str, Any]) -> list[tuple[str, int]]:
    """Flatten an EXPLAIN (ANALYZE, FORMAT JSON) tree into ``(node type, actual loops)`` pairs."""
    nodes = [(plan["Node Type"], plan["Actual Loops"])]
    for child in plan.get("Plans", []):
        nodes.extend(_loops_by_node(child))
    return nodes


async def _explain_captured_select(
    session: AsyncSession, db_connection: AsyncConnection, call: Callable[[AsyncSession], Awaitable[Any]]
) -> list[tuple[str, int]]:
    """Run ``call``, capture the one SELECT it sends, and return that statement's executed plan nodes."""
    captured: list[tuple[str, Any]] = []

    def _capture(conn: object, cursor: object, statement: str, parameters: Any, context: object, executemany: bool) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            captured.append((statement, parameters))

    sync_conn = db_connection.sync_connection
    event.listen(sync_conn, "before_cursor_execute", _capture)
    try:
        await call(session)
    finally:
        event.remove(sync_conn, "before_cursor_execute", _capture)

    assert len(captured) == 1, f"expected the reader to send exactly one SELECT, saw {[s for s, _ in captured]}"
    statement, parameters = captured[0]
    raw = (await session.connection()).exec_driver_sql("EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters)
    plan = (await raw).scalar_one()
    plan = json.loads(plan) if isinstance(plan, str) else plan
    return _loops_by_node(plan[0]["Plan"])


_READERS: dict[str, Callable[[AsyncSession], Awaitable[Any]]] = {
    "find_duplicate_groups_with_metadata": dedup.find_duplicate_groups_with_metadata,
    "find_duplicate_groups": dedup.find_duplicate_groups,
    "find_duplicate_groups_by_hashes": lambda s: dedup.find_duplicate_groups_by_hashes(s, [HASH_A, HASH_B]),
    "find_duplicate_group_by_hash": lambda s: dedup.find_duplicate_group_by_hash(s, HASH_A),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", sorted(_READERS))
async def test_reader_evaluates_its_window_and_aggregate_once_under_near_empty_stats(
    session: AsyncSession, _db_connection: AsyncConnection, reader: str
) -> None:
    """Under the stats state that flipped the plan, no Aggregate or WindowAgg is re-run per row.

    Before phaze-nrww1 the member query's WindowAgg ran once per outer row (62 loops here) and the page
    aggregate once per row pair (3,784); the IN-subquery page filter alone re-ran its aggregate once per
    candidate row. Every such node must now execute exactly once.
    """
    await _force_near_empty_stats(session)
    session.add_all([_make_file(f"/dir/a/{i:03d}.mp3", HASH_A) for i in range(_BIG_GROUP)])
    session.add_all([_make_file("/dir/b/1.mp3", HASH_B), _make_file("/dir/b/2.mp3", HASH_B)])
    await session.flush()

    nodes = await _explain_captured_select(session, _db_connection, _READERS[reader])

    repeated = [(node, loops) for node, loops in nodes if node in {"Aggregate", "WindowAgg"} and loops != 1]
    assert not repeated, f"{reader}: aggregate/window nodes re-evaluated per row: {repeated} (full plan: {nodes})"


# --- equivalence with the previous query shape --------------------------------------------------------


def _previous_capped_members(hash_filter: Any, cap: int) -> Any:
    """The member query exactly as it stood before phaze-nrww1: filter ``files`` back through ``id IN (ranked)``."""
    ranked = (
        select(
            FileRecord.id,
            func.row_number().over(partition_by=FileRecord.sha256_hash, order_by=FileRecord.original_path).label("rn"),
        )
        .where(hash_filter)
        .where(dedup._dedup_population_clause())
        .where(~dedup_resolved_clause())
    ).subquery()
    capped_ids = select(ranked.c.id).where(ranked.c.rn <= cap + 1)
    return (
        select(FileRecord, FileMetadata)
        .outerjoin(FileMetadata, FileRecord.id == FileMetadata.file_id)
        .where(FileRecord.id.in_(capped_ids))
        .order_by(FileRecord.sha256_hash, FileRecord.original_path)
    )


def _previous_page_filter(limit: int, offset: int) -> Any:
    """The page filter exactly as it stood before phaze-nrww1: a plain ``IN (subquery)``."""
    return FileRecord.sha256_hash.in_(select(dedup._dup_hash_subquery(limit, offset).c.sha256_hash))


async def _seed_mixed_groups(session: AsyncSession) -> None:
    """Groups of 5, 3, 2 and 1 members, with a resolved member, a companion row and partial metadata.

    HASH_A (5 media members) exceeds the small caps the tests pass, HASH_B has one resolved member (so
    2 unresolved remain), HASH_C has one media member plus a companion (so it is NOT a media duplicate),
    and HASH_D is a plain pair. Half the members carry FileMetadata so the outer join is exercised both ways.
    """
    files = [_make_file(f"/dir/a/{i}.mp3", HASH_A) for i in (3, 1, 4, 0, 2)]
    files += [_make_file(f"/dir/b/{i}.flac", HASH_B, "flac") for i in range(3)]
    files += [_make_file("/dir/c/0.mp3", HASH_C), _make_file("/dir/c/0.cue", HASH_C, "cue")]
    files += [_make_file("/dir/d/0.mp3", HASH_D), _make_file("/dir/d/1.mp4", HASH_D, "mp4")]
    session.add_all(files)
    await session.flush()
    session.add_all([FileMetadata(file_id=f.id, artist=f"artist-{n}", bitrate=128_000 + n) for n, f in enumerate(files) if n % 2 == 0])
    resolved = files[5]
    session.add(DedupResolution(file_id=resolved.id, canonical_file_id=files[6].id))
    await session.flush()


def _rows_signature(rows: list[Any]) -> list[tuple[uuid.UUID, uuid.UUID | None]]:
    return [(file_record.id, metadata.id if metadata is not None else None) for file_record, metadata in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [1, 2, 3, 500])
@pytest.mark.parametrize(
    "hash_filter",
    [
        pytest.param(lambda: FileRecord.sha256_hash == HASH_A, id="single-hash"),
        pytest.param(lambda: FileRecord.sha256_hash.in_([HASH_A, HASH_B, HASH_C, HASH_D]), id="hash-set"),
        pytest.param(lambda: _previous_page_filter(100, 0), id="page"),
    ],
)
async def test_member_query_returns_exactly_what_the_previous_shape_returned(session: AsyncSession, cap: int, hash_filter: Callable[[], Any]) -> None:
    """Same rows, same order, same metadata pairing, at caps below, at and above every group's size."""
    await _seed_mixed_groups(session)

    before = _rows_signature((await session.execute(_previous_capped_members(hash_filter(), cap))).all())
    after = _rows_signature((await session.execute(dedup._select_capped_group_members(hash_filter(), cap))).all())

    assert after == before
    assert before, "the oracle must return rows, or equality proves nothing"


@pytest.mark.asyncio
@pytest.mark.parametrize(("limit", "offset"), [(100, 0), (1, 0), (1, 1), (2, 1), (1, 5)])
async def test_page_filter_selects_exactly_the_hashes_the_plain_subquery_selected(session: AsyncSession, limit: int, offset: int) -> None:
    """The MATERIALIZED CTE page picks the same hashes as the IN-subquery it replaced, page by page."""
    await _seed_mixed_groups(session)

    def page_hashes(page_filter: Any) -> Any:
        return select(FileRecord.sha256_hash).where(page_filter).distinct().order_by(FileRecord.sha256_hash)

    before = (await session.execute(page_hashes(_previous_page_filter(limit, offset)))).scalars().all()
    after = (await session.execute(page_hashes(dedup._dup_hash_page_filter(limit, offset)))).scalars().all()

    assert after == before


@pytest.mark.asyncio
async def test_readers_build_the_same_groups_as_the_previous_shape(session: AsyncSession) -> None:
    """End to end through the public readers: the group dicts match the previous shape's, member for member."""
    await _seed_mixed_groups(session)
    cap = dedup._MAX_GROUP_MEMBERS

    async def previous(hash_filter: Any) -> list[dict[str, Any]]:
        return dedup._build_metadata_groups((await session.execute(_previous_capped_members(hash_filter, cap))).all(), cap=cap)

    assert await dedup.find_duplicate_groups_with_metadata(session) == await previous(_previous_page_filter(dedup.GROUP_PAGE_SIZE, 0))
    hashes = [HASH_A, HASH_B, HASH_C, HASH_D]
    assert await dedup.find_duplicate_groups_by_hashes(session, hashes) == await previous(FileRecord.sha256_hash.in_(hashes))
    assert await dedup.find_duplicate_group_by_hash(session, HASH_A) == (await previous(FileRecord.sha256_hash == HASH_A))[0]

    groups = await dedup.find_duplicate_groups(session)
    assert [(g["sha256_hash"], [f["original_path"] for f in g["files"]]) for g in groups] == [
        (HASH_A, [f"/dir/a/{i}.mp3" for i in range(5)]),
        (HASH_B, ["/dir/b/1.flac", "/dir/b/2.flac"]),
        (HASH_D, ["/dir/d/0.mp3", "/dir/d/1.mp4"]),
    ]
