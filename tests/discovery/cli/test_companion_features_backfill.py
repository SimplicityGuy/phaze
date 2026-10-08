"""Tests for the `phaze backfill companion-features` wrapper (phaze-osy6j).

Counting and page selection run against real Postgres in
``tests/discovery/services/test_companion_content.py``, and the agent half (the
``extract_companion_features`` task) in ``tests/discovery/test_companion_features_producers.py``. This
module covers the wrapper: dry-run by default and READ ONLY, page-by-page enqueue on ``--apply``, the
keyset cursor, and the exit code when an enqueue fails.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
import uuid

import pytest

from phaze import cli
from phaze.services.companion_content import BackfillCounts


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _FakeSession:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rolled_back = False

    async def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeRouter:
    instances: list[_FakeRouter] = []  # noqa: RUF012 -- test-only registry of constructed routers

    def __init__(self, **_kwargs: Any) -> None:
        self.enqueued: list[tuple[str, str, Any]] = []
        self.closed = False
        self.fail_on: int | None = None
        _FakeRouter.instances.append(self)

    async def enqueue_for_agent(self, *, agent_id: str, task_name: str, payload: Any) -> None:
        if self.fail_on is not None and len(self.enqueued) == self.fail_on:
            raise RuntimeError("broker down")
        self.enqueued.append((agent_id, task_name, payload))

    async def close(self) -> None:
        self.closed = True


_ROWS = [(uuid.uuid4(), f"/r/rel/{index}.nfo") for index in range(5)]


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    session = _FakeSession()
    state: dict[str, Any] = {"session": session, "afters": []}

    @asynccontextmanager
    async def _session() -> AsyncIterator[_FakeSession]:
        yield session

    async def _count(_session: Any) -> list[BackfillCounts]:
        return [
            BackfillCounts(agent_id="agent-a", companions=9, current=4, missing=3, stale_content=1, stale_extractor=1),
            BackfillCounts(agent_id="agent-b", companions=2, current=2, missing=0, stale_content=0, stale_extractor=0),
        ]

    async def _page(_session: Any, agent_id: str, *, after: tuple[str, uuid.UUID] | None, limit: int) -> list[tuple[uuid.UUID, str]]:
        assert agent_id == "agent-a"
        state["afters"].append(after)
        start = 0 if after is None else next(index for index, (file_id, path) in enumerate(_ROWS) if (path, file_id) == after) + 1
        return _ROWS[start : start + limit]

    _FakeRouter.instances = []
    monkeypatch.setattr(cli, "async_session", _session)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    monkeypatch.setattr(cli, "count_backfill", _count)
    monkeypatch.setattr(cli, "select_backfill_page", _page)
    monkeypatch.setattr(cli, "AgentTaskRouter", _FakeRouter)
    return state


def test_parser_defaults_to_a_dry_run_with_the_default_page_size() -> None:
    parser = cli._build_parser()

    args = parser.parse_args(["backfill", "companion-features"])
    assert (args.apply, args.page_size) == (False, cli.COMPANION_FEATURES_PAGE_SIZE)
    assert parser.parse_args(["backfill", "companion-features", "--apply", "--page-size", "50"]).apply is True
    with pytest.raises(SystemExit):
        parser.parse_args(["backfill", "companion-features", "--apply", "--dry-run"])


def test_dry_run_counts_read_only_and_enqueues_nothing(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backfill", "companion-features", "--page-size", "2"]) == 0

    out = capsys.readouterr().out
    assert "agent-a: companions=9 current=4 missing=3 stale_content=1 stale_extractor=1" in out
    assert "summary: companions=11 pending=5 jobs=3 page_size=2" in out
    assert "DRY RUN: nothing enqueued; 5 companion row(s) would be read in 3 job(s)." in out
    assert wired["session"].statements == ["SET TRANSACTION READ ONLY"]
    assert wired["session"].rolled_back is True
    assert _FakeRouter.instances == []


def test_apply_enqueues_one_meta_lane_job_per_page_with_a_keyset_cursor(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backfill", "companion-features", "--apply", "--page-size", "2"]) == 0

    (router,) = _FakeRouter.instances
    assert router.closed is True
    assert [(agent, task) for agent, task, _payload in router.enqueued] == [("agent-a", "extract_companion_features")] * 3
    sent = [(target.file_id, target.original_path) for _a, _t, payload in router.enqueued for target in payload.targets]
    assert sent == _ROWS
    assert wired["afters"] == [None, (_ROWS[1][1], _ROWS[1][0]), (_ROWS[3][1], _ROWS[3][0]), (_ROWS[4][1], _ROWS[4][0])]
    assert "agent-a: enqueued 3 job(s) for 5 row(s)" in capsys.readouterr().out


def test_a_failed_enqueue_stops_that_agent_and_exits_1(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _FakeRouter.__init__

    def _failing(self: _FakeRouter, **kwargs: Any) -> None:
        original(self, **kwargs)
        self.fail_on = 1

    monkeypatch.setattr(_FakeRouter, "__init__", _failing)

    assert cli.main(["backfill", "companion-features", "--apply", "--page-size", "2"]) == 1

    assert len(_FakeRouter.instances[0].enqueued) == 1
    assert "agent-a: enqueued 1 job(s) for 2 row(s); STOPPED: broker down" in capsys.readouterr().out


@pytest.mark.parametrize("page_size", ["0", "1001"])
def test_an_out_of_range_page_size_is_refused(wired: dict[str, Any], page_size: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backfill", "companion-features", "--page-size", page_size]) == 1
    assert "--page-size must be between 1 and 1000" in capsys.readouterr().err
    assert wired["session"].statements == []
