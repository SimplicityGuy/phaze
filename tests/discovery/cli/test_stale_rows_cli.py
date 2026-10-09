"""Tests for the `phaze backfill stale-row-candidates` / `reconcile-stale-rows` wrappers (phaze-5rfev).

Classification and writes run against real Postgres and real files in
``tests/discovery/services/test_stale_rows.py``. This module covers only the wrappers: flag parsing,
the READ ONLY transaction, stdin/stdout, the printed lines, and the commit/rollback decision. The
session is a fake that records what it was asked to do.
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
from phaze.services.stale_rows import ReconcileReport, RowAction, Verdict


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


_STALE, _DUP, _GONE, _REVIEWED = (uuid.UUID(int=n) for n in (1, 2, 3, 4))


def _report() -> ReconcileReport:
    return ReconcileReport(
        actions=[
            RowAction(Verdict.MERGE, _STALE, "/r/a/x.mp3", "/r/b/x.mp3", _DUP),
            RowAction(Verdict.MISSING, _GONE, "/r/gone.mp3"),
            RowAction(Verdict.KEPT_REVIEWED, _REVIEWED, "/r/a/y.mp3", "/r/b/y.mp3", _DUP, ["proposal:approved"]),
        ],
        present=10,
        unverifiable=1,
        already_missing=2,
    )


def test_parser_requires_an_agent_and_defaults_reconcile_to_dry_run() -> None:
    parser = cli._build_parser()

    candidates = parser.parse_args(["backfill", "stale-row-candidates", "--agent", "agent-a", "--under", "/r"])
    assert (candidates.agent_id, candidates.under) == ("agent-a", "/r")
    assert parser.parse_args(["backfill", "stale-row-candidates", "--agent", "agent-a"]).under is None
    assert parser.parse_args(["backfill", "reconcile-stale-rows", "--agent", "agent-a"]).apply is False
    assert parser.parse_args(["backfill", "reconcile-stale-rows", "--agent", "agent-a", "--apply"]).apply is True
    for argv in (["backfill", "stale-row-candidates"], ["backfill", "reconcile-stale-rows", "--apply", "--dry-run", "--agent", "a"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_candidates_print_the_document_read_only(
    fake_session: _FakeSession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    document = {"agent_id": "agent-a", "scan_roots": ["/r"], "under": "/r", "rows": [{"id": "1", "path": "/r/a.mp3", "sha256": "a" * 64, "size": 1}]}
    runner = AsyncMock(return_value=document)
    monkeypatch.setattr(cli, "stale_row_candidates", runner)

    assert cli.main(["backfill", "stale-row-candidates", "--agent", "agent-a", "--under", "/r"]) == 0

    out = capsys.readouterr()
    assert json.loads(out.out) == document
    assert "1 row(s) for agent 'agent-a'" in out.err
    assert fake_session.statements == ["SET TRANSACTION READ ONLY"]
    assert runner.await_args.args[1:] == ("agent-a", "/r")


def test_candidates_report_an_unknown_agent(fake_session: _FakeSession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "stale_row_candidates", AsyncMock(side_effect=ValueError("no agent 'x'")))

    assert cli.main(["backfill", "stale-row-candidates", "--agent", "x"]) == 1
    assert "no agent" in capsys.readouterr().err


def test_reconcile_dry_run_is_read_only_prints_every_verdict_and_rolls_back(
    fake_session: _FakeSession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = AsyncMock(return_value=_report())
    monkeypatch.setattr(cli, "reconcile_stale_rows", runner)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"agent_id": "agent-a", "rows": []})))

    assert cli.main(["backfill", "reconcile-stale-rows", "--agent", "agent-a"]) == 0

    out = capsys.readouterr().out
    assert f"MERGE         {_STALE}  /r/a/x.mp3  -> /r/b/x.mp3  (merge into row {_DUP})" in out
    assert f"MISSING       {_GONE}  /r/gone.mp3\n" in out
    assert f"KEPT_REVIEWED {_REVIEWED}  /r/a/y.mp3  -> /r/b/y.mp3  (row {_DUP})  [proposal:approved]" in out
    assert (
        "summary: repoint=0 merge=1 missing=1 restored=0 ambiguous=0 kept_reviewed=1 changed=0 "
        "already_missing=2 present=10 unverifiable=1 walk_errors=0"
    ) in out
    assert "DRY RUN: nothing written; 2 row(s) would change" in out
    assert fake_session.statements == ["SET TRANSACTION READ ONLY"]
    assert (fake_session.rolled_back, fake_session.committed) == (True, False)
    assert runner.await_args.kwargs == {"apply": False}


def test_reconcile_apply_commits(fake_session: _FakeSession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "reconcile_stale_rows", AsyncMock(return_value=_report()))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"agent_id": "agent-a", "rows": []})))

    assert cli.main(["backfill", "reconcile-stale-rows", "--agent", "agent-a", "--apply"]) == 0

    assert "APPLIED: 2 row(s) changed" in capsys.readouterr().out
    assert fake_session.statements == []
    assert (fake_session.rolled_back, fake_session.committed) == (False, True)


@pytest.mark.parametrize(("stdin", "error"), [("not json", "not the located JSON document"), ('{"agent_id": "other", "rows": []}', "not 'agent-a'")])
def test_reconcile_refuses_bad_input_without_writing(
    stdin: str, error: str, fake_session: _FakeSession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _refuse(_session: Any, agent_id: str, located: dict[str, Any], *, apply: bool) -> ReconcileReport:
        msg = f"the located document is for agent {located['agent_id']!r}, not {agent_id!r}"
        raise ValueError(msg)

    monkeypatch.setattr(cli, "reconcile_stale_rows", _refuse)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))

    assert cli.main(["backfill", "reconcile-stale-rows", "--agent", "agent-a", "--apply"]) == 1
    assert error in capsys.readouterr().err
    assert fake_session.committed is False
