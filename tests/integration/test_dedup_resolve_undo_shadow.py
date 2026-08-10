"""Service-level dedup resolve/undo CAS + idempotency coverage (marker is the sole authority).

Phase 90 (PR-C, MIG-04): the state enum + ``files.state`` column are gone and the
``shadow_compare`` subsystem is retired, so this file no longer asserts
``run_shadow_compare(...).hard_fail_total == 0`` or a restored scalar ``dup`` state value. What
remains is the load-bearing, state-INDEPENDENT service coverage that PR-A's router-level
``tests/review/routers/test_duplicates.py`` does NOT duplicate: the ``resolve_group`` /
``undo_resolve`` CAS, the stale-replay no-op, and the malformed-payload branches. The
``DedupResolution`` marker is now the sole resolve/undo authority (D-05/D-09), so every assertion
below is expressed against the marker set (``_marker_file_ids``) and the ``undo_resolve`` RETURNING
count alone.

Stale-replay (D-06): re-resolve picks a DIFFERENT canonical, so the file the stale payload targets
becomes the keeper (no marker). The stale ``undo`` then matches 0 rows for that id and cannot clobber
the re-resolution -- the exact CAS the old unconditional per-file UPDATE lacked.

Real-PG ``db_session`` fixture (no SAQ dependency). Run via ``just test-bucket integration`` on port
5433 (export ``TEST_DATABASE_URL``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, false as sa_false, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.base import Base
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord
from phaze.services.dedup import resolve_group, undo_resolve
from tests.db_guard import integration_dsns, require_test_database


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.integration

# DSN pair + destructive-DB guard, shared with every other integration module via `tests.db_guard`.
BROKER_DSN, SA_DSN = integration_dsns()
_TARGET_DB = require_test_database(SA_DSN, context="dedup resolve/undo integration tests")

_LEGACY_AGENT_ID = "test-fileserver"
HASH_A = "a" * 64


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a real-PG ``AsyncSession`` with all ORM tables + the FK agent."""
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    engine = create_async_engine(SA_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        # Seed the FK-parent IDEMPOTENTLY: a committed ``test-fileserver`` may already exist (the
        # ``committed_db`` fixture re-seeds one, and the session-scoped ``async_engine`` seeds one), so a
        # blind INSERT collides on ``pk_agents`` under the full-bucket ordering (92-05, DI-92-04-02).
        # Get-or-insert satisfies the FK either way and keeps this hermetic fixture order-independent.
        if await session.get(Agent, _LEGACY_AGENT_ID) is None:
            session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
            await session.flush()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


async def _file(session: AsyncSession, *, sha256: str = HASH_A) -> FileRecord:
    fid = uuid.uuid4()
    rec = FileRecord(
        agent_id="test-fileserver",
        id=fid,
        sha256_hash=sha256,
        original_path=f"/media/{fid}.mp3",
        original_filename=f"{fid}.mp3",
        current_path=f"/media/{fid}.mp3",
        file_type="mp3",
        file_size=1234,
    )
    session.add(rec)
    await session.flush()
    return rec


async def _marker_file_ids(session: AsyncSession) -> set[uuid.UUID]:
    result = await session.execute(select(DedupResolution.file_id))
    return set(result.scalars().all())


# --------------------------------------------------------------------------------------------------
# Main cycle: resolve -> undo -> re-resolve, marker set correct throughout.
# --------------------------------------------------------------------------------------------------
async def test_resolve_undo_reresolve_marker_cycle(db_session: AsyncSession) -> None:
    keeper = await _file(db_session)
    dup = await _file(db_session)

    # resolve: marker inserted for the non-canonical dup (Phase 90 D-09: marker-only).
    _count, payload = await resolve_group(db_session, HASH_A, keeper.id)
    assert dup.id in await _marker_file_ids(db_session)

    # undo: marker DELETEd (the sole undo authority).
    restored = await undo_resolve(db_session, payload)
    assert restored == 1
    assert dup.id not in await _marker_file_ids(db_session)

    # re-resolve: marker re-inserted.
    _count2, _payload2 = await resolve_group(db_session, HASH_A, keeper.id)
    assert dup.id in await _marker_file_ids(db_session)


# --------------------------------------------------------------------------------------------------
# Stale-replay CAS (D-06): a browser payload replayed after a re-resolve with a DIFFERENT canonical
# targets a file that is now the keeper (no marker) -> DELETE matches 0 rows -> no clobber.
# --------------------------------------------------------------------------------------------------
async def test_stale_undo_replay_is_a_noop(db_session: AsyncSession) -> None:
    f_b = await _file(db_session)
    f_c = await _file(db_session)

    # (1) resolve with canonical=C -> marker on B; capture the stale payload P (targets B).
    _count, stale_payload = await resolve_group(db_session, HASH_A, f_c.id)
    assert {e["id"] for e in stale_payload} == {str(f_b.id)}

    # (2) undo P -> marker B gone (marker DELETE is the sole undo authority).
    await undo_resolve(db_session, stale_payload)
    assert f_b.id not in await _marker_file_ids(db_session)

    # (3) re-resolve with the OTHER canonical=B -> now C is the resolved duplicate (marker on C),
    #     B is the keeper (no marker).
    await resolve_group(db_session, HASH_A, f_b.id)
    assert await _marker_file_ids(db_session) == {f_c.id}

    # (4) STALE replay of P (targets B, now a keeper with no marker) -> CAS matches 0 rows -> no-op.
    restored = await undo_resolve(db_session, stale_payload)
    assert restored == 0  # nothing undone: B held no marker at replay time

    # The re-resolution is intact: C still marked, B untouched.
    assert await _marker_file_ids(db_session) == {f_c.id}


# --------------------------------------------------------------------------------------------------
# phaze-btix (ABA): a stale undo payload must not delete a NEWER marker written on the SAME file.
#
# ``test_stale_undo_replay_is_a_noop`` above targets a file that becomes a bare KEEPER (no marker at
# all) by replay time -- so its DELETE trivially matches nothing regardless of whether the CAS scoping
# is correct. It does not exercise the actual reported defect: the same non-canonical file can carry a
# DIFFERENT marker across two resolutions (different canonical_id each time), and file_id alone cannot
# tell those two markers apart. This test reproduces that exact sequence.
# --------------------------------------------------------------------------------------------------
async def test_stale_undo_replay_against_a_later_different_resolution_is_a_noop(db_session: AsyncSession) -> None:
    """A stale undo replay must not revert a LATER resolution's marker on the same file (phaze-btix)."""
    f_a = await _file(db_session)
    f_b = await _file(db_session)
    f_c = await _file(db_session)

    # (1) resolve with canonical=A -> markers on B and C, both canonical_file_id=A. Capture the stale
    #     payload P now, before it is ever undone.
    _count, stale_payload = await resolve_group(db_session, HASH_A, f_a.id)
    assert {e["id"] for e in stale_payload} == {str(f_b.id), str(f_c.id)}
    assert all(e["canonical_id"] == str(f_a.id) for e in stale_payload)

    # (2) undo P -> both markers deleted.
    undone = await undo_resolve(db_session, stale_payload)
    assert undone == 2
    assert await _marker_file_ids(db_session) == set()

    # (3) re-resolve with a DIFFERENT canonical=B -> markers on A and C, this time canonical_file_id=B.
    #     C's marker is now a genuinely DIFFERENT row (new marker id, new canonical_file_id) from the
    #     one P was minted against in step (1).
    await resolve_group(db_session, HASH_A, f_b.id)
    assert await _marker_file_ids(db_session) == {f_a.id, f_c.id}

    # (4) STALE replay of the ORIGINAL payload P (targets B and C, canonical=A). B holds no marker
    #     (a harmless no-op for that entry), but C DOES hold a marker -- just a NEWER one written by
    #     the re-resolution in step (3), with canonical=B, not A. The CAS must not match it.
    restored = await undo_resolve(db_session, stale_payload)
    assert restored == 0, "stale replay deleted a marker written by a later, different resolution"

    # The re-resolution is intact: A and C still marked (canonical=B), nothing clobbered.
    assert await _marker_file_ids(db_session) == {f_a.id, f_c.id}


# ---------------------------------------------------------------------------
# Regression: a malformed undo payload must never delete the wrong marker (code-review WR-01/WR-02).
# The DELETE + gate derive from the payload id-set alone (the PR-A blocker fix), so a corrupted
# previous_state still deletes the marker for the valid id -- and never raises.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_undo_with_invalid_previous_state_deletes_marker(db_session: AsyncSession) -> None:
    """An unknown previous_state NO LONGER gates the marker DELETE (id-set is the sole authority)."""
    keeper = await _file(db_session)
    dup = await _file(db_session)
    await db_session.flush()

    _count, payload = await resolve_group(db_session, HASH_A, keeper.id)
    assert dup.id in await _marker_file_ids(db_session)

    # Corrupt the state the browser echoes back. The marker DELETE is keyed on the id-set, not this value.
    poisoned = [{**entry, "previous_state": "not_a_real_state"} for entry in payload]
    restored = await undo_resolve(db_session, poisoned)

    assert restored == 1  # DELETE keyed on entry["id"], independent of the unparseable previous_state
    assert dup.id not in await _marker_file_ids(db_session)  # marker DELETED


@pytest.mark.asyncio
async def test_undo_with_malformed_uuid_does_not_raise(db_session: AsyncSession) -> None:
    """A non-UUID id is dropped, not propagated as a 500 -- and it cannot suppress valid entries."""
    keeper = await _file(db_session)
    dup = await _file(db_session)
    await db_session.flush()

    _count, payload = await resolve_group(db_session, HASH_A, keeper.id)

    # A garbage id, a missing-key entry, and the one real entry, all in the same payload.
    mixed = [{"id": "definitely-not-a-uuid"}, {}, *payload]
    restored = await undo_resolve(db_session, mixed)

    assert restored == 1  # only the real entry was restored; no exception escaped
    assert dup.id not in await _marker_file_ids(db_session)


@pytest.mark.asyncio
async def test_undo_duplicate_entries_do_not_inflate_count(db_session: AsyncSession) -> None:
    """The same file listed twice restores once -- the count is the marker DELETE's RETURNING cardinality."""
    keeper = await _file(db_session)
    dup = await _file(db_session)
    await db_session.flush()

    _count, payload = await resolve_group(db_session, HASH_A, keeper.id)
    restored = await undo_resolve(db_session, [*payload, *payload])

    assert restored == 1
    assert dup.id not in await _marker_file_ids(db_session)


# ---------------------------------------------------------------------------
# T-84-03-03 (threat model): a concurrent HTMX double-submit of resolve must be idempotent.
#
# `resolve_group` guards with `on_conflict_do_nothing(index_elements=["file_id"])`. The selection
# filters `~dedup_resolved_clause()`, so a *sequential* second POST never reaches the INSERT, and the
# conflict can only fire when a concurrent transaction's marker was invisible to our SELECT snapshot.
# Both cases are covered below.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_second_resolve_of_same_group_is_a_noop(db_session: AsyncSession) -> None:
    """Sequential double-submit: the second resolve selects nothing, inserts nothing, raises nothing."""
    keeper = await _file(db_session)
    dup = await _file(db_session)
    await db_session.flush()

    first_count, _payload = await resolve_group(db_session, HASH_A, keeper.id)
    assert first_count == 1
    assert await _marker_file_ids(db_session) == {dup.id}

    second_count, second_payload = await resolve_group(db_session, HASH_A, keeper.id)

    assert second_count == 0
    assert second_payload == []
    assert await _marker_file_ids(db_session) == {dup.id}  # still exactly one marker


@pytest.mark.asyncio
async def test_concurrent_double_submit_insert_conflict_is_a_noop(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent double-submit: a marker the SELECT could not see must not raise IntegrityError.

    Models the race exactly. Under real concurrency, transaction B's snapshot predates A's insert, so
    B's `~dedup_resolved_clause()` filter does not exclude the file and B attempts the INSERT anyway.
    Blinding the clause reproduces that deterministically against the *real* pg_insert statement.
    Removing `.on_conflict_do_nothing(...)` from `resolve_group` makes this raise IntegrityError.
    """
    keeper = await _file(db_session)
    dup = await _file(db_session)
    await db_session.flush()

    # Transaction A already inserted the marker (and committed, in the real race).
    db_session.add(DedupResolution(file_id=dup.id, canonical_file_id=keeper.id))
    await db_session.flush()

    # Transaction B's snapshot cannot see it -> its exclusion filter matches nothing.
    monkeypatch.setattr("phaze.services.dedup.dedup_resolved_clause", lambda: sa_false())

    count, _payload = await resolve_group(db_session, HASH_A, keeper.id)

    assert count == 1  # B believed it resolved the file...
    markers = await _marker_file_ids(db_session)
    assert markers == {dup.id}  # ...but first-writer-wins: still exactly one marker, no IntegrityError


@pytest.mark.asyncio
async def test_resolve_group_member_deleted_between_select_and_insert_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-a3if1: a group member deleted by a concurrent scan-batch delete AFTER resolve_group's own
    non-canonical SELECT has already returned it, but BEFORE the DedupResolution INSERT runs, must not
    raise IntegrityError out of resolve_group. `delete_scan_cascade`'s own comment documents the
    expected outcome as "the losing writer simply FK-fails against the already-deleted file" --
    request_guards.py contract rule 4 (catch the race, not the typo) makes that the router's/service's
    obligation, not an unhandled 500.

    Uses a REAL second connection to delete-and-commit `dup` mid-``resolve_group`` (wrapping the
    resolving session's own `execute` to fire the side effect right after the non-canonical SELECT
    returns, before the INSERT), so the INSERT genuinely FK-fails against Postgres -- not a mocked
    exception.

    Deliberately does NOT take the shared ``db_session`` fixture: it flushes but never commits its
    seed rows, so a genuinely independent second connection cannot see (or delete) `dup` at all --
    the exact reason ``test_concurrent_resolve_with_disjoint_canonicals_leaves_exactly_one_keeper``
    below provisions its own engine/schema/seed end-to-end instead.
    """
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    engine = create_async_engine(SA_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    keeper: FileRecord | None = None
    dup: FileRecord | None = None
    try:
        async with session_factory() as seed_session:
            if await seed_session.get(Agent, _LEGACY_AGENT_ID) is None:
                seed_session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
                await seed_session.flush()
            keeper = await _file(seed_session)
            dup = await _file(seed_session)
            await seed_session.commit()

        async with session_factory() as resolving_session:
            original_execute = resolving_session.execute
            call_count = 0

            async def patched_execute(*args, **kwargs):
                nonlocal call_count
                result = await original_execute(*args, **kwargs)
                call_count += 1
                if call_count == 3:
                    # Call 1: pg_advisory_xact_lock. Call 2: canonical membership SELECT. Call 3:
                    # the "files in this group except canonical" SELECT -- it has just returned
                    # `dup`. Delete it on a REAL second connection and commit, so resolve_group's
                    # own INSERT (call 4) below FK-fails against a row that genuinely no longer
                    # exists by the time it runs.
                    async with session_factory() as other:
                        await other.execute(delete(FileRecord).where(FileRecord.id == dup.id))  # type: ignore[union-attr]
                        await other.commit()
                return result

            monkeypatch.setattr(resolving_session, "execute", patched_execute)

            count, payload = await resolve_group(resolving_session, HASH_A, keeper.id)
            await resolving_session.commit()

            assert count == 0
            assert payload == []
            assert await _marker_file_ids(resolving_session) == set()  # the SAVEPOINT unwound the failed insert
            # The nested SAVEPOINT rollback (not a full session.rollback()) must leave the outer
            # transaction -- and the already-loaded `keeper` row -- usable for the rest of the
            # request (contract rule 5).
            assert await resolving_session.get(FileRecord, keeper.id) is not None
    finally:
        async with session_factory() as cleanup_session:
            if keeper is not None:
                await cleanup_session.execute(delete(DedupResolution).where(DedupResolution.file_id == keeper.id))
                await cleanup_session.execute(delete(FileRecord).where(FileRecord.id == keeper.id))
            if dup is not None:
                await cleanup_session.execute(delete(DedupResolution).where(DedupResolution.file_id == dup.id))
                await cleanup_session.execute(delete(FileRecord).where(FileRecord.id == dup.id))
            await cleanup_session.commit()
        await engine.dispose()


# ---------------------------------------------------------------------------
# phaze-v0iy: TRUE concurrency -- two independent connections/transactions resolving the SAME group
# with DIFFERENT keepers. This is the shape ``test_concurrent_double_submit_insert_conflict_is_a_noop``
# above does NOT cover: that test simulates one connection observing a stale snapshot of a marker
# some OTHER canonical_id already wrote. Here both requests pick genuinely disjoint canonicals (the
# worst-case 2-member group), so before the phaze-v0iy fix the two INSERTs never conflicted on
# (file_id) at all and BOTH committed -- stranding both keepers with zero survivor. Real Postgres,
# real second connection, real ``pg_advisory_xact_lock`` contention (not monkeypatched).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_resolve_with_disjoint_canonicals_leaves_exactly_one_keeper() -> None:
    """Two overlapping resolves of a 2-member group picking DIFFERENT keepers must not both succeed.

    Reproduces the exact reported interleaving: request A (canonical=x) is slow (holds its
    transaction open past its own insert); request B (canonical=y) starts while A is still in
    flight. Before the fix, A's insert set (``{y}``) and B's insert set (``{x}``) share no row, so
    ``ON CONFLICT (file_id) DO NOTHING`` never fires for either and both commit -- x and y end up
    pointing at each other, the group vanishes with zero surviving canonical. After the fix, B's
    ``pg_advisory_xact_lock`` call (the first statement ``resolve_group`` runs) blocks until A
    commits, then B's re-run membership check sees its own canonical (y) already marked and no-ops.

    Deliberately does NOT take the shared ``db_session`` fixture: that fixture flushes (but never
    commits) a seed ``Agent`` row and holds the transaction open for the whole test, which would
    block -- on Postgres, not merely serialize -- this test's own from-scratch engine trying to
    insert a row with the same primary key, for the entire test body. This test provisions its own
    engine/schema/seed data end-to-end instead, precisely so two INDEPENDENT connections are real.
    """
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    engine = create_async_engine(SA_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    file_x: FileRecord | None = None
    file_y: FileRecord | None = None
    try:
        async with session_factory() as seed_session:
            if await seed_session.get(Agent, _LEGACY_AGENT_ID) is None:
                seed_session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
                await seed_session.flush()
            file_x = await _file(seed_session)
            file_y = await _file(seed_session)
            await seed_session.commit()

        results: dict[str, int] = {}

        async def resolve_keep_x() -> None:
            """Request A: keeper=x. Slow -- holds its transaction (and the advisory lock) open."""
            async with session_factory() as session_a:
                count, _payload = await resolve_group(session_a, HASH_A, file_x.id)  # type: ignore[union-attr]
                # Widen the race window past the membership-check+insert, matching the reported
                # "first request is slow" interleaving -- the lock is held until this commit.
                await asyncio.sleep(0.2)
                await session_a.commit()
                results["keep_x"] = count

        async def resolve_keep_y() -> None:
            """Request B: keeper=y, started while A is still in flight."""
            await asyncio.sleep(0.05)  # let A acquire the advisory lock first
            async with session_factory() as session_b:
                count, _payload = await resolve_group(session_b, HASH_A, file_y.id)  # type: ignore[union-attr]
                await session_b.commit()
                results["keep_y"] = count

        await asyncio.gather(resolve_keep_x(), resolve_keep_y())

        # A ran first and won the lock: it marked y (keeping x) and committed normally.
        assert results["keep_x"] == 1
        # B was serialized behind A's commit; by the time it re-checked, its own canonical (y) was
        # already marked non-canonical by A -- a genuine no-op, NOT a second conflicting resolution.
        assert results["keep_y"] == 0

        async with session_factory() as verify_session:
            markers = await _marker_file_ids(verify_session)

        # Exactly one marker (on y) -- x survives as the sole live keeper. Before the fix this
        # assertion fails: markers == {file_x.id, file_y.id}, i.e. zero surviving canonical.
        assert {file_x.id, file_y.id} & markers == {file_y.id}
    finally:
        # This test commits real rows (needed for two GENUINELY independent transactions to
        # race) against the shared per-worktree test database, unlike every other test in this
        # module (which rolls back via the `db_session` fixture). Clean up explicitly so later
        # tests' `_marker_file_ids() == {...}` exact-equality assertions are not polluted by
        # this test's leftover marker/files (FK-ordered: marker row before its file rows).
        async with session_factory() as cleanup_session:
            if file_x is not None and file_y is not None:
                await cleanup_session.execute(delete(DedupResolution).where(DedupResolution.file_id.in_([file_x.id, file_y.id])))
                await cleanup_session.execute(delete(FileRecord).where(FileRecord.id.in_([file_x.id, file_y.id])))
                await cleanup_session.commit()
        await engine.dispose()


# ---------------------------------------------------------------------------
# T-84-03-02 branch coverage (Nyquist gap, validate-phase): the undo payload arrives from the browser,
# so every validation branch in `undo_resolve` is attacker-reachable.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_undo_accepts_uuid_typed_id(db_session: AsyncSession) -> None:
    """A payload whose `id` is a real UUID object (not a str) undoes normally (dedup.py:315)."""
    keeper = await _file(db_session)
    dup = await _file(db_session)
    await db_session.flush()

    _count, payload = await resolve_group(db_session, HASH_A, keeper.id)
    # The browser sends strings; internal callers may pass UUID objects. Both must work. phaze-btix:
    # the payload carries both id and canonical_id, both of which must accept UUID-typed values.
    uuid_typed = [{"id": uuid.UUID(entry["id"]), "canonical_id": uuid.UUID(entry["canonical_id"])} for entry in payload]

    restored = await undo_resolve(db_session, uuid_typed)

    assert restored == 1
    assert dup.id not in await _marker_file_ids(db_session)


@pytest.mark.asyncio
async def test_undo_with_null_previous_state_deletes_marker(db_session: AsyncSession) -> None:
    """A JSON `null` (or any non-str) previous_state STILL deletes the marker (id-set is the authority).

    Under the decoupled undo the DELETE is keyed on the payload id-set, so an unparseable/absent
    previous_state no longer gates it. A fresh group per case isolates the assertions (once deleted, a
    marker cannot be deleted again).
    """
    for i, bad_state in enumerate((None, 42, ["discovered"])):
        group_hash = f"{i:064d}"
        keeper = await _file(db_session, sha256=group_hash)
        dup = await _file(db_session, sha256=group_hash)
        await db_session.flush()

        _count, payload = await resolve_group(db_session, group_hash, keeper.id)
        assert dup.id in await _marker_file_ids(db_session)

        poisoned = [{**entry, "previous_state": bad_state} for entry in payload]
        assert await undo_resolve(db_session, poisoned) == 1  # id-set DELETE fires despite the bad previous_state
        assert dup.id not in await _marker_file_ids(db_session)  # marker DELETED
