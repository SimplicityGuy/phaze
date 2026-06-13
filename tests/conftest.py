"""Shared test fixtures for Phaze test suite."""

from collections.abc import AsyncGenerator
import hashlib
import os
import secrets

from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.database import get_session
from phaze.main import create_app
from phaze.models.agent import LEGACY_AGENT_ID, Agent
from phaze.models.base import Base


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test")

DB_FIXTURES = {"async_engine", "session", "client", "authenticated_client", "seed_test_agent"}


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
    from phaze.config import AgentSettings, ControlSettings

    for cls in (ControlSettings, AgentSettings):
        new_config = dict(cls.model_config)
        new_config["env_file"] = None
        monkeypatch.setattr(cls, "model_config", new_config)
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


@pytest_asyncio.fixture
async def async_engine():  # type: ignore[no-untyped-def]
    """Create async engine, set up tables, seed the legacy agent, yield, then tear down.

    Seeds a ``legacy-application-server`` Agent row after table creation so tests
    that construct ``FileRecord`` / ``ScanBatch`` without explicitly setting
    ``agent_id`` (relying on the model-level default added in phase 24) satisfy
    the NOT NULL + FK constraint.
    """
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session_factory() as setup_session:
        setup_session.add(Agent(id=LEGACY_AGENT_ID, name=LEGACY_AGENT_ID, scan_roots=[]))
        await setup_session.commit()
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(async_engine) -> AsyncGenerator[AsyncSession]:  # type: ignore[no-untyped-def]
    """Yield an async database session for testing."""
    async_session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(session) -> AsyncGenerator[AsyncClient]:  # type: ignore[no-untyped-def]
    """Yield an async HTTP test client with database session override."""
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
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
