"""Tests for the `phaze backfill junk-review` wrapper (phaze-bk5jp).

The detector itself runs against real Postgres in ``tests/discovery/services/test_companion_junk_review.py``.
This module covers the wrapper: dry run by default in a READ ONLY transaction that is rolled back,
one committed transaction per agent on ``--apply``, and the per-reason report.
"""

from __future__ import annotations

from collections import Counter
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest

from phaze import cli
from phaze.services.companion_junk_review import DetectionOutcome


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _Result:
    def __init__(self, values: list[str]) -> None:
        self._values = values

    def scalars(self) -> list[str]:
        return self._values


class _FakeSession:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.committed = 0
        self.rolled_back = 0

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(str(statement))
        return _Result(["agent-a", "agent-b"])

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    session = _FakeSession()
    state: dict[str, Any] = {"session": session, "calls": []}

    @asynccontextmanager
    async def _session() -> AsyncIterator[_FakeSession]:
        yield session

    async def _detect(_session: Any, agent_id: str, *, apply: bool) -> DetectionOutcome:
        state["calls"].append((agent_id, apply))
        if agent_id == "agent-a":
            return DetectionOutcome(agent_id=agent_id, created=Counter({"known_stamp": 3, "duplicate": 2}), reappeared=1, withdrawn=1)
        return DetectionOutcome(agent_id=agent_id)

    monkeypatch.setattr(cli, "async_session", _session)
    monkeypatch.setattr(cli, "detect_junk_reviews", _detect)
    return state


def test_dry_run_is_the_default_read_only_and_rolled_back(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backfill", "junk-review"]) == 0

    session = wired["session"]
    assert wired["calls"] == [("agent-a", False), ("agent-b", False)]
    assert sum("READ ONLY" in statement for statement in session.statements) == 2
    assert (session.committed, session.rolled_back) == (0, 2)
    out = capsys.readouterr().out
    assert "agent-a: new pending: duplicate=2 known_stamp=3 (reappeared=1)" in out
    assert "withdrawn=1" in out
    assert "agent-b: new pending: none" in out
    assert "summary: new pending=5 duplicate=2 known_stamp=3" in out
    assert "DRY RUN" in out


def test_apply_commits_one_transaction_per_agent(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backfill", "junk-review", "--apply"]) == 0

    session = wired["session"]
    assert wired["calls"] == [("agent-a", True), ("agent-b", True)]
    assert not any("READ ONLY" in statement for statement in session.statements)
    assert (session.committed, session.rolled_back) == (2, 0)
    assert "APPLIED" in capsys.readouterr().out
