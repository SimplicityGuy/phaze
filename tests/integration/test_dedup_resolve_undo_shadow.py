"""SC#3 proof way 1 (D-16.1): the resolve→undo→re-resolve cycle keeps the Phase-79 shadow gate green.

``services/shadow_compare.py:135`` asserts the HARD invariant ``state='duplicate_resolved' ⇒ a
dedup_resolution marker exists``. This phase's new writer (``resolve_group``) and CAS undo
(``undo_resolve``) must never introduce a divergence. This test drives the real service functions over
a synthetic real-PG corpus and asserts ``run_shadow_compare(session).hard_fail_total == 0`` after every
step of the cycle, plus a stale-tab replay case proving the D-06 CAS no-ops.

Design note: the duplicate-group files are seeded at ``DISCOVERED`` — the one FileState with NO shadow
invariant (intentionally vacuous, absent from the registry) — so the only invariant this corpus can
ever trip is the ``duplicate_resolved`` one the writer is responsible for. A file seeded at, say,
``analyzed`` would itself trip the (unrelated) ``analyzed ⇒ analysis row`` hard invariant and mask the
signal.

Stale-replay (D-06): re-resolve picks a DIFFERENT canonical, so the file the stale payload targets
becomes the keeper (no marker). The stale ``undo`` then matches 0 rows for that id and cannot clobber
the re-resolution — the exact CAS the old unconditional per-file UPDATE lacked.

Real-PG ``db_session`` fixture copied from ``tests/integration/test_shadow_compare.py:84-113`` (no SAQ
dependency). Run via ``just test-bucket integration`` on port 5433 (export ``TEST_DATABASE_URL``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import false as sa_false, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.base import Base
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord, FileState
from phaze.services.dedup import resolve_group, undo_resolve
from phaze.services.shadow_compare import run_shadow_compare


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.integration

BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")

_TARGET_DB = make_url(SA_DSN).database or ""
if not _TARGET_DB.endswith("_test"):
    pytest.skip(
        f"Refusing to run dedup shadow integration tests against non-test database {_TARGET_DB!r}; "
        "set TEST_DATABASE_URL to a *_test DSN (e.g. run `just test-db`).",
        allow_module_level=True,
    )

_LEGACY_AGENT_ID = "test-fileserver"
HASH_A = "a" * 64


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a real-PG ``AsyncSession`` with all ORM tables + the FK agent (copied from test_shadow_compare)."""
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
        session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
        await session.flush()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


async def _file(session: AsyncSession, *, sha256: str = HASH_A, state: str = FileState.DISCOVERED.value) -> FileRecord:
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
        state=state,
    )
    session.add(rec)
    await session.flush()
    return rec


async def _marker_file_ids(session: AsyncSession) -> set[uuid.UUID]:
    result = await session.execute(select(DedupResolution.file_id))
    return set(result.scalars().all())


# --------------------------------------------------------------------------------------------------
# Main cycle: resolve → undo → re-resolve, shadow gate green (hard_fail_total == 0) throughout.
# --------------------------------------------------------------------------------------------------
async def test_resolve_undo_reresolve_keeps_shadow_green(db_session: AsyncSession) -> None:
    keeper = await _file(db_session)
    dup = await _file(db_session)

    # resolve: marker inserted for the non-canonical dup (Phase 90 D-09: marker-only, no files.state write).
    _count, payload = await resolve_group(db_session, HASH_A, keeper.id)
    assert dup.id in await _marker_file_ids(db_session)
    assert (await run_shadow_compare(db_session)).hard_fail_total == 0

    # undo: marker DELETEd (the sole undo authority; no state restore).
    restored = await undo_resolve(db_session, payload)
    assert restored == 1
    assert dup.id not in await _marker_file_ids(db_session)
    assert (await run_shadow_compare(db_session)).hard_fail_total == 0

    # re-resolve: marker re-inserted.
    _count2, _payload2 = await resolve_group(db_session, HASH_A, keeper.id)
    assert dup.id in await _marker_file_ids(db_session)
    assert (await run_shadow_compare(db_session)).hard_fail_total == 0


# --------------------------------------------------------------------------------------------------
# Stale-replay CAS (D-06): a browser payload replayed after a re-resolve with a DIFFERENT canonical
# targets a file that is now the keeper (no marker) → DELETE matches 0 rows → no clobber. The
# re-resolution's marker + state survive, and the gate stays green.
# --------------------------------------------------------------------------------------------------
async def test_stale_undo_replay_is_a_noop(db_session: AsyncSession) -> None:
    f_b = await _file(db_session)
    f_c = await _file(db_session)

    # (1) resolve with canonical=C → marker on B; capture the stale payload P (targets B).
    _count, stale_payload = await resolve_group(db_session, HASH_A, f_c.id)
    assert {e["id"] for e in stale_payload} == {str(f_b.id)}

    # (2) undo P → marker B gone (Phase 90 D-09: marker DELETE is the sole undo authority; no state restore).
    await undo_resolve(db_session, stale_payload)
    assert f_b.id not in await _marker_file_ids(db_session)

    # (3) re-resolve with the OTHER canonical=B → now C is the resolved duplicate (marker on C),
    #     B is the keeper (no marker).
    await resolve_group(db_session, HASH_A, f_b.id)
    assert await _marker_file_ids(db_session) == {f_c.id}

    # (4) STALE replay of P (targets B, now a keeper with no marker) → CAS matches 0 rows → no-op.
    restored = await undo_resolve(db_session, stale_payload)
    assert restored == 0  # nothing undone: B held no marker at replay time

    # The re-resolution is intact: C still marked, B untouched, gate green.
    assert await _marker_file_ids(db_session) == {f_c.id}
    assert (await run_shadow_compare(db_session)).hard_fail_total == 0


# ---------------------------------------------------------------------------
# Regression: a malformed undo payload must never delete a marker (code-review WR-01/WR-02).
#
# `undo_resolve` deletes the marker and restores `FileRecord.state` from an attacker-controllable,
# browser-held payload. If validation ran AFTER the DELETE, an unusable `previous_state` would skip
# the restore while the marker was already gone -- leaving `state='duplicate_resolved'` with no
# marker, the exact HARD divergence at shadow_compare.py:135 that SC#3 must keep green.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_undo_with_invalid_previous_state_deletes_marker_and_neutralises_state(db_session: AsyncSession) -> None:
    """Phase 90 (PR-A, INVERTED contract): an unknown previous_state NO LONGER gates the marker DELETE.

    The DELETE + gate now derive from the payload id-set alone (the blocker fix), so a corrupted
    previous_state still deletes the marker for the valid id. The legacy state restore then neutralises
    to ``FileState.DISCOVERED`` (no parseable previous_state), so there is never a
    ``duplicate_resolved``-without-marker orphan and the shadow gate stays green.
    """
    keeper = await _file(db_session)
    dup = await _file(db_session)
    await db_session.flush()

    _count, payload = await resolve_group(db_session, HASH_A, keeper.id)
    assert dup.id in await _marker_file_ids(db_session)
    assert (await run_shadow_compare(db_session)).hard_fail_total == 0

    # Corrupt the state the browser echoes back. The marker DELETE is keyed on the id-set, not this value.
    poisoned = [{**entry, "previous_state": "not_a_real_state"} for entry in payload]
    restored = await undo_resolve(db_session, poisoned)

    assert restored == 1  # DELETE keyed on entry["id"], independent of the unparseable previous_state
    assert dup.id not in await _marker_file_ids(db_session)  # marker DELETED (decoupled from FileState)
    await db_session.refresh(dup)
    assert dup.state == FileState.DISCOVERED  # neutralised (no parseable previous_state), never left resolved
    assert (await run_shadow_compare(db_session)).hard_fail_total == 0  # no duplicate_resolved-without-marker orphan


@pytest.mark.asyncio
async def test_undo_with_malformed_uuid_does_not_raise(db_session: AsyncSession) -> None:
    """A non-UUID id is dropped, not propagated as a 500 — and it cannot suppress valid entries."""
    keeper = await _file(db_session)
    dup = await _file(db_session)
    await db_session.flush()

    _count, payload = await resolve_group(db_session, HASH_A, keeper.id)

    # A garbage id, a missing-key entry, and the one real entry, all in the same payload.
    mixed = [{"id": "definitely-not-a-uuid", "previous_state": FileState.DISCOVERED.value}, {"previous_state": FileState.DISCOVERED.value}, *payload]
    restored = await undo_resolve(db_session, mixed)

    assert restored == 1  # only the real entry was restored; no exception escaped
    assert dup.id not in await _marker_file_ids(db_session)
    await db_session.refresh(dup)
    assert dup.state == FileState.DISCOVERED
    assert (await run_shadow_compare(db_session)).hard_fail_total == 0


@pytest.mark.asyncio
async def test_undo_duplicate_entries_do_not_inflate_count(db_session: AsyncSession) -> None:
    """The same file listed twice restores once — the count is the marker DELETE's RETURNING cardinality."""
    keeper = await _file(db_session)
    dup = await _file(db_session)
    await db_session.flush()

    _count, payload = await resolve_group(db_session, HASH_A, keeper.id)
    restored = await undo_resolve(db_session, [*payload, *payload])

    assert restored == 1
    assert dup.id not in await _marker_file_ids(db_session)
    assert (await run_shadow_compare(db_session)).hard_fail_total == 0


# ---------------------------------------------------------------------------
# T-84-03-03 (threat model): a concurrent HTMX double-submit of resolve must be idempotent.
#
# `resolve_group` guards with `on_conflict_do_nothing(index_elements=["file_id"])`. Nothing tested it:
# the selection filters `~dedup_resolved_clause()`, so a *sequential* second POST never reaches the
# INSERT, and the conflict can only fire when a concurrent transaction's marker was invisible to our
# SELECT snapshot. Both cases are covered below.
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
    assert (await run_shadow_compare(db_session)).hard_fail_total == 0


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
    assert (await run_shadow_compare(db_session)).hard_fail_total == 0


# ---------------------------------------------------------------------------
# T-84-03-02 branch coverage (Nyquist gap, validate-phase): the undo payload arrives from the browser,
# so every validation branch in `undo_resolve` is attacker-reachable. Two were uncovered:
#   dedup.py:315 — `id` already a uuid.UUID rather than a str
#   dedup.py:325 — `previous_state` not a str at all (JSON `null` -> None)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_undo_accepts_uuid_typed_id(db_session: AsyncSession) -> None:
    """A payload whose `id` is a real UUID object (not a str) undoes normally (dedup.py:315)."""
    keeper = await _file(db_session)
    dup = await _file(db_session)
    await db_session.flush()

    _count, payload = await resolve_group(db_session, HASH_A, keeper.id)
    # The browser sends strings; internal callers may pass UUID objects. Both must work. Phase 90 (D-09):
    # the payload is id-only (no previous_state key).
    uuid_typed = [{"id": uuid.UUID(entry["id"])} for entry in payload]

    restored = await undo_resolve(db_session, uuid_typed)

    assert restored == 1
    assert dup.id not in await _marker_file_ids(db_session)
    assert (await run_shadow_compare(db_session)).hard_fail_total == 0


@pytest.mark.asyncio
async def test_undo_with_null_previous_state_deletes_marker_and_neutralises_state(db_session: AsyncSession) -> None:
    """Phase 90 (PR-A, INVERTED contract): a JSON `null` (or any non-str) previous_state STILL deletes the marker.

    Under the decoupled undo the DELETE is keyed on the payload id-set, so an unparseable/absent
    previous_state no longer gates it. For each malformed value the marker is deleted for the valid id and
    the state neutralises to ``FileState.DISCOVERED`` (the vacuous, invariant-free state), keeping the
    shadow gate green (no ``duplicate_resolved``-without-marker orphan). A fresh group per case isolates
    the assertions (once deleted, a marker cannot be deleted again).
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
        await db_session.refresh(dup)
        assert dup.state == FileState.DISCOVERED  # neutralised
        assert (await run_shadow_compare(db_session)).hard_fail_total == 0
