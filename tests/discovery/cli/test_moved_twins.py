"""Tests for the `phaze backfill moved-twin-candidates` / `retire-moved-twins` wrappers (phaze-oxn2m).

Selection, classification and retirement run against real Postgres in
``tests/discovery/services/test_moved_file_state.py``. This module covers only the wrappers: flag
parsing, the READ ONLY transaction, stdin/stdout, and the commit/rollback decision. The session is a
fake that records what it was asked to do.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import io
import json
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze import cli
from phaze.services.scan_deletion import MovedTwinReport


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _FakeSession:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    session = _FakeSession()

    @asynccontextmanager
    async def _session() -> AsyncIterator[_FakeSession]:
        yield session

    monkeypatch.setattr(cli, "async_session", _session)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    return session


def _report() -> MovedTwinReport:
    stale, twin, kept = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    return MovedTwinReport(retire=[(stale, twin)], same_content=1, kept_reviewed=[(kept, twin, ["proposal:approved"])], absent_without_twin=2)


def test_parser_requires_an_agent_and_defaults_retire_to_dry_run() -> None:
    parser = cli._build_parser()

    assert parser.parse_args(["backfill", "moved-twin-candidates", "--agent", "agent-a"]).agent_id == "agent-a"
    assert parser.parse_args(["backfill", "retire-moved-twins", "--agent", "agent-a"]).apply is False
    assert parser.parse_args(["backfill", "retire-moved-twins", "--agent", "agent-a", "--apply"]).apply is True
    for argv in (["backfill", "moved-twin-candidates"], ["backfill", "retire-moved-twins", "--apply", "--dry-run", "--agent", "a"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_candidates_print_the_document_read_only(
    fake_session: _FakeSession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    document = {"agent_id": "agent-a", "scan_roots": ["/r"], "groups": [[{"id": "1", "path": "/r/a.mp3"}, {"id": "2", "path": "/r/b/a.mp3"}]]}
    monkeypatch.setattr(cli, "moved_twin_candidates", AsyncMock(return_value=document))

    assert cli.main(["backfill", "moved-twin-candidates", "--agent", "agent-a"]) == 0

    out = capsys.readouterr()
    assert json.loads(out.out) == document
    assert "1 group(s), 2 row(s)" in out.err
    assert fake_session.statements == ["SET TRANSACTION READ ONLY"]


def test_candidates_report_an_unknown_agent(fake_session: _FakeSession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "moved_twin_candidates", AsyncMock(side_effect=ValueError("no agent 'x'")))

    assert cli.main(["backfill", "moved-twin-candidates", "--agent", "x"]) == 1
    assert "no agent" in capsys.readouterr().err


def test_retire_dry_run_is_read_only_and_rolls_back(
    fake_session: _FakeSession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = AsyncMock(return_value=_report())
    monkeypatch.setattr(cli, "retire_moved_twins", runner)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"agent_id": "agent-a", "groups": []})))

    assert cli.main(["backfill", "retire-moved-twins", "--agent", "agent-a"]) == 0

    out = capsys.readouterr().out
    assert "summary: retire=1 (same content=1) kept_reviewed=1 absent_without_twin=2" in out
    assert "reviewed: proposal:approved" in out
    assert "DRY RUN: nothing written; 1 row(s) would be retired" in out
    assert fake_session.statements == ["SET TRANSACTION READ ONLY"]
    assert (fake_session.rolled_back, fake_session.committed) == (True, False)
    assert runner.await_args.kwargs == {"apply": False}


def test_retire_apply_commits(fake_session: _FakeSession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "retire_moved_twins", AsyncMock(return_value=_report()))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"agent_id": "agent-a", "groups": []})))

    assert cli.main(["backfill", "retire-moved-twins", "--agent", "agent-a", "--apply"]) == 0

    assert "APPLIED: 1 row(s) retired" in capsys.readouterr().out
    assert fake_session.statements == []
    assert (fake_session.rolled_back, fake_session.committed) == (False, True)


@pytest.mark.parametrize(
    ("stdin", "error"), [("not json", "not the checked JSON document"), ('{"agent_id": "other", "groups": []}', "not 'agent-a'")]
)
def test_retire_refuses_bad_input_without_writing(
    stdin: str, error: str, fake_session: _FakeSession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _refuse(_session: Any, agent_id: str, checked: dict[str, Any], *, apply: bool) -> MovedTwinReport:
        msg = f"the checked document is for agent {checked['agent_id']!r}, not {agent_id!r}"
        raise ValueError(msg)

    monkeypatch.setattr(cli, "retire_moved_twins", _refuse)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))

    assert cli.main(["backfill", "retire-moved-twins", "--agent", "agent-a", "--apply"]) == 1
    assert error in capsys.readouterr().err
    assert fake_session.committed is False
