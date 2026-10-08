"""Tests for the `phaze junk quarantine` wrapper (phaze-lwuf6).

The plan and the dispatch run against real Postgres in ``tests/discovery/services/test_junk_quarantine.py``.
This module covers the wrapper: a dry run by default that lists every approved row with its destination
in a READ ONLY transaction and dispatches nothing, and ``--apply`` dispatching exactly the listed rows.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
import uuid

import pytest

from phaze import cli
from phaze.services.junk_quarantine import QuarantinePlanItem


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence


_IDS = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
_PLAN = [
    QuarantinePlanItem(_IDS[0], "agent-a", "empty", 0, "/music/a/site.nfo", "/music/.phaze-quarantine/a/site.nfo"),
    QuarantinePlanItem(_IDS[1], "agent-a", "known_stamp", 48, "/music/b/site.txt", "/music/.phaze-quarantine/b/site.txt"),
    QuarantinePlanItem(_IDS[2], "agent-b", "duplicate", 12, "/elsewhere/x.cue", None),
]


class _FakeSession:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rolled_back = 0

    async def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))

    async def rollback(self) -> None:
        self.rolled_back += 1


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    session = _FakeSession()
    state: dict[str, Any] = {"session": session, "dispatched": [], "enqueued": len(_PLAN)}

    @asynccontextmanager
    async def _session() -> AsyncIterator[_FakeSession]:
        yield session

    async def _plan(_session: Any) -> list[QuarantinePlanItem]:
        return list(_PLAN)

    async def _enqueue(_session: Any, review_ids: Sequence[uuid.UUID]) -> int:
        state["dispatched"].append(list(review_ids))
        return int(state["enqueued"])

    monkeypatch.setattr(cli, "async_session", _session)
    monkeypatch.setattr(cli, "plan_quarantine", _plan)
    monkeypatch.setattr(cli, "enqueue_quarantine", _enqueue)
    return state


def test_dry_run_is_the_default_lists_every_move_and_dispatches_nothing(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["junk", "quarantine"]) == 0

    session = wired["session"]
    assert any("READ ONLY" in statement for statement in session.statements)
    assert session.rolled_back == 1
    assert wired["dispatched"] == []
    out = capsys.readouterr().out
    assert "agent-a empty 0 bytes: /music/a/site.nfo -> /music/.phaze-quarantine/a/site.nfo" in out
    assert "agent-a known_stamp 48 bytes: /music/b/site.txt -> /music/.phaze-quarantine/b/site.txt" in out
    assert "agent-b duplicate 12 bytes: /elsewhere/x.cue -> REFUSED: outside every scan root of the agent" in out
    assert "summary: approved=3 agent-a=2 agent-b=1" in out
    assert "DRY RUN: nothing moved" in out


def test_explicit_dry_run_flag_is_the_same(wired: dict[str, Any]) -> None:
    assert cli.main(["junk", "quarantine", "--dry-run"]) == 0
    assert wired["dispatched"] == []


def test_apply_dispatches_exactly_the_listed_rows(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["junk", "quarantine", "--apply"]) == 0

    assert wired["dispatched"] == [_IDS]
    assert "APPLIED: 3 of 3 moves dispatched" in capsys.readouterr().out


def test_apply_exits_1_when_a_move_was_not_dispatched(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    wired["enqueued"] = 2

    assert cli.main(["junk", "quarantine", "--apply"]) == 1
    assert "APPLIED: 2 of 3 moves dispatched" in capsys.readouterr().out


def test_dry_run_and_apply_are_mutually_exclusive(wired: dict[str, Any]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["junk", "quarantine", "--dry-run", "--apply"])
    assert wired["dispatched"] == []
