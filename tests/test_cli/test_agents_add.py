"""Tests for the `phaze agents add` management CLI (phaze.cli)."""

import asyncio

import pytest
from sqlalchemy import NullPool, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze import cli
from phaze.cli import add_agent, derive_queue_name, validate_agent_id, validate_scan_roots
from phaze.models.agent import Agent
from phaze.routers.agent_auth import hash_token
from tests.conftest import TEST_DATABASE_URL


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


def test_main_success_inserts_and_prints(
    async_engine,  # type: ignore[no-untyped-def] — ensures schema exists in the test DB
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


def test_main_duplicate_id_exits_nonzero(
    async_engine,  # type: ignore[no-untyped-def]
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
