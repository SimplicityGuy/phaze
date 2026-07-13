"""Shared test fixtures for Phaze test suite."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
import hashlib
import os
import secrets
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


def _coerce_async_dsn(dsn: str) -> str:
    """Coerce a libpq / psycopg2 Postgres DSN to the asyncpg driver the async fixtures need.

    ``async_engine`` feeds ``TEST_DATABASE_URL`` straight to ``create_async_engine``. A bare
    ``postgresql://`` (or explicit ``postgresql+psycopg2://``) URL resolves SQLAlchemy's default
    ``psycopg2`` sync dialect, which is not installed (the async stack uses asyncpg) — every
    DB-fixture test then dies at setup with a cryptic ``No module named 'psycopg2'``. Operators
    naturally export the libpq form (it matches ``PHAZE_QUEUE_URL``), so normalize it here rather
    than leaking the footgun into each fixture.
    """
    for sync_prefix in ("postgresql+psycopg2://", "postgresql+psycopg://", "postgresql://"):
        if dsn.startswith(sync_prefix):
            return "postgresql+asyncpg://" + dsn[len(sync_prefix) :]
    return dsn


TEST_DATABASE_URL = _coerce_async_dsn(os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test"))

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


@pytest_asyncio.fixture
async def async_engine():  # type: ignore[no-untyped-def]
    """Create async engine, set up tables, seed the shared test fileserver, yield, then tear down.

    Seeds a real ``kind='fileserver'`` Agent row (``test-fileserver``) after table
    creation so tests that flush ``FileRecord`` / ``ScanBatch`` rows have a valid FK
    target for ``agent_id`` (ON DELETE RESTRICT). Phase 89 (LEGACY-03, D-08) dropped the
    ``agent_id`` model-level default, so every construction now supplies ``agent_id``
    explicitly (pointing at this seed) rather than relying on the removed
    ``legacy-application-server`` sentinel default.
    """
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session_factory() as setup_session:
        setup_session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
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
    ``sampled=True`` and non-NULL fine/coarse window counts; the per-window rows span
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
            sampled=True,
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

    Backs ``classify_compute_lanes`` (RECORD-03, D-07): ``running`` count seeds ACTIVE-lane
    rows, ``submitted_inadmissible`` count seeds WAITING-lane rows (status=submitted +
    ``inadmissible=True``); passing all-zero leaves the IDLE (no live jobs) case. Each CloudJob
    needs a distinct ``file_id`` (unique FK), so every row gets its own ``make_file``.
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
