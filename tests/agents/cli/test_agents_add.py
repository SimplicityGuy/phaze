"""Tests for the `phaze agents add` management CLI (phaze.cli)."""

import asyncio

import pytest
from sqlalchemy import NullPool, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze import cli
from phaze.cli import add_agent, derive_queue_name, validate_agent_id, validate_agent_name, validate_scan_roots
from phaze.models.agent import Agent
from phaze.routers.agent_auth import hash_token
from tests.conftest import TEST_DATABASE_URL


@pytest.fixture
def _cleanup_committed_agents(async_engine: object) -> "object":  # type: ignore[misc]
    """Delete any agent this module COMMITTED (via the CLI `agents add` path), preserving the FK parent.

    CLEAN-02 (92-05, deferred item DI-92-04-01): the ``test_main_*`` cells drive ``cli.main`` through a
    real ``create_async_engine(TEST_DATABASE_URL)`` sessionmaker that COMMITS agent rows (``cli-ok``,
    ``oci-a1``, ``oci-tok``, ``cli-dup``). Under 92-03's session-scoped engine there is no per-test
    ``drop_all``/``create_all``, so those committed rows survived into ``tests/agents/services/
    test_agent_bootstrap.py`` and made ``ensure_dev_agent`` see a non-empty agents table (5 spurious
    failures in the combined ``tests/agents`` bucket; the cells passed in isolation). This teardown wipes
    every agent EXCEPT the session-scoped ``test-fileserver`` FK parent (seeded once by ``async_engine``,
    the target of every hermetic ``make_file``), restoring the empty-table precondition the bootstrap
    cells rely on. Uses its OWN NullPool engine because the hermetic ``session`` fixture is rolled back at
    teardown and cannot delete a COMMITTED row. Depends on ``async_engine`` so the schema (and the
    ``agents`` table) is guaranteed to exist; only the committing ``test_main_*`` cells request it, so the
    pure-function/``main``-exit cells that never touch the DB are untouched.
    """
    yield

    async def _clean() -> None:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(delete(Agent).where(Agent.id != "test-fileserver"))
        finally:
            await engine.dispose()

    asyncio.run(_clean())


# ---------------------------------------------------------------------------
# Pure-function validation (no DB) — proves rejection happens before any write.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["Foo_Bar", "-x", "x-", "", "a b", "x--y", "UPPER"])
def test_validate_agent_id_rejects_bad(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid agent id"):
        validate_agent_id(bad)


@pytest.mark.parametrize("good", ["x-y", "a1", "fileserver-east", "abc", "a-b-c"])
def test_validate_agent_id_accepts_good(good: str) -> None:
    assert validate_agent_id(good) is None


def test_validate_agent_id_rejects_65_chars() -> None:
    """phaze-pev8: a charset-valid id past the `agents.id` String(64) column width must be
    rejected here, BEFORE any DB access -- Postgres would otherwise raise StringDataRightTruncation
    (surfaced by SQLAlchemy as DataError, a DBAPIError sibling of IntegrityError, not a subclass),
    which escapes the CLI's `except IntegrityError` as a raw traceback."""
    too_long = "a" * 65
    with pytest.raises(ValueError, match="must be at most 64 characters"):
        validate_agent_id(too_long)


def test_validate_agent_id_accepts_64_chars() -> None:
    assert validate_agent_id("a" * 64) is None


def test_validate_agent_name_rejects_129_chars() -> None:
    """phaze-pev8: mirrors the `agents.name` String(128) column width."""
    too_long = "x" * 129
    with pytest.raises(ValueError, match="must be at most 128 characters"):
        validate_agent_name(too_long)


def test_validate_agent_name_accepts_128_chars() -> None:
    assert validate_agent_name("x" * 128) is None


def test_validate_scan_roots_rejects_relative() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        validate_scan_roots(["data/music"])


def test_validate_scan_roots_rejects_empty_entry() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        validate_scan_roots([""])


def test_validate_scan_roots_accepts_absolute() -> None:
    assert validate_scan_roots(["/data/music", "/data/concerts"]) is None


def test_derive_queue_name() -> None:
    assert derive_queue_name("x-y") == "phaze-agent-x-y"


# ---------------------------------------------------------------------------
# add_agent — DB-backed (integration via the shared `session` fixture).
# ---------------------------------------------------------------------------


async def test_add_agent_happy_path(session: AsyncSession) -> None:
    token = await add_agent(session, "x-y", "X", ["/data/music"])
    assert token.startswith("phaze_agent_")

    row = (await session.execute(select(Agent).where(Agent.id == "x-y"))).scalar_one()
    assert row.token_hash == hash_token(token)
    assert row.name == "X"
    assert row.scan_roots == ["/data/music"]
    assert derive_queue_name("x-y") == "phaze-agent-x-y"


async def test_add_agent_compute_empty_roots(session: AsyncSession) -> None:
    token = await add_agent(session, "oci-a1", "OCI A1", [], kind="compute")
    assert token.startswith("phaze_agent_")

    row = (await session.execute(select(Agent).where(Agent.id == "oci-a1"))).scalar_one()
    assert row.kind == "compute"
    assert row.scan_roots == []


async def test_add_agent_defaults_fileserver(session: AsyncSession) -> None:
    await add_agent(session, "fs1", "FS1", ["/data/music"])
    row = (await session.execute(select(Agent).where(Agent.id == "fs1"))).scalar_one()
    assert row.kind == "fileserver"


async def test_add_agent_duplicate_id_raises(session: AsyncSession) -> None:
    await add_agent(session, "dup", "Dup", ["/data/music"])
    with pytest.raises(IntegrityError):
        await add_agent(session, "dup", "Dup Again", ["/data/concerts"])
    await session.rollback()


# ---------------------------------------------------------------------------
# main() exit codes — drives the print/exit branches.
# ---------------------------------------------------------------------------


def test_main_invalid_id_exits_before_db(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["agents", "add", "--id", "Bad_Id", "--scan-roots", "/data/music"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip()  # error went to stderr
    assert "phaze_agent_" not in captured.out  # no token minted


def test_main_relative_scan_root_exits_before_db(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["agents", "add", "--id", "ok-id", "--scan-roots", "relative/path"])
    assert rc == 1
    assert capsys.readouterr().err.strip()


def test_main_65_char_id_exits_before_db(capsys: pytest.CaptureFixture[str]) -> None:
    """phaze-pev8: a 65-char all-lowercase kebab id passes AGENT_ID_RE but exceeds the
    `agents.id` String(64) column -- must be a friendly error-plus-exit-1, not a raw
    DataError traceback from an opened DB session."""
    over_long_id = "a" * 65
    rc = cli.main(["agents", "add", "--id", over_long_id, "--scan-roots", "/data/music"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "must be at most 64 characters" in captured.err
    assert "phaze_agent_" not in captured.out  # no token minted -- no session opened


def test_main_129_char_explicit_name_exits_before_db(capsys: pytest.CaptureFixture[str]) -> None:
    """phaze-pev8: an explicit --name past the `agents.name` String(128) column must be
    rejected before any DB access."""
    over_long_name = "n" * 129
    rc = cli.main(["agents", "add", "--id", "ok-id", "--name", over_long_name, "--scan-roots", "/data/music"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "must be at most 128 characters" in captured.err
    assert "phaze_agent_" not in captured.out


def test_main_derived_titleized_name_over_128_exits_before_db(capsys: pytest.CaptureFixture[str]) -> None:
    """phaze-pev8: with no --name, the default is `agent_id.replace("-", " ").title()`, which
    never changes the string's length (only "-" -> " " and case). So once :func:`validate_agent_id`
    bounds the id at 64 chars, the titleized default can never exceed the 128-char name column --
    the id check now transitively closes the originally-reported "long id -> over-long derived
    name" path. This asserts that ordering directly: an id long enough to have tripped the old
    unbounded-name bug is rejected by the id-length check first, before the name is even derived."""
    over_long_id = "a-" * 64 + "a"  # 129 chars, well past MAX_AGENT_ID_LENGTH
    rc = cli.main(["agents", "add", "--id", over_long_id, "--scan-roots", "/data/music"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "must be at most 64 characters" in captured.err
    assert "phaze_agent_" not in captured.out


def test_main_success_inserts_and_prints(
    async_engine,  # type: ignore[no-untyped-def] — ensures schema exists in the test DB
    _cleanup_committed_agents: object,  # wipes the committed agent so it never leaks into a later bucket cell
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A NullPool engine gives fresh, immediately-closed connections so the loop
    # created by main()'s `asyncio.run` does not collide with the fixture loop.
    factory_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(factory_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(cli, "async_session", factory)
    try:
        rc = cli.main(["agents", "add", "--id", "cli-ok", "--name", "CLI OK", "--scan-roots", "/data/music"])
    finally:
        asyncio.run(factory_engine.dispose())
    assert rc == 0
    out = capsys.readouterr().out
    assert "phaze_agent_" in out
    assert "phaze-agent-cli-ok" in out


def test_main_compute_no_scan_roots_succeeds(
    async_engine,  # type: ignore[no-untyped-def] — ensures schema exists in the test DB
    _cleanup_committed_agents: object,  # wipes the committed agent so it never leaks into a later bucket cell
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(factory_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(cli, "async_session", factory)
    try:
        rc = cli.main(["agents", "add", "--kind", "compute", "--id", "oci-a1", "--name", "OCI A1"])
    finally:
        asyncio.run(factory_engine.dispose())
    assert rc == 0
    out = capsys.readouterr().out
    assert "phaze_agent_" in out
    assert "phaze-agent-oci-a1" in out


def test_main_fileserver_without_scan_roots_fails(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["agents", "add", "--kind", "fileserver", "--id", "fs1", "--name", "FS1"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip()  # scan roots still required for fileserver
    assert "phaze_agent_" not in captured.out  # no token minted


def test_main_compute_token_not_logged(
    async_engine,  # type: ignore[no-untyped-def]
    _cleanup_committed_agents: object,  # wipes the committed agent so it never leaks into a later bucket cell
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-13: the minted token reaches stdout (print) but never a logger."""
    factory_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(factory_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(cli, "async_session", factory)
    with caplog.at_level(0):
        try:
            rc = cli.main(["agents", "add", "--kind", "compute", "--id", "oci-tok", "--name", "OCI Tok"])
        finally:
            asyncio.run(factory_engine.dispose())
    assert rc == 0
    out = capsys.readouterr().out
    assert "phaze_agent_" in out  # token printed
    assert "phaze_agent_" not in caplog.text  # token NOT in any log record


def test_main_duplicate_id_exits_nonzero(
    async_engine,  # type: ignore[no-untyped-def]
    _cleanup_committed_agents: object,  # wipes the committed agent so it never leaks into a later bucket cell
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(factory_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(cli, "async_session", factory)
    args = ["agents", "add", "--id", "cli-dup", "--scan-roots", "/data/music"]
    try:
        assert cli.main(args) == 0
        rc = cli.main(args)
    finally:
        asyncio.run(factory_engine.dispose())
    assert rc == 1
    assert "already exists" in capsys.readouterr().err
