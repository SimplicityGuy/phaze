"""Tests for the `phaze backfill companion-links` wrapper (phaze-spd83).

The unit it runs (association, then the junk-review refresh) is real-Postgres tested in
``tests/discovery/services/test_companion_autolink.py`` and ``tests/discovery/services/test_companion.py``.
This module covers the wrapper: dry run by default in a READ ONLY transaction that is rolled back,
``--apply`` under each agent's association lock, a locked agent skipped with exit 1, and the report.
"""

from __future__ import annotations

from collections import Counter
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest

from phaze import cli
from phaze.services.companion import AssociationOutcome
from phaze.services.companion_junk_review import DetectionOutcome


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _Result:
    def scalars(self) -> list[str]:
        return ["agent-a", "agent-b"]


class _FakeSession:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rolled_back = 0

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(str(statement))
        return _Result()

    async def rollback(self) -> None:
        self.rolled_back += 1


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    session = _FakeSession()
    state: dict[str, Any] = {"session": session, "calls": [], "locked": set(), "locks": []}

    @asynccontextmanager
    async def _session() -> AsyncIterator[_FakeSession]:
        yield session

    @asynccontextmanager
    async def _lock(_session: Any, agent_id: str) -> AsyncIterator[bool]:
        state["locks"].append(agent_id)
        yield agent_id not in state["locked"]

    async def _run(_session: Any, agent_id: str, *, apply: bool) -> tuple[AssociationOutcome, DetectionOutcome | None]:
        state["calls"].append((agent_id, apply))
        association = AssociationOutcome(links_created=3, links_removed=1, links_kept=5, awaiting_features=2, decided={"reference": 2, "unlinked": 1})
        detection = DetectionOutcome(agent_id=agent_id, created=Counter({"duplicate": 1}), withdrawn=2) if apply else None
        return association, detection

    monkeypatch.setattr(cli, "async_session", _session)
    monkeypatch.setattr(cli, "agent_association_lock", _lock)
    monkeypatch.setattr(cli, "run_agent_association", _run)
    return state


def test_dry_run_is_the_default_read_only_rolled_back_and_takes_no_lock(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backfill", "companion-links"]) == 0

    assert wired["calls"] == [("agent-a", False), ("agent-b", False)]
    assert wired["locks"] == []
    assert sum("READ ONLY" in statement for statement in wired["session"].statements) == 2
    assert wired["session"].rolled_back == 2
    out = capsys.readouterr().out
    assert "agent-a: decided by step: reference=2 unlinked=1; links added=3 removed=1 kept=5; awaiting features=2" in out
    assert "junk review" not in out
    assert "summary: links added=6 removed=2 kept=10 awaiting features=4" in out
    assert "DRY RUN" in out


def test_apply_runs_each_agent_under_its_lock_and_reports_the_junk_refresh(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backfill", "companion-links", "--apply"]) == 0

    assert wired["calls"] == [("agent-a", True), ("agent-b", True)]
    assert wired["locks"] == ["agent-a", "agent-b"]
    assert not any("READ ONLY" in statement for statement in wired["session"].statements[1:])
    out = capsys.readouterr().out
    assert "junk review: new pending=1 withdrawn=2" in out
    assert out.rstrip().endswith("APPLIED: links re-derived and the junk-review queue refreshed")


def test_an_agent_whose_automatic_run_holds_the_lock_is_skipped_and_exits_1(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    wired["locked"].add("agent-a")

    assert cli.main(["backfill", "companion-links", "--apply"]) == 1

    assert wired["calls"] == [("agent-b", True)]
    out = capsys.readouterr().out
    assert "agent-a: SKIPPED" in out
    assert "1 agent(s) skipped, re-run to finish" in out
