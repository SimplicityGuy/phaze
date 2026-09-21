"""Tests for the `phaze backfill reenqueue-incomplete-analyses` management CLI (phaze-kj8dl).

Unit-level: the actual selection/routing behavior is exhaustively covered against a real
database in ``tests/analyze/services/pipeline/test_reanalysis_backfill.py``. This module tests
ONLY the thin argparse + print + exit-code wrapper in ``phaze.cli``, with the service layer and
I/O (``async_session`` / ``AgentTaskRouter``) stubbed out -- mirrors ``tests/agents/cli/
test_agents_add.py``'s split between DB-backed ``test_main_*`` cells and pure-function cells,
minus the DB dependency this module's wrapper does not need to prove.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze import cli
from phaze.services.reanalysis_backfill import ReanalysisOutcome


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def test_build_parser_accepts_backfill_reenqueue_incomplete_analyses() -> None:
    """``phaze backfill reenqueue-incomplete-analyses`` parses to the expected namespace."""
    parser = cli._build_parser()

    args = parser.parse_args(["backfill", "reenqueue-incomplete-analyses"])

    assert args.group == "backfill"
    assert args.backfill_command == "reenqueue-incomplete-analyses"


def test_build_parser_backfill_requires_a_subcommand() -> None:
    """A bare ``phaze backfill`` (no subcommand) is a usage error (``required=True`` subparsers)."""
    parser = cli._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["backfill"])


@pytest.fixture
def _stub_io(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub ``async_session`` (a no-op async context manager) and ``AgentTaskRouter.close``.

    Returns the ``AsyncMock`` standing in for ``task_router.close()`` so a test can assert it was
    awaited exactly once (the ``try/finally`` cleanup contract), regardless of what the routed
    service call underneath does.
    """

    @asynccontextmanager
    async def _fake_async_session() -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr(cli, "async_session", _fake_async_session)

    close_mock = AsyncMock()
    fake_router = type("FakeRouter", (), {"close": close_mock})()
    monkeypatch.setattr(cli, "AgentTaskRouter", lambda **_kwargs: fake_router)
    return close_mock


@pytest.mark.asyncio
async def test_run_reenqueue_incomplete_analyses_prints_report_and_exits_zero_on_no_candidates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stub_io: AsyncMock
) -> None:
    """Genuinely nothing to do: both diagnostic counts print, the outcome list is empty, exit 0."""
    monkeypatch.setattr(cli, "count_null_windows_columns_rows", AsyncMock(return_value=3))
    monkeypatch.setattr(cli, "count_applied_incomplete_analyses_rows", AsyncMock(return_value=2))
    monkeypatch.setattr(cli, "enqueue_incomplete_reanalysis", AsyncMock(return_value=[]))

    exit_code = await cli._run_reenqueue_incomplete_analyses()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "3 pre-Phase-43 legacy row(s)" in out
    assert "2 file(s) with incomplete prior coverage already executed/moved" in out
    assert "0 file(s) with incomplete prior analysis coverage routed" in out
    _stub_io.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_reenqueue_incomplete_analyses_prints_per_file_outcomes_and_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stub_io: AsyncMock
) -> None:
    """Per-file outcome lines print (via the ``on_outcome`` callback) and the tally is alphabetical."""
    monkeypatch.setattr(cli, "count_null_windows_columns_rows", AsyncMock(return_value=0))
    monkeypatch.setattr(cli, "count_applied_incomplete_analyses_rows", AsyncMock(return_value=0))

    queued_id, blocked_id = uuid.uuid4(), uuid.uuid4()
    outcomes = [
        ReanalysisOutcome(queued_id, "/music/a.mp3", "queued"),
        ReanalysisOutcome(blocked_id, "/music/b.mp3", "blocked"),
    ]

    async def _fake_enqueue(_session: Any, _app_state: Any, *, on_outcome: Any = None) -> list[ReanalysisOutcome]:
        for outcome in outcomes:
            if on_outcome is not None:
                on_outcome(outcome)
        return outcomes

    monkeypatch.setattr(cli, "enqueue_incomplete_reanalysis", _fake_enqueue)

    exit_code = await cli._run_reenqueue_incomplete_analyses()

    out = capsys.readouterr().out
    assert f"  {queued_id}  {'queued':<18}  /music/a.mp3" in out
    assert f"  {blocked_id}  {'blocked':<18}  /music/b.mp3" in out
    assert "2 file(s) with incomplete prior analysis coverage routed" in out
    assert "summary: blocked=1 queued=1" in out
    # At least one productive outcome (queued) -> success despite the blocked one.
    assert exit_code == 0


@pytest.mark.asyncio
async def test_run_reenqueue_incomplete_analyses_exits_nonzero_when_nothing_productive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stub_io: AsyncMock
) -> None:
    """Candidates existed but every single one was ``no_active_agent`` -> nonzero exit (finding #7)."""
    monkeypatch.setattr(cli, "count_null_windows_columns_rows", AsyncMock(return_value=0))
    monkeypatch.setattr(cli, "count_applied_incomplete_analyses_rows", AsyncMock(return_value=0))
    stuck = [ReanalysisOutcome(uuid.uuid4(), "/music/a.mp3", "no_active_agent")]
    monkeypatch.setattr(cli, "enqueue_incomplete_reanalysis", AsyncMock(return_value=stuck))

    exit_code = await cli._run_reenqueue_incomplete_analyses()

    assert exit_code == 1
    assert "summary: no_active_agent=1" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "runner_name", "runner_kwargs"),
    [
        (["reenqueue-incomplete-analyses"], "_run_reenqueue_incomplete_analyses", {}),
        (["set-projection"], "_run_backfill_set_projection", {}),
        (["recover-stranded-analyses", "--enqueue"], "_run_recover_stranded_analyses", {"enqueue": True}),
    ],
)
def test_main_backfill_dispatches_to_the_async_runner(
    monkeypatch: pytest.MonkeyPatch, args: list[str], runner_name: str, runner_kwargs: dict[str, bool]
) -> None:
    """Each backfill subcommand reaches its own runner with the operator's flags."""
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, runner_name, runner)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    assert cli.main(["backfill", *args]) == 0
    runner.assert_awaited_once_with(**runner_kwargs)


def test_main_backfill_propagates_nonzero_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_run_reenqueue_incomplete_analyses", AsyncMock(return_value=1))
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    assert cli.main(["backfill", "reenqueue-incomplete-analyses"]) == 1


def test_stranded_recovery_requires_explicit_enqueue_flag() -> None:
    parser = cli._build_parser()

    assert parser.parse_args(["backfill", "recover-stranded-analyses"]).enqueue is False
    assert parser.parse_args(["backfill", "recover-stranded-analyses", "--enqueue"]).enqueue is True


@pytest.mark.asyncio
async def test_stranded_recovery_dry_run_only_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stub_io: AsyncMock
) -> None:
    keys = {"process_file:synthetic-1", "process_file:synthetic-2"}
    monkeypatch.setattr(cli, "select_stranded_analysis_keys", AsyncMock(return_value=keys))
    recover = AsyncMock()
    monkeypatch.setattr(cli, "recover_orphaned_work", recover)

    assert await cli._run_recover_stranded_analyses(enqueue=False) == 0
    assert capsys.readouterr().out == "2 stranded analysis file(s) selected\n"
    recover.assert_not_awaited()
    _stub_io.assert_not_awaited()


@pytest.mark.asyncio
async def test_stranded_recovery_enqueues_only_selected_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _stub_io: AsyncMock
) -> None:
    keys = {"process_file:synthetic-1", "process_file:synthetic-2"}
    monkeypatch.setattr(cli, "select_stranded_analysis_keys", AsyncMock(return_value=keys))
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(queue_url="unused", redis_url="unused"))
    recover = AsyncMock(return_value={"stages": {"process_file": {"reenqueued": 2, "skipped": 0, "errored": 0, "unreplayable": 0}}})
    monkeypatch.setattr(cli, "recover_orphaned_work", recover)

    assert await cli._run_recover_stranded_analyses(enqueue=True) == 0
    assert "summary: reenqueued=2 skipped=0 errored=0 unreplayable=0" in capsys.readouterr().out
    assert recover.await_args.kwargs == {"force": True, "only_keys": keys}
    _stub_io.assert_awaited_once()
