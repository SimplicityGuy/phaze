"""Tests for the `phaze backfill reset-cloud-attempts` CLI wrapper (phaze-ww6yk).

The selection, classification and write are covered against a real database in
``tests/analyze/services/test_cloud_attempts_reset.py``. This module covers only the wrapper: flag
parsing, the READ ONLY dry-run transaction, and the commit/rollback decision. The session is a fake
that records what it was asked to do.
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze import cli
from phaze.services.cloud_attempts_reset import AnalysisState, Disposition, LedgerAction, ResetReport, ResetScope, ScopedRow


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


_ARGS = [
    "backfill",
    "reset-cloud-attempts",
    "--backend",
    "kueue-burst",
    "--window-start",
    "2026-09-26T16:11:00Z",
    "--window-end",
    "2026-09-27T03:29:00Z",
    "--attempts-floor",
    "3",
]
_SCOPE = ResetScope("kueue-burst", datetime(2026, 9, 26, 16, 11, tzinfo=UTC), datetime(2026, 9, 27, 3, 29, tzinfo=UTC), 3)


def _row(disposition: Disposition = Disposition.RESET) -> ScopedRow:
    return ScopedRow(
        file_id=uuid.uuid4(),
        attempts=3,
        updated_at=datetime(2026, 9, 26, 22, tzinfo=UTC),
        last_exit_code=None,
        last_failure_reason=None,
        analysis=AnalysisState.NONE,
        disposition=disposition,
        ledger=LedgerAction.CLEARED if disposition is Disposition.RESET else LedgerAction.UNTOUCHED,
        ledger_chains_spent=1,
    )


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
    return session


def test_parser_defaults_to_dry_run_and_requires_every_scope_flag() -> None:
    parser = cli._build_parser()

    args = parser.parse_args(_ARGS)
    assert (args.backend_id, args.attempts_floor, args.apply) == ("kueue-burst", 3, False)
    assert args.window_start == datetime(2026, 9, 26, 16, 11, tzinfo=UTC)
    assert parser.parse_args([*_ARGS, "--apply"]).apply is True
    assert parser.parse_args([*_ARGS, "--dry-run"]).apply is False
    for flag in ("--backend", "--window-start", "--window-end", "--attempts-floor"):
        index = _ARGS.index(flag)
        with pytest.raises(SystemExit):
            parser.parse_args(_ARGS[:index] + _ARGS[index + 2 :])
    with pytest.raises(SystemExit):
        parser.parse_args([*_ARGS, "--apply", "--dry-run"])


@pytest.mark.parametrize(("value", "message"), [("2026-09-26T16:11:00", "no UTC offset"), ("yesterday", "not an ISO 8601")])
def test_window_bounds_refuse_a_naive_or_unparseable_time(value: str, message: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=message):
        cli._aware_datetime(value)


def test_main_builds_the_scope_and_passes_the_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, "_run_reset_cloud_attempts", runner)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    assert cli.main([*_ARGS, "--apply"]) == 0
    runner.assert_awaited_once_with(_SCOPE, apply=True)


def test_main_reports_an_inverted_window_without_opening_a_session(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, "_run_reset_cloud_attempts", runner)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    inverted = [*_ARGS]
    inverted[inverted.index("--window-end") + 1] = "2026-09-26T16:11:00Z"

    assert cli.main(inverted) == 1
    assert "is not after" in capsys.readouterr().err
    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_opens_a_read_only_transaction_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_session: _FakeSession
) -> None:
    report = ResetReport(scope=_SCOPE, rows=[_row(), _row(Disposition.EXCLUDED_ANALYZE_DONE)])
    preview = AsyncMock(return_value=report)
    apply = AsyncMock()
    monkeypatch.setattr(cli, "preview_reset", preview)
    monkeypatch.setattr(cli, "apply_reset", apply)

    assert await cli._run_reset_cloud_attempts(_SCOPE, apply=False) == 0

    assert fake_session.statements == ["SET TRANSACTION READ ONLY"]
    assert (fake_session.rolled_back, fake_session.committed) == (True, False)
    apply.assert_not_awaited()
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "2 row(s) in scope"
    assert out[-1] == "DRY RUN: nothing written; 1 row(s) would be reset. Re-run with --apply to write."


@pytest.mark.asyncio
async def test_apply_commits_when_the_update_matched_every_classified_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_session: _FakeSession
) -> None:
    report = ResetReport(scope=_SCOPE, rows=[_row(), _row()], applied=True, rows_reset=2, ledger_rows_deleted=2)
    monkeypatch.setattr(cli, "apply_reset", AsyncMock(return_value=report))

    assert await cli._run_reset_cloud_attempts(_SCOPE, apply=True) == 0

    assert (fake_session.committed, fake_session.rolled_back) == (True, False)
    assert fake_session.statements == []  # no READ ONLY on the write path
    assert capsys.readouterr().out.splitlines()[-1] == "APPLIED: 2 row(s) reset; cloud_budget rows deleted=2 decremented=0"


@pytest.mark.asyncio
async def test_apply_rolls_back_when_a_row_left_scope_under_the_update(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_session: _FakeSession
) -> None:
    report = ResetReport(scope=_SCOPE, rows=[_row(), _row()], applied=True, rows_reset=1)
    monkeypatch.setattr(cli, "apply_reset", AsyncMock(return_value=report))

    assert await cli._run_reset_cloud_attempts(_SCOPE, apply=True) == 1

    assert (fake_session.committed, fake_session.rolled_back) == (False, True)
    captured = capsys.readouterr()
    assert "reset matched 1 row(s) but 2 were classified for reset; rolled back" in captured.err
    assert "APPLIED" not in captured.out
