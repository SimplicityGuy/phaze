"""phaze-yhhy: the bulk tag-write advisory lock must survive per-file pooled-connection churn.

``bulk_write_no_discrepancies`` commits once per file (phaze-k7g6), and a pooled ``AsyncSession``
returns its DBAPI connection to the pool on every ``commit()``. A Postgres SESSION-scoped advisory
lock (``pg_try_advisory_lock``/``pg_advisory_unlock``) is bound to the physical connection, not to
any ORM transaction -- so acquiring on ``session`` and releasing on ``session`` again acquires and
releases on WHATEVER connection the pool hands back each time, which is a DIFFERENT physical
connection as soon as the pool holds more than one idle connection at the moment of the first
per-file commit. The release then silently no-ops and the lock leaks onto an idle pooled connection.

This is invisible under the suite's hermetic ``session``/``client`` fixtures: 92-03 pins every
session in a test to ONE shared connection (``tests/conftest.py::_db_connection``), so there is
never more than one physical connection for the lock to hide behind. Reproducing (and guarding
against) the leak needs REAL, independent pool connections -- the ``committed_db``-style real-PG
harness other advisory-lock regressions (``test_agent_push_concurrency.py``,
``test_agent_s3_concurrency.py``) already use, per plan 92-04.

Two tests:

* ``test_advisory_lock_leaks_when_acquired_and_released_on_a_churning_session`` reproduces the BUG
  MECHANISM directly (acquire/release both on one ``AsyncSession``, forced pool churn in between) --
  proof the harness can detect it, independent of the router.
* ``test_bulk_tagwrite_lock_survives_pooled_connection_churn`` exercises the ACTUAL FIX (
  ``routers.tags._bulk_tagwrite_lock_connection`` + the connection-scoped acquire/release helpers)
  under the identical churn and asserts the lock is fully released.

Pool priming: a fresh ``pool_size=2`` engine is FIFO-primed with two idle connections in a known
order (``c_a`` checked in before ``c_b``, so ``c_a`` is popped first on the next checkout -- see
``tests/conftest.py``'s own "QueuePool is FIFO" note and ``src/phaze/database.py``). With exactly
two physical connections, alternating checkouts land ``c_a, c_b, c_a, c_b, ...`` (empirically
verified via ``pg_backend_pid()``): checkout #1 (the acquire) is ``c_a``; two further churn
round-trips are checkouts #2/#3 (``c_b``/``c_a``); the release is checkout #4 -- ``c_b``, the
connection that never held the lock.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog
from phaze.routers import tags as tags_module
from phaze.routers.tags import (
    _BULK_TAGWRITE_LOCK_KEY,
    _acquire_bulk_tagwrite_lock,
    _bulk_tagwrite_lock_connection,
    _release_bulk_tagwrite_lock,
    router as tags_router,
)
from tests.db_guard import require_test_database, resolve_test_dsn


pytestmark = pytest.mark.integration

SA_DSN = resolve_test_dsn()

# phaze-yhhy: the exact catalog encoding of _BULK_TAGWRITE_LOCK_KEY (Postgres splits a bigint
# advisory-lock key into two int4 catalog columns: classid = high 32 bits, objid = low 32 bits).
# Verified: _BULK_TAGWRITE_LOCK_KEY >> 32 == 5269601, _BULK_TAGWRITE_LOCK_KEY & 0xFFFFFFFF == 2053469281.
_LOCK_CLASSID = _BULK_TAGWRITE_LOCK_KEY >> 32
_LOCK_OBJID = _BULK_TAGWRITE_LOCK_KEY & 0xFFFFFFFF

# phaze-ieqg: `pg_locks` is CLUSTER-wide, not database-scoped. Without the `database` predicate this
# count includes every OTHER worktree's seat holding the same application key against its own
# `phaze_<name>_test` database on the shared harness Postgres -- so `count == 1` reads 2 and
# `count == 0` reads 1, and the module fails with a leak that does not exist. Measured: two
# concurrent full suites, correctly isolated per phaze-fwo7, both red here on
# `assert 2 == 1`. Advisory locks taken with the single-argument `pg_advisory_lock(bigint)` are
# always database-scoped, so `pg_locks.database` is populated for them and this predicate is exact.
_ADVISORY_LOCK_COUNT_SQL = text(
    "select count(*) from pg_locks "
    "where locktype = 'advisory' and classid = :classid and objid = :objid "
    "and database = (select oid from pg_database where datname = current_database())"
)


async def _prime_two_connections_fifo(engine):  # type: ignore[no-untyped-def]
    """Open and release two connections in order, so the pool's idle queue is ``[c_a, c_b]``.

    The pool is FIFO (``use_lifo=False``, the default): the next checkout pops ``c_a``.
    """
    c_a = await engine.connect()
    c_b = await engine.connect()
    await c_a.close()
    await c_b.close()


async def _force_unlock_all(engine) -> None:  # type: ignore[no-untyped-def]
    """Best-effort cleanup: release every advisory lock this test's connections might have leaked
    (a genuinely-detected leak, or a mid-test failure), so a later test never inherits one.
    """
    async with engine.connect() as conn:
        await conn.execute(select(func.pg_advisory_unlock_all()))


@pytest.mark.asyncio
async def test_advisory_lock_leaks_when_acquired_and_released_on_a_churning_session() -> None:
    """Reproduces the bug mechanism directly: acquire/release both via ``session`` still leaks."""
    require_test_database(SA_DSN, context="tag-write advisory lock regression")
    engine = create_async_engine(SA_DSN, pool_size=2, max_overflow=0)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await _prime_two_connections_fifo(engine)

        async with session_factory() as session:
            # checkout #1 -> c_a: acquire the lock (mirrors the OLD `_acquire_bulk_tagwrite_lock(session)`).
            acquired = bool((await session.execute(select(func.pg_try_advisory_lock(_BULK_TAGWRITE_LOCK_KEY)))).scalar())
            assert acquired is True
            await session.commit()  # checkin c_a

            # Two churn round-trips -- checkouts #2, #3 alternate c_b, c_a (mirrors the per-file
            # `_has_terminal_tagwrite` selects + per-file `session.commit()`s the bulk loop runs
            # between the lock acquire and its `finally`-block release).
            for _ in range(2):
                await session.execute(select(1))
                await session.commit()

            # The release (OLD code: on `session` again) lands on checkout #4's connection -- c_b,
            # never held the lock -- a silent no-op.
            await session.execute(select(func.pg_advisory_unlock(_BULK_TAGWRITE_LOCK_KEY)))
            await session.commit()

        async with engine.connect() as verify_conn:
            count = (await verify_conn.execute(_ADVISORY_LOCK_COUNT_SQL, {"classid": _LOCK_CLASSID, "objid": _LOCK_OBJID})).scalar()
        assert count == 1, "acquiring and releasing on a churning session must leak the lock (proves the harness detects the bug)"
    finally:
        await _force_unlock_all(engine)
        await engine.dispose()


@pytest.mark.asyncio
async def test_bulk_tagwrite_lock_survives_pooled_connection_churn() -> None:
    """The FIX: acquire/release on a connection pinned by ``_bulk_tagwrite_lock_connection`` survives
    the identical churn -- ``select count(*) from pg_locks ...`` is 0 after the handler returns.
    """
    require_test_database(SA_DSN, context="tag-write advisory lock regression")
    engine = create_async_engine(SA_DSN, pool_size=2, max_overflow=0)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await _prime_two_connections_fifo(engine)

        async with session_factory() as session:
            lock_conn, owns_lock_conn = await _bulk_tagwrite_lock_connection(session)
            try:
                assert await _acquire_bulk_tagwrite_lock(lock_conn) is True
                for _ in range(3):
                    await session.execute(select(1))
                    await session.commit()
            finally:
                await _release_bulk_tagwrite_lock(lock_conn)
                if owns_lock_conn:
                    await lock_conn.close()

        async with engine.connect() as verify_conn:
            count = (await verify_conn.execute(_ADVISORY_LOCK_COUNT_SQL, {"classid": _LOCK_CLASSID, "objid": _LOCK_OBJID})).scalar()
        assert count == 0, "the pinned-connection release must clear the lock even after per-file session churn"

        # Belt-and-suspenders, catalog-encoding-independent check: a fresh connection can re-acquire.
        async with engine.connect() as reacquire_conn:
            reacquired = bool((await reacquire_conn.execute(select(func.pg_try_advisory_lock(_BULK_TAGWRITE_LOCK_KEY)))).scalar())
            assert reacquired is True, "a leaked lock would refuse this re-acquire"
            await reacquire_conn.execute(select(func.pg_advisory_unlock(_BULK_TAGWRITE_LOCK_KEY)))
    finally:
        await _force_unlock_all(engine)
        await engine.dispose()


async def _delete_seeded_files(engine, file_ids: list[uuid.UUID]) -> None:  # type: ignore[no-untyped-def]
    """Delete seeded ``FileRecord``s (and their children) for real, in FK-safe order.

    This module's tests commit through a REAL engine, independent of the suite's hermetic
    per-test savepoint sandbox (that sandbox is exactly what these tests must NOT use -- see the
    module docstring). Nothing rolls these commits back automatically, so a row left behind here
    persists in the shared test database for the rest of the pytest session and pollutes any LATER
    test that counts rows globally (e.g. ``_get_tag_stats``'s ``TagWriteLog`` tallies). None of the
    child tables declare ``ON DELETE CASCADE`` (``proposals``/``file_metadata``/``tag_write_log``
    all FK to ``files.id`` with the default RESTRICT), so children are deleted first.
    """
    if not file_ids:
        return
    async with engine.begin() as conn:
        await conn.execute(delete(TagWriteLog).where(TagWriteLog.file_id.in_(file_ids)))
        await conn.execute(delete(FileMetadata).where(FileMetadata.file_id.in_(file_ids)))
        await conn.execute(delete(RenameProposal).where(RenameProposal.file_id.in_(file_ids)))
        await conn.execute(delete(FileRecord).where(FileRecord.id.in_(file_ids)))


async def _seed_bulk_qualifying_file(session: AsyncSession, *, filename: str) -> uuid.UUID:
    """Seed one applied file with a real bulk-qualifying change (artist/title absent from metadata)."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/test/music/{uuid.uuid4().hex}/{filename}",
            original_filename=filename,
            current_path=f"/test/music/{filename}",
            file_type="mp3",
            file_size=1024,
        )
    )
    session.add(RenameProposal(id=uuid.uuid4(), file_id=file_id, proposed_filename=filename, status=ProposalStatus.EXECUTED.value))
    session.add(FileMetadata(id=uuid.uuid4(), file_id=file_id, artist=None, title=None, album="Keep Album"))
    await session.commit()
    return file_id


async def _review_tokens(session: AsyncSession, file_ids: list[uuid.UUID]) -> list[str]:
    tokens: list[str] = []
    for file_id in file_ids:
        file_record = await tags_module._get_file_with_metadata(session, file_id)
        assert file_record is not None
        tracklist = await tags_module._get_tracklist_for_file(session, file_id)
        link = await tags_module._get_accepted_discogs_link(session, file_id)
        proposed = tags_module.compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename, discogs_link=link)
        tokens.append(tags_module._encode_tag_review_token(tags_module._tag_review_payload(file_record, tracklist, link, proposed)))
    return tokens


@pytest.mark.asyncio
async def test_bulk_write_endpoint_releases_lock_over_a_real_pool(async_engine) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: the live ``/tags/bulk-write-no-discrepancies`` route over a real (small) pool
    releases its advisory lock even though several files each force a per-file commit/checkin cycle.

    Depends on the session-scoped ``async_engine`` fixture purely to guarantee the schema exists
    (it runs ``create_all`` once for the whole test session) -- this test's own ``engine`` is a
    SEPARATE, small-pool connection to the SAME database, matching the other cells in this module.
    """
    require_test_database(SA_DSN, context="tag-write advisory lock regression")
    engine = create_async_engine(SA_DSN, pool_size=3, max_overflow=0)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    file_ids: list[uuid.UUID] = []
    try:
        async with session_factory() as seed_session:
            # get-or-insert: async_engine's own session-scoped seed may already have committed this
            # FK parent (same physical database, a different SQLAlchemy Engine object).
            if await seed_session.get(Agent, "test-fileserver") is None:
                seed_session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
                await seed_session.commit()
            for i in range(4):
                # "Artist - Title.mp3" so parse_filename actually produces a change against the
                # seeded (artist=None, title=None) metadata -- a plain "bulk-lock-N.mp3" name parses
                # to no artist/title at all, so every candidate fell out as a zero-change NO_OP and
                # never reached the dispatch this test exists to exercise.
                file_ids.append(await _seed_bulk_qualifying_file(seed_session, filename=f"Bulk Artist {i} - Bulk Title.mp3"))
            review_tokens = await _review_tokens(seed_session, file_ids)

        app = FastAPI(title="smoke", version="test")
        app.include_router(tags_router)
        # phaze-6bkk: bulk_write_no_discrepancies dispatches each qualifying file via
        # ``request.app.state.task_router`` -- wire a fake one so the loop actually reaches
        # enqueue_tag_write (and its advisory-lock acquire/release) instead of every file being
        # silently swallowed as a per-file ``failed`` by a missing ``app.state.task_router``.
        fake_router = MagicMock()
        fake_router.enqueue_for_agent = AsyncMock()
        app.state.task_router = fake_router

        async def _get_session():  # type: ignore[no-untyped-def]
            async with session_factory() as s:
                yield s

        app.dependency_overrides[get_session] = _get_session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/tags/bulk-write-no-discrepancies", data={"review_tokens": review_tokens})
        assert resp.status_code == 200
        assert fake_router.enqueue_for_agent.await_count == len(file_ids), "every qualifying file must reach the dispatch"

        async with engine.connect() as verify_conn:
            count = (await verify_conn.execute(_ADVISORY_LOCK_COUNT_SQL, {"classid": _LOCK_CLASSID, "objid": _LOCK_OBJID})).scalar()
        assert count == 0, "the live endpoint must never leave the bulk-tagwrite advisory lock held"
    finally:
        await _delete_seeded_files(engine, file_ids)
        await _force_unlock_all(engine)
        await engine.dispose()


# phaze-ieqg / phaze-7bjjj: `pg_stat_activity` is CLUSTER-wide too, exactly like `pg_locks` above --
# scope by exact backend `pid` (unique cluster-wide) AND `datname = current_database()` so this can
# never read another concurrent worktree's backend by coincidence.
_BACKEND_STATE_SQL = text("select state from pg_stat_activity where pid = :pid and datname = current_database()")


@pytest.mark.asyncio
async def test_bulk_write_lock_connection_not_idle_in_transaction_once_the_loop_is_running() -> None:
    """phaze-7bjjj: ``lock_conn`` must leave its auto-begun transaction (commit) before the bounded
    per-file loop runs, so it does not sit idle-in-transaction for the whole scan -- holding back the
    backend_xmin vacuum horizon and exposed to ``idle_in_transaction_session_timeout``.
    ``pg_try_advisory_lock`` is SESSION-, not transaction-scoped (see the comment above
    ``_BULK_TAGWRITE_LOCK_KEY``), so committing right after acquire does not release it -- the lock
    is still held (and the loop still serialized) for the whole request.

    Drives the REAL ``/tags/bulk-write-no-discrepancies`` route over a real, dedicated connection
    (so ``_bulk_tagwrite_lock_connection`` takes the ``owns_lock_conn=True`` branch this fix
    targets -- the hermetic per-test fixture always takes the OTHER branch, see that function's own
    docstring) and captures ``lock_conn``'s backend pid right as the lock is acquired, then checks
    ``pg_stat_activity.state`` for that pid from a second connection once the per-file loop has
    actually started.
    """
    require_test_database(SA_DSN, context="tag-write advisory lock regression")
    engine = create_async_engine(SA_DSN, pool_size=3, max_overflow=0)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    file_ids: list[uuid.UUID] = []

    real_acquire = tags_module._acquire_bulk_tagwrite_lock
    captured_pid: list[int] = []

    async def _acquire_and_capture_pid(conn):  # type: ignore[no-untyped-def]
        acquired = await real_acquire(conn)
        captured_pid.append((await conn.execute(select(func.pg_backend_pid()))).scalar())
        return acquired

    real_has_terminal = tags_module._has_terminal_tagwrite
    checked = {"done": False}

    async def _has_terminal_and_check(inner_session, file_id):  # type: ignore[no-untyped-def]
        if not checked["done"]:
            checked["done"] = True
            assert captured_pid, "lock_conn's backend pid must be captured before the per-file loop starts"
            async with engine.connect() as verify_conn:
                state = (await verify_conn.execute(_BACKEND_STATE_SQL, {"pid": captured_pid[0]})).scalar()
            assert state != "idle in transaction", "lock_conn must not be idle-in-transaction once the per-file loop is running"
        return await real_has_terminal(inner_session, file_id)

    try:
        async with session_factory() as seed_session:
            if await seed_session.get(Agent, "test-fileserver") is None:
                seed_session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
                await seed_session.commit()
            file_ids.append(await _seed_bulk_qualifying_file(seed_session, filename="Idle Lock Artist - Idle Lock Title.mp3"))
            review_tokens = await _review_tokens(seed_session, file_ids)

        app = FastAPI(title="smoke", version="test")
        app.include_router(tags_router)
        fake_router = MagicMock()
        fake_router.enqueue_for_agent = AsyncMock()
        app.state.task_router = fake_router

        async def _get_session():  # type: ignore[no-untyped-def]
            async with session_factory() as s:
                yield s

        app.dependency_overrides[get_session] = _get_session

        with (
            patch.object(tags_module, "_acquire_bulk_tagwrite_lock", new=_acquire_and_capture_pid),
            patch.object(tags_module, "_has_terminal_tagwrite", new=_has_terminal_and_check),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/tags/bulk-write-no-discrepancies", data={"review_tokens": review_tokens})
        assert resp.status_code == 200
        assert checked["done"], "the per-file loop must have run for the idle-in-transaction assertion to have executed"

        async with engine.connect() as verify_conn:
            count = (await verify_conn.execute(_ADVISORY_LOCK_COUNT_SQL, {"classid": _LOCK_CLASSID, "objid": _LOCK_OBJID})).scalar()
        assert count == 0, "the commit-after-acquire must not leak the session-scoped advisory lock"
    finally:
        await _delete_seeded_files(engine, file_ids)
        await _force_unlock_all(engine)
        await engine.dispose()
