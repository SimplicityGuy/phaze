"""Shared test fixtures for Phaze test suite."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
import hashlib
import itertools
import secrets
from typing import Any
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from phaze.database import get_session
from phaze.main import create_app
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.base import Base
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from tests._background_drain import leaked_background_tasks_message, pending_router_background_tasks
from tests._queue_fakes import install_fake_queues
from tests.db_guard import (
    SharedTestDatabaseError,
    acquire_exclusive_session_lock,
    coerce_async_dsn,
    database_name,
    release_exclusive_session_lock,
    resolve_test_dsn,
)


# Re-exported under its historical private name: `tests/shared/test_conftest_dsn_coercion.py`
# imports `_coerce_async_dsn` from here. The implementation now lives in `tests.db_guard`, the
# single place that resolves and validates the test target.
_coerce_async_dsn = coerce_async_dsn

# Defaults to the 5433 ephemeral harness, NOT 5432 -- the justfile reserves 5432 for the
# developer's own database, and these fixtures create and drop schema. See `tests/db_guard.py`.
TEST_DATABASE_URL = resolve_test_dsn()

DB_FIXTURES = {"async_engine", "_db_connection", "session", "verify", "client", "authenticated_client", "seed_test_agent"}


@pytest.fixture(autouse=True)
def _isolate_pydantic_settings_from_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sever every BaseSettings class's `.env` file loading for the test session.

    pydantic-settings reads `.env` (relative to cwd) into every `BaseSettings()`
    constructor. The project's `.env` in docker-compose mode pins runtime
    overrides like `PHAZE_WATCHER_POLLING_MODE=true` and
    `PHAZE_WATCHER_SETTLE_SECONDS=3`, which silently change which code path
    tests exercise — defaults assertions fail, tests that mock only the native
    `Observer` end up hitting `PollingObserver` and crashing on missing
    `/data/music`.

    The fix: point `env_file` at an empty tempfile for every Settings subclass
    we own, for every test. ``monkeypatch.setattr`` on the class-level
    ``model_config`` (a TypedDict) is enough — pydantic-settings reads it at
    construction time. Tests can still ``monkeypatch.setenv(...)`` to inject
    specific values; ``os.environ`` continues to take precedence over the
    (now-empty) env_file.
    """
    from phaze.config import AgentSettings, ControlSettings, get_settings

    for cls in (ControlSettings, AgentSettings):
        new_config = dict(cls.model_config)
        new_config["env_file"] = None
        monkeypatch.setattr(cls, "model_config", new_config)

    # `get_settings()` is `@lru_cache(maxsize=1)`: the first call in the process caches a
    # role-specific singleton (ControlSettings vs AgentSettings, selected by `PHAZE_ROLE`).
    # A test that constructs the agent role — e.g. importing `agent_worker`, which builds a
    # Queue at import time under `PHAZE_ROLE=agent` — poisons that singleton for every LATER
    # test, because `monkeypatch` reverts the env but never clears the cache. Downstream
    # control-plane code (`cloud_staging` casts `get_settings()` to `ControlSettings`) then
    # reads the leaked `AgentSettings` and `AttributeError`s on ControlSettings-only fields
    # (e.g. `s3_multipart_part_size_bytes`). This was latent while the suite ran as one process
    # (collection order happened to hide it) and surfaced once the suite was partitioned into
    # per-bucket CI jobs. Clearing the cache per test makes settings resolution always reflect
    # the test's own env, never a leaked singleton.
    get_settings.cache_clear()
    # Also clear non-infrastructure env vars that the project's docker .env
    # defines, so the OS env layer cannot leak into tests. We deliberately
    # leave DATABASE_URL and REDIS_URL alone — integration-test fixtures
    # depend on them being set to the test-DB connection string. The vars
    # cleared here are all "feature toggle" / "tuning knob" overrides whose
    # tests assert against documented defaults.
    for var in (
        "MODELS_PATH",
        "SCAN_PATH",
        "DEBUG",
        "PHAZE_AUTO_MIGRATE",
        "PHAZE_DEV_SEED_AGENT",
        "PHAZE_DEV_AGENT_TOKEN",
        "PHAZE_AGENT_API_URL",
        "PHAZE_AGENT_TOKEN",
        "PHAZE_AGENT_SCAN_ROOTS",
        "PHAZE_ROLE",
        "PHAZE_WATCHER_SETTLE_SECONDS",
        "PHAZE_WATCHER_MAX_PENDING_SECONDS",
        "PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS",
        "PHAZE_WATCHER_POLLING_MODE",
        "PHAZE_SCAN_CHUNK_SIZE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _route_structlog_through_stdlib() -> "AsyncGenerator[None]":  # type: ignore[misc]
    """Configure structlog for the stdlib bridge per test, then reset (PR3 observability).

    Production entry points call ``configure_logging()`` exactly once per OS process.
    Unit tests do not boot an entry point, so without this the module-level
    ``structlog.get_logger(__name__)`` loggers fall back to structlog's DEFAULT
    ``PrintLoggerFactory`` -- which writes straight to stdout and bypasses stdlib
    ``logging`` entirely. That breaks every ``caplog``-based assertion (caplog hooks
    stdlib logging and would capture nothing).

    Configuring here routes structlog through ``LoggerFactory`` + ``ProcessorFormatter``
    so records propagate to the stdlib root logger and ``caplog`` captures them again,
    with ``PositionalArgumentsFormatter`` interpolating legacy ``%s`` calls. Level is
    ``DEBUG`` so DEBUG-level assertions work; ``json_logs=False`` keeps console output.
    The teardown calls ``structlog.reset_defaults()`` and clears root handlers so a
    ``configure_logging()`` call inside code-under-test (entry-point startups) cannot
    leak global logging state into the next test.
    """
    import logging

    import structlog

    from phaze.logging_config import configure_logging

    configure_logging(level="DEBUG", json_logs=False)
    yield
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)


# Stash key for the connection holding this session's exclusive lock on the test database.
# Parked on `config` (not a module global) so it travels with the pytest session that owns it.
_SESSION_LOCK_ATTR = "_phaze_test_db_session_lock"


def pytest_sessionstart(session: pytest.Session) -> None:
    """Claim the test database for THIS pytest process, or refuse to run (phaze-ieqg).

    ``TEST_DATABASE_URL`` isolates a worktree; it never isolated a *process*. Two pytest
    processes resolving one DSN wreck each other -- ``async_engine``'s ``drop_all`` at the
    first-finishing session's teardown deletes the schema the other is still using -- and the
    victim's output is a large, branch-unrelated failure set that passes on isolated re-run.
    That signature was misdiagnosed as cross-worktree contention for two dispatch rounds.

    Refusing here (before collection, before a single test runs) turns hours of false-red
    triage into one line naming the process that already owns the database. If Postgres is not
    reachable the lock is simply not taken -- see ``acquire_exclusive_session_lock``.

    ``--collect-only`` is exempt: it imports modules and never opens the schema, so it cannot
    corrupt a live run, and it is exactly the command someone reaches for to inspect the suite
    WHILE it is running (CI itself uses `pytest tests/<bucket> -m integration --collect-only` to
    decide bucket parallelism). Refusing it would buy no safety and cost the one non-destructive
    introspection tool available mid-run.
    """
    if session.config.option.collectonly:
        setattr(session.config, _SESSION_LOCK_ATTR, None)
        return
    try:
        lock = acquire_exclusive_session_lock(TEST_DATABASE_URL)
    except SharedTestDatabaseError as exc:
        # `pytest.exit` rather than a raised exception: this is a harness precondition, not a
        # bug in the suite, and it should read as a refusal instead of an INTERNALERROR dump.
        pytest.exit(str(exc), returncode=pytest.ExitCode.USAGE_ERROR)
    setattr(session.config, _SESSION_LOCK_ATTR, lock)


def pytest_sessionfinish(session: pytest.Session, exitstatus: object) -> None:
    """Release the test-database lock so the next run (or another worktree) can claim it."""
    release_exclusive_session_lock(getattr(session.config, _SESSION_LOCK_ATTR, None))
    setattr(session.config, _SESSION_LOCK_ATTR, None)


def pytest_report_header(config: pytest.Config) -> str:
    """State the resolved test database in the pytest header, on every run.

    Silent misconfiguration was only possible because nothing in the default output said which
    database the suite had targeted. Printing it unconditionally means a wrong target is visible
    in any captured log, including a passing one.

    The exclusivity state is printed on the same line for the same reason: ``unlocked`` means
    ``pytest_sessionstart`` could not reach Postgres, so this run is NOT protected against a
    second pytest process sharing the database -- worth seeing in a captured log before someone
    spends a round triaging its failure set.
    """
    url = make_url(TEST_DATABASE_URL)
    held = getattr(config, _SESSION_LOCK_ATTR, None) is not None
    exclusivity = "exclusive" if held else "unlocked (Postgres unreachable or bypass set)"
    return f"phaze test database: {database_name(TEST_DATABASE_URL)!r} on {url.host}:{url.port} (from TEST_DATABASE_URL, {exclusivity})"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark tests that require external services as integration tests.

    Three triggers, because the migration + queue suites reach Postgres three ways:
      * any test consuming a DB-backed fixture (``DB_FIXTURES``),
      * every test under ``tests/test_migrations/`` -- those run Alembic against a
        live Postgres, some via the ``migrated_engine`` fixture and some by building
        their own engine against ``MIGRATIONS_TEST_DATABASE_URL`` inline, and
      * every test under ``tests/integration/`` (Phase 36) -- those open a real
        ``PostgresQueue`` against the ephemeral test-db broker (port 5433) and never
        consume a DB-backed fixture, so the fixture trigger alone would miss them.

    Without the path rule the direct-engine migration tests and the real-PG queue
    integration tests escape the marker and break ``pytest -m 'not integration'``
    when no database is running.
    """
    for item in items:
        path_parts = item.path.parts
        if DB_FIXTURES & set(getattr(item, "fixturenames", ())) or "test_migrations" in path_parts or "integration" in path_parts:
            item.add_marker(pytest.mark.integration)


# ---------------------------------------------------------------------------
# Phase 67 (Plan 02): backend-registry TOML fixture.
#
# Writes a tmp backends.toml + points PHAZE_BACKENDS_CONFIG_FILE at it so a
# ControlSettings() construction resolves the given registry via the Idiom-B
# tomllib loader (config.py). Wave-3 consumers reuse this to construct a control
# plane with a chosen registry. Yields a `write(toml_text) -> Path` callable so
# each test supplies its own [[backends]]/[[buckets]] TOML; the get_settings
# lru_cache is cleared before AND after so a cached singleton never leaks.
# ---------------------------------------------------------------------------


@pytest.fixture
def backends_toml_env(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """Return a callable that writes a backends.toml and points the env pointer at it."""
    import textwrap

    from phaze.config import get_settings

    def _write(toml_text: str):  # type: ignore[no-untyped-def]
        path = tmp_path / "backends.toml"
        path.write_text(textwrap.dedent(toml_text), encoding="utf-8")
        monkeypatch.setenv("PHAZE_BACKENDS_CONFIG_FILE", str(path))
        get_settings.cache_clear()
        return path

    get_settings.cache_clear()
    yield _write
    get_settings.cache_clear()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def async_engine():  # type: ignore[no-untyped-def]
    """Session-scoped engine: create the schema ONCE, seed the FK-parent fileserver ONCE (D-06/D-07).

    CLEAN-02 (92-03): the former function-scoped ``create_all``/``drop_all``-per-test fixture that
    committed a ``test-fileserver`` seed EVERY test was the shared root of the 83-01/83-03 flake class
    -- a committed row that survived a failed teardown collided on ``pk_agents`` in the next test. The
    fix (D-06 global) is to make the suite hermetic BY CONSTRUCTION: build the schema exactly once for
    the whole session and let each test run inside a per-test outer transaction that is ROLLED BACK at
    teardown (see :func:`session`), so no committed row ever survives.

    The stable ``kind='fileserver'`` Agent row (``test-fileserver``) is seeded ONCE here, committed for
    real OUTSIDE any per-test transaction, so ``make_file``'s hardcoded ``agent_id="test-fileserver"``
    FK target (ON DELETE RESTRICT) survives across every test without being re-seeded (Phase 89
    LEGACY-03 dropped the model-level ``agent_id`` default, so the explicit FK target must exist).

    ``poolclass=NullPool``: pytest-asyncio (asyncio_mode=auto) runs each TEST in its own function-scoped
    event loop while this engine lives in the session loop. An asyncpg connection binds to the loop that
    created it, so a POOLED connection would raise "attached to a different loop" when a later test's
    ``_db_connection`` checkout reuses it. NullPool opens a FRESH connection per ``connect()`` (in the
    caller's own loop) and closes it on release -- no cross-loop reuse -- which is exactly what the
    per-test ``_db_connection`` fixture needs.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session_factory() as setup_session:
        # Seed the FK-parent fileserver IDEMPOTENTLY. In the ``integration`` bucket the ``committed_db``
        # fixture (tests/integration/conftest.py) re-seeds a COMMITTED ``test-fileserver`` at teardown by
        # design (92-04, commit 46d554c4) so later hermetic ``make_file`` calls keep their FK parent. If a
        # ``committed_db`` test runs BEFORE this session-scoped fixture is first instantiated, that row
        # already exists and a blind INSERT collides on ``pk_agents`` -- which errored EVERY hermetic
        # (`session`/`client`) test in the bucket once 92-05 shifted collection order. Get-or-insert makes
        # the seed order-independent: reuse the committed parent when present, else create it. In every
        # other (DB-free-of-committed_db) bucket the row never pre-exists, so behaviour is unchanged.
        if await setup_session.get(Agent, "test-fileserver") is None:
            setup_session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
            await setup_session.commit()
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def _db_connection(async_engine) -> AsyncGenerator[AsyncConnection]:  # type: ignore[no-untyped-def]
    """Yield ONE per-test connection -- the single connection every session in the test binds to (D-07).

    The ``create_savepoint`` hermeticity contract (RESEARCH CRITICAL wiring corollary) holds only if the
    test's :func:`session`, the app's ``get_session`` override, the shared :func:`verify` read session,
    AND the routed production fan-out (92-03 Task 2) ALL bind to this ONE connection -- a session on a
    different pool connection would be in a different transaction and, because the per-test outer
    transaction is never committed, would read ZERO/STALE under read-committed isolation.
    """
    async with async_engine.connect() as conn:
        _watch_savepoint_order(conn)
        yield conn


# --------------------------------------------------------------------------------------------------
# phaze-5lq8a: the savepoint-ordering probe.
#
# Sharing ONE connection between the test's `session`, the app's `get_session` override, the `verify`
# read session and the production fan-out is what makes uncommitted rows mutually visible (D-07). The
# price is that all of them push onto ONE Postgres savepoint stack, and `RELEASE SAVEPOINT s` destroys
# `s` AND EVERY SAVEPOINT ESTABLISHED AFTER IT. So the arrangement is sound only while those sessions
# nest strictly LIFO, and nothing in the wiring enforces that.
#
# When a router background task outlives the test body it breaks LIFO invisibly: it opens savepoint N,
# `verify`'s first read opens N+1, the task commits and RELEASEs N -- taking N+1 with it -- and the
# only symptom is `InvalidSavepointSpecificationError: savepoint "sa_savepoint_N+1" does not exist`
# raised from a FIXTURE FINALIZER, long after the test body passed. The run then reports
# "1 passed ... 1 error" and exits 1.
#
# This probe watches the stack directly, so the diagnosis is available whether or not the offending
# task is still alive at teardown -- which the `pending_router_background_tasks()` check alone cannot
# do, because the common case is a task that finished between corrupting the stack and teardown.
# --------------------------------------------------------------------------------------------------
_SAVEPOINT_ORDER_KEY = "phaze_savepoint_order"


def _watch_savepoint_order(conn: AsyncConnection) -> None:
    """Record every out-of-order savepoint release on this connection, for the finalizers to report."""
    sync_conn = conn.sync_connection
    state: dict[str, list[str]] = {"stack": [], "violations": []}
    sync_conn.info[_SAVEPOINT_ORDER_KEY] = state
    # `Connection._savepoint_impl` dispatches `savepoint` BEFORE it mints the name, so the event
    # hands us ``None`` and the name has to be mirrored from the same per-connection counter it
    # uses (`sa_savepoint_1`, `sa_savepoint_2`, ...). Both start at zero because this probe is
    # installed the moment the connection is checked out. The release/rollback events, which DO
    # carry the real name, are what keeps that mirror honest: a name we never minted is simply
    # ignored below rather than mistaken for a violation.
    counter = itertools.count(1)

    def _destroyed(name: str) -> None:
        """``name`` is going away; in Postgres so is everything established above it."""
        stack = state["stack"]
        if name not in stack:
            return
        index = stack.index(name)
        collateral = stack[index + 1 :]
        if collateral:
            state["violations"].append(f"{name} was released with {', '.join(collateral)} still established above it")
        del stack[index:]

    # `savepoint` is (conn, name); `release_savepoint` / `rollback_savepoint` are (conn, name, context).
    @event.listens_for(sync_conn, "savepoint")
    def _pushed(conn_: Any, name: str | None) -> None:
        state["stack"].append(name or f"sa_savepoint_{next(counter)}")

    @event.listens_for(sync_conn, "release_savepoint")
    def _released(conn_: Any, name: str, context: Any) -> None:
        _destroyed(name)

    @event.listens_for(sync_conn, "rollback_savepoint")
    def _rolled_back(conn_: Any, name: str, context: Any) -> None:
        _destroyed(name)


def _savepoint_order_violations(conn: AsyncConnection) -> list[str]:
    state = conn.sync_connection.info.get(_SAVEPOINT_ORDER_KEY)
    return list(state["violations"]) if state else []


async def _finalize_shared_connection_session(s: AsyncSession, leaked: list[asyncio.Task[Any]], violations: list[str], rollback: Any = None) -> None:
    """Close a session bound to the shared connection, then report what corrupted it (phaze-5lq8a).

    ``rollback`` is :func:`session`'s outer-transaction rollback and is omitted by :func:`verify`,
    which owns no outer transaction.

    THE ORDERING IS THE POINT, and it is why this is a function rather than four inline lines.
    ``s.close()`` is exactly what raises when another session on the connection released a savepoint
    out of order, and the previous ``await s.close()`` / ``await outer.rollback()`` pair ran them
    SEQUENTIALLY -- so a raising close skipped the rollback entirely and the per-test outer
    transaction was left open on a connection about to be handed back. The hermeticity contract
    (D-06/D-07) says that rollback ALWAYS runs; `finally` is what makes that true.

    Raising the diagnosis from the same ``finally`` is deliberate too: Python keeps the close's
    exception as this one's ``__context__``, so the asyncpg evidence is preserved under a headline
    that names the cause. Nothing is swallowed.
    """
    try:
        await s.close()
    finally:
        if rollback is not None:
            await rollback()
        if leaked or violations:
            raise AssertionError(_shared_connection_failure_message(leaked, violations))


def _shared_connection_failure_message(leaked: list[asyncio.Task[Any]], violations: list[str]) -> str:
    """Name the cause at the fixture that is about to fail because of it (phaze-5lq8a).

    Without this the whole class reads as a bare
    ``InvalidSavepointSpecificationError: savepoint "sa_savepoint_N" does not exist`` raised from a
    fixture the test never mentions, AFTER the test body passed -- which is how it survived on main
    long enough to be attributed to a reused database, an event loop, and two unrelated epics.
    """
    parts: list[str] = []
    if violations:
        parts.append(
            "savepoints on the shared per-test connection were released OUT OF ORDER: "
            + "; ".join(violations)
            + ". In Postgres a RELEASE destroys every savepoint established above it, so the session that "
            "owned those is now holding a savepoint the server has already discarded -- its close() raises "
            "'InvalidSavepointSpecificationError: savepoint \"sa_savepoint_N\" does not exist' at teardown."
        )
    if leaked:
        parts.append(leaked_background_tasks_message(leaked))
    elif violations:
        parts.append(
            "No router background task was still running at teardown, so the offender finished between "
            "corrupting the stack and this point -- the usual shape. Look for a request that detaches work "
            "(`asyncio.create_task`) with no following "
            "`await tests._background_drain.drain_router_background_tasks()`."
        )
    return " ".join(parts)


@pytest_asyncio.fixture
async def _route_stats_fanout(_db_connection: AsyncConnection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route ``get_stage_progress``'s PRODUCTION fan-out sessions through the per-test connection (92-03 Task 2).

    THE FAILURE CLASS THIS FIXES: after 92-02, ``get_stage_progress`` no longer reads via the passed-in
    ``session``; it opens its OWN per-task sessions from the module-level ``phaze.database.async_session``
    (the production engine, its own pool connection). Under the ``create_savepoint`` fixture, seeded rows
    live ONLY in the UNCOMMITTED per-test outer transaction on ``_db_connection``; a fan-out session on a
    DIFFERENT pool connection is a different transaction -> read-committed -> it reads ZERO/STALE. That
    breaks every seed-then-read test (``tests/analyze/core/test_stage_progress.py`` + the
    ``tests/shared/routers/test_pipeline.py`` seed-then-``GET /pipeline/stats`` cases).

    FIX (RESEARCH option a -- smallest blast radius): monkeypatch the production sessionmaker so its
    sessions bind to the SAME per-test connection. ``_read_in_own_session`` resolves ``async_session`` via
    a DEFERRED ``from phaze.database import async_session`` at CALL time (the agent-worker import-boundary
    convention), so patching the SOURCE attribute ``phaze.database.async_session`` is picked up by every
    fan-out read with no ``pipeline.py`` edit. ``_STATS_FANOUT = asyncio.Semaphore(1)`` serializes the
    fan-out onto the ONE shared asyncpg connection (a single connection cannot run concurrent statements),
    preserving VISIBILITY of the per-test savepoint state while avoiding the concurrency collision.
    Production is UNCHANGED: the module default ``_STATS_FANOUT`` stays ``None`` (fresh ``Semaphore(4)``
    per poll, one pool connection per task) outside the test run; ``monkeypatch`` reverts both per test.

    Scoped to DB-fixture tests via the fixture chain (``session`` depends on this) -- deliberately NOT
    ``autouse=True`` global, which would force ``_db_connection`` (and a DB) for DB-free tests.
    """
    fanout_factory = async_sessionmaker(bind=_db_connection, class_=AsyncSession, join_transaction_mode="create_savepoint", expire_on_commit=False)
    monkeypatch.setattr("phaze.database.async_session", fanout_factory)
    monkeypatch.setattr("phaze.services.pipeline.stages._STATS_FANOUT", asyncio.Semaphore(1))


@pytest_asyncio.fixture
async def session(_db_connection: AsyncConnection, _route_stats_fanout: None) -> AsyncGenerator[AsyncSession]:
    """Yield the per-test session: an outer transaction + ``create_savepoint`` join mode (D-07).

    Begins a per-test OUTER transaction on the shared ``_db_connection`` and constructs an
    ``AsyncSession(bind=_db_connection, join_transaction_mode="create_savepoint")``. In this SQLAlchemy
    2.0 built-in mode the Session's root is always a SAVEPOINT nested inside the outer transaction, so
    when a mutating router calls ``await session.commit()`` (the ``get_session``-never-commits invariant
    -- ``project_get_session_never_commits``) the SAVEPOINT is released and a new one opened: the rows
    become visible WITHIN the outer transaction to any sibling session bound to the same connection, yet
    nothing is durably committed. Teardown ``await outer.rollback()`` discards ALL in-test commits
    (including the savepoint-release commits), so nothing survives into the next test -- no ``pk_agents``
    collision, hermetic by construction. Do NOT hand-roll the ``after_transaction_end`` listener:
    ``create_savepoint`` IS that recipe (RESEARCH / docs.sqlalchemy.org session_transaction.html).
    """
    outer = await _db_connection.begin()
    s = AsyncSession(bind=_db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield s
    finally:
        # phaze-5lq8a: sample the leak BEFORE closing -- the close is what fails when a router
        # background task has been releasing savepoints on this same connection behind the test's
        # back, and by then the diagnosis has to already be in hand. See `_watch_savepoint_order`.
        leaked = pending_router_background_tasks()
        violations = _savepoint_order_violations(_db_connection)
        # The outer rollback is the hermeticity contract (D-06/D-07) and must run even when the close
        # raises -- previously a raising close skipped it entirely. Pinned by
        # `tests/shared/test_conftest_hermeticity.py::test_the_outer_rollback_runs_even_when_the_close_raises`.
        await _finalize_shared_connection_session(s, leaked, violations, rollback=outer.rollback)


@pytest_asyncio.fixture
async def verify(session: AsyncSession, _db_connection: AsyncConnection) -> AsyncGenerator[AsyncSession]:
    """Yield an INDEPENDENT read session bound to the same per-test ``_db_connection`` (D-07).

    This is the migration target for plan 92-04's 21 independent-verify call sites (which today build
    their own ``async_sessionmaker(async_engine)`` on a DIFFERENT connection and will go RED after this
    rewrite lands). Because it shares the ONE outer-transaction connection with :func:`session`, a read
    through this session SEES rows an in-test ``session.commit()`` made -- proving "commit visible to a
    sibling on the same connection" -- while remaining a distinct ``Session`` object (its own identity
    map) so it exercises a genuine round-trip to the DB rather than returning the writer's cached ORM
    instances. It, too, uses ``join_transaction_mode="create_savepoint"`` so it joins the outer
    transaction without disturbing it; teardown just closes it (the outer rollback in :func:`session`
    discards everything).

    Depends on :func:`session` (WR-03) so the ordering is correct BY CONSTRUCTION rather than by
    call-site parameter convention: requesting ``session`` guarantees its ``outer`` transaction is begun
    before this fixture runs, and pins pytest's teardown to close THIS session before ``session`` fires
    its ``await outer.rollback()`` (closing a session whose outer txn was already rolled back would
    raise). The parameter is a pure ordering dependency -- the body still binds to ``_db_connection``.
    """
    s = AsyncSession(bind=_db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield s
    finally:
        # phaze-5lq8a: this finalizer is the one the savepoint error was reported from, and it is
        # the FIRST of the DB fixtures to tear down (it depends on `session`, so it unwinds before
        # it) -- which makes it the earliest point at which an undrained router background task can
        # be named. Sampled before the close for the same reason as in `session`.
        #
        # Being first is also why this can only DIAGNOSE and never PREVENT: every fixture that could
        # host a drain runs after this one, and by then the leaked task has already issued the
        # RELEASE that discarded this session's savepoint. Prevention lives in the test body, in the
        # `drain_router_background_tasks()` call before the first read through `verify` -- see
        # `tests/_background_drain`, "AND NO, THIS CANNOT BE MOVED INTO A FIXTURE", before deleting
        # one of those calls on the grounds that this guard would catch it.
        leaked = pending_router_background_tasks()
        violations = _savepoint_order_violations(_db_connection)
        await _finalize_shared_connection_session(s, leaked, violations)


@pytest_asyncio.fixture
async def client(session) -> AsyncGenerator[AsyncClient]:  # type: ignore[no-untyped-def]
    """Yield an async HTTP test client with database session override.

    phaze-6bkk: ``app.state.controller_queue`` / ``app.state.task_router`` are wired to the shared
    fakes by default. The real lifespan (which builds them) never runs under ASGITransport, and
    since DIST-01 forced the archive-touching routes (/tags write+undo, /cue generate) to become
    PRODUCERS rather than doing their own disk I/O, an unwired ``app.state`` turns every one of
    them into an ``AttributeError``. Tests that want to assert on the enqueues still call
    ``install_fake_queues`` / ``wire_fakes`` themselves to get a handle -- both simply replace
    these, so the default is invisible to them.
    """
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        install_fake_queues(ac)
        yield ac


@pytest_asyncio.fixture
async def seed_test_agent(session: AsyncSession) -> tuple[Agent, str]:
    """Create a known agent with a known token. Returns (agent, raw_token).

    Token format: ``phaze_agent_<43 urlsafe-base64 chars>`` per phase-25 D-01.
    Hash storage: full wire string (prefix + secret) sha256-hex (D-02).
    """
    raw_token = "phaze_agent_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    agent = Agent(
        id="test-agent-01",  # kebab-case slug valid under ck_agents_id_charset
        name="test-agent-01",
        token_hash=token_hash,
        scan_roots=["/test/music"],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, raw_token


@pytest_asyncio.fixture
async def authenticated_client(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> AsyncGenerator[AsyncClient]:
    """AsyncClient with Authorization: Bearer <known token> pre-set.

    Mirrors the existing ``client`` fixture's session-override pattern and
    additionally pre-sets the Authorization header so handlers gated by
    ``Depends(get_authenticated_agent)`` (Plan 02) succeed.
    """
    _agent, raw_token = seed_test_agent
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Phase 52 (Plan 02): one-shot job_runner fixtures.
#
# These are Postgres-free on purpose — the DB-less one-shot pod they exercise
# never touches the DB-backed fixtures above. They set the minimal AgentSettings
# env (PHAZE_ROLE=agent + the agent URL/token/CA/models) and clear the
# ``get_settings`` lru_cache so each test gets a fresh ``AgentSettings`` dispatch.
#
# ``construct_agent_client`` passes the CA path to ``httpx.AsyncClient(verify=...)``,
# which eagerly builds an SSLContext from the file — so the CA file must be a
# PARSEABLE PEM cert (respx intercepts below TLS, so it is never used in a real
# handshake; it only has to load). This is a static self-signed test CA.
# ---------------------------------------------------------------------------

_TEST_CA_PEM = """\
-----BEGIN CERTIFICATE-----
MIIDETCCAfmgAwIBAgIUNtMbPaofTIJdy9NQ3DYaj21GqSIwDQYJKoZIhvcNAQEL
BQAwGDEWMBQGA1UEAwwNcGhhemUtdGVzdC1jYTAeFw0yNjA2MjcxODQ4MjZaFw0z
NjA2MjQxODQ4MjZaMBgxFjAUBgNVBAMMDXBoYXplLXRlc3QtY2EwggEiMA0GCSqG
SIb3DQEBAQUAA4IBDwAwggEKAoIBAQCiaszaVlmBuSK6XNSPIImqljRnng2JuYER
hfpSZUMMkANAGFacOmTegNlmLZELJ9aq0CqrQbv4tjQ9qfF/cCsJK+jxquNsU1MT
HCb4fG88pMDt4hoOs3yRJ+4lAs4lv/STP4soVNpf9lohtg4Fdd1FPsptQtWS3ueD
OhYYHaW3Qv8LkfllLdkUTfqBeFNJtNX0q0hFspXnZTEVnw1Vyk2s5n06LyNU2bkC
JftKLY+DwAquctOUcTCyU3rJiHKujO66jmjWMnJF/SIe4zVIw9PNLhYVQn3jDo7b
YHcl7eYsbglEN/FQOO+mKhGNR1rGZPODyaWRizbfd7pd/aSzWxgrAgMBAAGjUzBR
MB0GA1UdDgQWBBSXbKGkSkpaPreFj9PhkD0N2OFWMTAfBgNVHSMEGDAWgBSXbKGk
SkpaPreFj9PhkD0N2OFWMTAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUA
A4IBAQBgjGFfbTr6mikB2BYgU+ushgwcjMsUGsq9GRi4YwqQ1MGRcqzAAXaIITWw
YPut7xDz+Ly8w4QsEvEJNNUashpyfrarbhS5m0O2ifZPZd9E+zk73YYsTPAXhJAy
F/IHc31D/sgbgORIfKdK4QXO4wTWe4I+YUwBeV28VOj72V/8RICEJYrdx1DfBh9R
E0opkznSBx52nk4eFI8IEjsLTxs3zL7GvSKCHICWdPHqP9Pb71QJdeT+PcoWINnf
sQ+jfRtVfnQ0LzU/94K1Su4p2yF/n3nHZtBSOjulqm4F5uL6kDrn68I3Z9G/gaBU
jzO99XLCVGQKtzlazzxEILFIF8Ih
-----END CERTIFICATE-----
"""


@pytest.fixture
def job_env(monkeypatch: pytest.MonkeyPatch, tmp_path):  # type: ignore[no-untyped-def]
    """Env + on-disk artifacts the one-shot job_runner reads at startup.

    Writes a non-empty CA file (so ``construct_agent_client`` passes its
    existence/size guard) and a models dir, sets the agent env vars, and yields
    the resolved values. The ``get_settings`` lru_cache is cleared before AND
    after so the autouse env-isolation fixture cannot leak a cached settings
    object across tests.
    """
    from phaze.config import get_settings

    ca_file = tmp_path / "phaze-ca.crt"
    ca_file.write_text(_TEST_CA_PEM, encoding="utf-8")
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    file_id = uuid.uuid4()

    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://app.test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", str(tmp_path))
    monkeypatch.setenv("PHAZE_AGENT_CA_FILE", str(ca_file))
    monkeypatch.setenv("PHAZE_MODELS_DIR", str(models_dir))
    monkeypatch.setenv("PHAZE_JOB_FILE_ID", str(file_id))
    # phaze-27myl: job_runner.py is EXCLUSIVELY the k8s one-shot analyze-pod entrypoint
    # (build_job_manifest code-injects PHAZE_AGENT_KIND=compute alongside PHAZE_JOB_FILE_ID on
    # every real deploy, docs/k8s-burst.md Sec 6) -- match that here so this fixture stays
    # accurate to the real env AND so AgentSettings' queue_url fail-fast (scoped to
    # kind != "compute") doesn't apply to it; job_runner never imports the PostgresQueue
    # machinery and its documented agent-env ConfigMap carries no PHAZE_QUEUE_URL.
    monkeypatch.setenv("PHAZE_AGENT_KIND", "compute")

    get_settings.cache_clear()
    yield {
        "file_id": file_id,
        "base_url": "http://app.test",
        "ca_file": str(ca_file),
        "models_dir": str(models_dir),
    }
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Phase 54 (Plan 03): fake-kube seam fixture.
#
# kr8s talks to the API server over httpx, so the seam (kube_staging.py) tests
# stub the kube REST surface with respx -- the same library the S3 upload/agent
# tests already use. kr8s performs API *discovery* on first use
# (GET /version, /api, /apis) before any verb; an unstubbed discovery call fails
# with a confusing connection error (RESEARCH Pitfall 5). This fixture pre-stubs
# those discovery endpoints with a minimal canned doc and yields the respx router
# so each seam test registers only the verbs it asserts on.
#
# KUBECONFIG is pointed at a nonexistent path so kr8s's KubeAuth never loads the
# host's real kubeconfig (e.g. a local colima cluster) -- the seam server URL comes
# solely from the test-supplied kube_api_url.
# ---------------------------------------------------------------------------

KUBE_TEST_API_URL = "https://kube.test"


@pytest.fixture
def kube_respx(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Stub kr8s API-discovery endpoints; yield a respx router for the seam tests.

    The router has ``assert_all_called=False`` so the (often-unused) discovery stubs
    never fail a test that only touches one verb; individual tests add the create/get/
    list/delete routes they assert on. kr8s appends a trailing slash to the discovery
    endpoints (e.g. ``GET /version/``), so these stubs use a trailing-slash-tolerant regex.
    """
    import re

    from httpx import Response
    import respx

    monkeypatch.setenv("KUBECONFIG", "/nonexistent/phaze-test-kubeconfig")

    base = re.escape(KUBE_TEST_API_URL)
    with respx.mock(base_url=KUBE_TEST_API_URL, assert_all_called=False) as router:
        router.get(url__regex=rf"^{base}/version/?$").mock(return_value=Response(200, json={"major": "1", "minor": "30", "gitVersion": "v1.30.0"}))
        router.get(url__regex=rf"^{base}/api/?$").mock(return_value=Response(200, json={"kind": "APIVersions", "versions": ["v1"]}))
        router.get(url__regex=rf"^{base}/api/v1/?$").mock(
            return_value=Response(200, json={"kind": "APIResourceList", "groupVersion": "v1", "resources": []})
        )
        router.get(url__regex=rf"^{base}/apis/?$").mock(return_value=Response(200, json={"kind": "APIGroupList", "groups": []}))
        yield router


# ---------------------------------------------------------------------------
# Phase 60 (Plan 60-01): Review & Apply seed factories.
#
# Async ORM insert factories (test fixtures only -- no backend change) that the
# Wave-0 scaffold and every later Review workspace plan build their assertions on.
# Each fixture returns an async callable bound to the shared test ``session`` (the
# SAME object the ``client`` fixture overrides ``get_session`` with, so seeded rows
# are visible to HX requests). Values are kept ASCII-safe and built through the real
# model constructors; the legacy agent is already seeded by ``async_engine`` so a
# bare ``FileRecord`` satisfies its NOT NULL + FK ``agent_id`` default.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
def make_file(session: AsyncSession):  # type: ignore[no-untyped-def]
    """Return an async factory inserting one ``FileRecord`` (unique path per call)."""

    async def _make(
        *,
        original_filename: str = "set.mp3",
        file_type: str = "mp3",
        sha256: str | None = None,
    ) -> FileRecord:
        # Phase 90 (MIG-04): the ``files.state`` column is gone; a file's stage/status is DERIVED
        # from its output rows (AnalysisResult / RenameProposal / CloudJob / DedupResolution markers).
        # Callers needing a specific derived status seed the corresponding marker themselves (the
        # sibling factories below do exactly that).
        # Unique path segment so the (agent_id, original_path) unique index never collides.
        record = FileRecord(
            agent_id="test-fileserver",
            id=uuid.uuid4(),
            sha256_hash=sha256 or (uuid.uuid4().hex + uuid.uuid4().hex),  # 64 hex chars
            original_path=f"/test/music/{uuid.uuid4().hex}/{original_filename}",
            original_filename=original_filename,
            current_path=f"/test/music/{original_filename}",
            file_type=file_type,
            file_size=1024,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record

    return _make


@pytest_asyncio.fixture
def seed_pending_proposal(session: AsyncSession, make_file):  # type: ignore[no-untyped-def]
    """Return an async factory inserting a PROPOSAL_GENERATED file + one PENDING proposal.

    ``confidence`` may be ``None`` (the NULLABLE column) to seed the Pitfall-2 case that the
    ``confidence >= 0.9`` SQL predicate must exclude.
    """

    async def _make(
        confidence: float | None,
        *,
        proposed_filename: str = "Renamed Set.mp3",
        proposed_path: str | None = None,
        original_filename: str = "orig.mp3",
    ) -> RenameProposal:
        file = await make_file(original_filename=original_filename)
        proposal = RenameProposal(
            id=uuid.uuid4(),
            file_id=file.id,
            proposed_filename=proposed_filename,
            proposed_path=proposed_path,
            confidence=confidence,
            status=ProposalStatus.PENDING.value,
        )
        session.add(proposal)
        await session.commit()
        await session.refresh(proposal)
        proposal.file = file
        return proposal

    return _make


@pytest_asyncio.fixture
def seed_executed_file_with_metadata(session: AsyncSession, make_file):  # type: ignore[no-untyped-def]
    """Return an async factory inserting an applied file + its FileMetadata (tag-compare source).

    Phase 85: applied-ness is carried by an EXECUTED ``RenameProposal`` (the ``applied()`` gate),
    NOT by ``files.state``. The file is seeded ``state='moved'`` (a real post-apply state, not the
    dead ``EXECUTED`` sentinel) so a reverted ``state == EXECUTED`` guard would drop it (mutation-safe).
    """

    async def _make(
        *,
        original_filename: str = "executed.mp3",
        artist: str | None = "Old Artist",
        title: str | None = "Old Title",
        album: str | None = None,
        year: int | None = None,
        genre: str | None = None,
        track_number: int | None = None,
    ) -> tuple[FileRecord, FileMetadata]:
        file = await make_file(original_filename=original_filename)
        session.add(
            RenameProposal(
                id=uuid.uuid4(),
                file_id=file.id,
                proposed_filename=original_filename,
                status=ProposalStatus.EXECUTED.value,
            )
        )
        md = FileMetadata(
            id=uuid.uuid4(),
            file_id=file.id,
            artist=artist,
            title=title,
            album=album,
            year=year,
            genre=genre,
            track_number=track_number,
        )
        session.add(md)
        await session.commit()
        await session.refresh(file)
        return file, md

    return _make


@pytest_asyncio.fixture
def seed_duplicate_group(session: AsyncSession, make_file):  # type: ignore[no-untyped-def]
    """Return an async factory inserting ``count`` EXECUTED files that share one sha256 (a dupe group)."""

    async def _make(*, count: int = 2, sha256: str | None = None) -> list[FileRecord]:
        shared = sha256 or (uuid.uuid4().hex + uuid.uuid4().hex)
        files: list[FileRecord] = []
        for i in range(count):
            files.append(await make_file(original_filename=f"dupe-{i}.mp3", sha256=shared))
        return files

    return _make


@pytest_asyncio.fixture
def seed_cue_set(session: AsyncSession, make_file):  # type: ignore[no-untyped-def]
    """Return an async factory inserting an applied file + approved Tracklist + a version/track.

    ``eligible=True`` seeds >=1 timestamped track (the cue eligibility gate); ``eligible=False``
    seeds a track with NO timestamp (the ineligible "awaiting tracklist match" card).

    Phase 85: applied-ness is carried by an EXECUTED ``RenameProposal`` (the ``applied()`` gate),
    not ``files.state``; the file is seeded ``state='moved'`` so a reverted ``state == EXECUTED``
    cue guard would drop it (mutation-safe).
    """

    async def _make(*, eligible: bool = True, original_filename: str | None = None) -> tuple[FileRecord, Tracklist, TracklistVersion]:
        fname = original_filename or ("cue-eligible.mp3" if eligible else "cue-ineligible.mp3")
        file = await make_file(original_filename=fname)
        session.add(
            RenameProposal(
                id=uuid.uuid4(),
                file_id=file.id,
                proposed_filename=fname,
                status=ProposalStatus.EXECUTED.value,
            )
        )
        tracklist = Tracklist(
            id=uuid.uuid4(),
            external_id=f"ext-{uuid.uuid4().hex[:12]}",
            source_url="https://example.test/tracklist",
            file_id=file.id,
            status="approved",
        )
        session.add(tracklist)
        await session.commit()
        version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tracklist.id, version_number=1)
        session.add(version)
        await session.commit()
        track = TracklistTrack(
            id=uuid.uuid4(),
            version_id=version.id,
            position=1,
            title="Track 1",
            artist="Artist",
            timestamp="00:00:00" if eligible else None,
        )
        session.add(track)
        tracklist.latest_version_id = version.id
        await session.commit()
        return file, tracklist, version

    return _make


# ---------------------------------------------------------------------------
# Phase 61 (Plan 61-01): record / palette / agents / empty-state seed factories.
#
# Wave-0 read-model fixtures the four surface plans (61-02..05) verify against.
# Same async-factory shape as make_file above (build on make_file; add -> commit
# -> refresh); no backend/logic change. See 61-VALIDATION.md "Wave 0 Requirements".
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
def seed_file_with_windows(session: AsyncSession, make_file):  # type: ignore[no-untyped-def]
    """Return an async factory seeding a file + its ``AnalysisResult`` + fine/coarse ``AnalysisWindow`` rows.

    Backs the record timeline (RECORD-01): the aggregate carries bpm/musical_key with
    non-NULL fine/coarse window counts; the per-window rows span
    ``tier="fine"`` (bpm/musical_key populated) and ``tier="coarse"`` (mood/style populated)
    with distinct ``window_index`` so a timeline read has ordered, tiered data.
    """

    async def _make(
        *,
        fine_count: int = 3,
        coarse_count: int = 2,
        original_filename: str = "analyzed-set.mp3",
    ) -> tuple[FileRecord, AnalysisResult, list[AnalysisWindow]]:
        file = await make_file(original_filename=original_filename)
        result = AnalysisResult(
            id=uuid.uuid4(),
            file_id=file.id,
            bpm=128.0,
            musical_key="Am",
            mood="energetic",
            style="techno",
            fine_windows_analyzed=fine_count,
            fine_windows_total=fine_count,
            coarse_windows_analyzed=coarse_count,
            coarse_windows_total=coarse_count,
            analysis_completed_at=datetime.now(UTC),
        )
        session.add(result)
        windows: list[AnalysisWindow] = []
        for i in range(fine_count):
            windows.append(
                AnalysisWindow(
                    id=uuid.uuid4(),
                    file_id=file.id,
                    tier="fine",
                    window_index=i,
                    start_sec=float(i * 30),
                    end_sec=float((i + 1) * 30),
                    bpm=128.0 + i,
                    musical_key="Am" if i % 2 == 0 else "C",
                )
            )
        for i in range(coarse_count):
            windows.append(
                AnalysisWindow(
                    id=uuid.uuid4(),
                    file_id=file.id,
                    tier="coarse",
                    window_index=i,
                    start_sec=float(i * 60),
                    end_sec=float((i + 1) * 60),
                    mood="energetic" if i % 2 == 0 else "dark",
                    style="techno",
                )
            )
        session.add_all(windows)
        await session.commit()
        await session.refresh(result)
        return file, result, windows

    return _make


@pytest_asyncio.fixture
def seed_distinct_artists(session: AsyncSession, make_file):  # type: ignore[no-untyped-def]
    """Return an async factory seeding ``FileMetadata`` + ``Tracklist`` rows with distinct artists.

    Backs ``distinct_artists()`` (RECORD-02, D-05): includes a name SHARED across both tables
    (must collapse to ONE distinct result) and a ``None`` artist in each table (must be excluded).
    Returns the sorted set of the non-None artist names actually seeded.
    """

    async def _make() -> set[str]:
        shared = "Bonobo"
        # FileMetadata artists (one shared, one unique, one None).
        meta_artists: list[str | None] = [shared, "Four Tet", None]
        for artist in meta_artists:
            file = await make_file(original_filename="meta.mp3")
            session.add(FileMetadata(id=uuid.uuid4(), file_id=file.id, artist=artist, title="t"))
        # Tracklist artists (one shared with metadata, one unique, one None).
        tl_artists: list[str | None] = [shared, "Caribou", None]
        for artist in tl_artists:
            session.add(
                Tracklist(
                    id=uuid.uuid4(),
                    external_id=f"ext-{uuid.uuid4().hex[:12]}",
                    source_url="https://example.test/tl",
                    artist=artist,
                    status="approved",
                )
            )
        await session.commit()
        return {"Bonobo", "Four Tet", "Caribou"}

    return _make


@pytest_asyncio.fixture
def seed_cloud_jobs(session: AsyncSession, make_file):  # type: ignore[no-untyped-def]
    """Return an async factory seeding ``CloudJob`` rows in a chosen liveness mix.

    Backs the compute-lane liveness derivation (RECORD-03, D-07 → COMPUTE-01): ``running`` count
    seeds ACTIVE-lane rows, ``submitted_inadmissible`` count seeds WAITING-lane rows (status=submitted
    + ``inadmissible=True``); passing all-zero leaves the IDLE (no live jobs) case. Each CloudJob
    needs a distinct ``file_id`` (unique FK), so every row gets its own ``make_file``. These rows carry
    NO ``backend_id``, so they aggregate into the trailing ``"unattributed"`` lane.
    """

    async def _make(*, running: int = 0, submitted_inadmissible: int = 0) -> list[CloudJob]:
        jobs: list[CloudJob] = []
        for _ in range(running):
            file = await make_file(original_filename="cloud-run.mp3")
            jobs.append(
                CloudJob(
                    id=uuid.uuid4(),
                    file_id=file.id,
                    s3_key=f"staging/{file.id}",
                    status=CloudJobStatus.RUNNING.value,
                )
            )
        for _ in range(submitted_inadmissible):
            file = await make_file(original_filename="cloud-wait.mp3")
            jobs.append(
                CloudJob(
                    id=uuid.uuid4(),
                    file_id=file.id,
                    s3_key=f"staging/{file.id}",
                    status=CloudJobStatus.SUBMITTED.value,
                    inadmissible=True,
                )
            )
        session.add_all(jobs)
        await session.commit()
        for job in jobs:
            await session.refresh(job)
        return jobs

    return _make
