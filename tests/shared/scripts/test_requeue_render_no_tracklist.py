"""The phaze-c9go7 re-queue tool: it moves exactly the in-scope cache rows to due, and nothing else.

The SQL under test is the SQL the operator runs: ``apply_requeue`` executes ``apply_statements`` and
``psql_script`` is built from the same tuple (pinned below), so there is no second copy to drift.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text

from phaze.enums.tracklist_candidate import CacheDecision, LookupOutcome
from phaze.models.tracklist_lookup_cache import TracklistLookupCache
from phaze.services.tracklist_lookup_cache import verdict_for
from scripts import requeue_render_no_tracklist as tool


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


IN_SCOPE_DETAIL = "render no_tracklist: page rendered but no track list"
FUTURE = datetime.now(UTC) + timedelta(days=100)


def _row(key: str, outcome: LookupOutcome, detail: str | None, *, expires_at: datetime | None = FUTURE, attempts: int = 1) -> TracklistLookupCache:
    return TracklistLookupCache(
        set_key=key,
        query_text=f"query {key}",
        outcome=outcome.value,
        detail=detail,
        attempts=attempts,
        expires_at=expires_at,
        first_attempted_at=datetime(2026, 8, 15, tzinfo=UTC),
        last_attempted_at=datetime(2026, 8, 15, tzinfo=UTC),
    )


IN_SCOPE = ("zk-in-1", "zk-in-2", "zk-in-3")
OUT_OF_SCOPE = (
    "zk-found",
    "zk-search",
    "zk-other-detail",
    "zk-blocked",
    "zk-render-failed",
    "zk-expired-in-scope",
    "zk-null-detail",
    "zk-low-confidence",
)


async def _seed(session: AsyncSession) -> None:
    session.add_all(
        [
            _row("zk-in-1", LookupOutcome.NOT_FOUND, IN_SCOPE_DETAIL, attempts=2),
            _row("zk-in-2", LookupOutcome.NOT_FOUND, "render no_tracklist: no detail"),
            _row("zk-in-3", LookupOutcome.NOT_FOUND, "render no_tracklist: ", expires_at=None),
            _row("zk-found", LookupOutcome.FOUND, None, expires_at=None),
            _row("zk-search", LookupOutcome.NOT_FOUND, "search no_results"),
            _row("zk-other-detail", LookupOutcome.NOT_FOUND, "render parse_failed: selectors matched nothing"),
            _row("zk-blocked", LookupOutcome.BLOCKED, "render no_tracklist: carried over"),
            _row("zk-render-failed", LookupOutcome.RENDER_FAILED, "render no_tracklist: carried over"),
            _row("zk-expired-in-scope", LookupOutcome.NOT_FOUND, IN_SCOPE_DETAIL, expires_at=datetime(2026, 1, 1, tzinfo=UTC)),
            _row("zk-null-detail", LookupOutcome.NOT_FOUND, None),
            _row("zk-low-confidence", LookupOutcome.LOW_CONFIDENCE, "render no_tracklist: x"),
        ]
    )
    await session.flush()


async def _snapshot(session: AsyncSession) -> dict[str, tuple[str, str | None, int, datetime | None, datetime]]:
    session.expire_all()
    rows = (await session.execute(select(TracklistLookupCache))).scalars().all()
    return {r.set_key: (r.outcome, r.detail, r.attempts, r.expires_at, r.updated_at) for r in rows}


@pytest.mark.asyncio
async def test_requeue_touches_only_render_no_tracklist_rows(session: AsyncSession) -> None:
    await _seed(session)
    before = await _snapshot(session)

    assert await tool.count_in_scope(session) == len(IN_SCOPE)
    assert await tool.apply_requeue(session, len(IN_SCOPE)) == (len(IN_SCOPE), 0)

    after = await _snapshot(session)
    for key in OUT_OF_SCOPE:
        assert after[key] == before[key], key
    for key in IN_SCOPE:
        # Only the expiry (and updated_at) moves; the attempt record is preserved.
        assert after[key][:3] == before[key][:3], key
        assert after[key][3] is not None
        assert after[key][3] <= datetime.now(UTC) + timedelta(minutes=1)  # db clock, not the test process clock


@pytest.mark.asyncio
async def test_after_requeue_verdict_for_treats_exactly_those_rows_as_due(session: AsyncSession) -> None:
    await _seed(session)
    now = datetime.now(UTC) + timedelta(seconds=1)
    suppressed = set(IN_SCOPE)
    session.expire_all()
    for row in (await session.execute(select(TracklistLookupCache).where(TracklistLookupCache.set_key.in_(IN_SCOPE)))).scalars():
        assert verdict_for(row, now).decision is CacheDecision.SUPPRESSED_NEGATIVE

    await tool.apply_requeue(session, len(IN_SCOPE))
    session.expire_all()

    due = set()
    for row in (await session.execute(select(TracklistLookupCache))).scalars():
        verdict = verdict_for(row, now)
        if verdict.decision is CacheDecision.NEGATIVE_EXPIRED:
            assert verdict.should_query
            due.add(row.set_key)
    # "zk-expired-in-scope" was already due before the run and must stay so; nothing else becomes due.
    assert due == suppressed | {"zk-expired-in-scope"}
    for row in (
        await session.execute(
            select(TracklistLookupCache).where(TracklistLookupCache.set_key.in_(["zk-search", "zk-other-detail", "zk-null-detail"]))
        )
    ).scalars():
        assert verdict_for(row, now).decision is CacheDecision.SUPPRESSED_NEGATIVE


@pytest.mark.asyncio
async def test_dry_run_changes_no_row(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    await _seed(session)
    before = await _snapshot(session)

    class _Factory:
        async def __aenter__(self) -> AsyncSession:
            return session

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr("phaze.database.async_session", lambda: _Factory())
    assert await tool._run(tool.argparse.Namespace(apply=False, expect_count=None)) == 0

    assert await _snapshot(session) == before
    out = capsys.readouterr().out
    assert f"DRY RUN: {len(IN_SCOPE)} row(s) would be re-queued" in out
    assert "UPDATE tracklist_lookup_cache" in out
    for key in (*IN_SCOPE, *OUT_OF_SCOPE):
        assert key not in out  # counts only: no set keys


@pytest.mark.asyncio
async def test_expect_count_mismatch_changes_nothing(session: AsyncSession) -> None:
    await _seed(session)
    before = await _snapshot(session)

    with pytest.raises(ValueError, match="differs from --expect-count"):
        await tool.apply_requeue(session, len(IN_SCOPE) + 1)

    assert await _snapshot(session) == before


@pytest.mark.asyncio
async def test_sql_guard_aborts_on_a_wrong_count_when_run_through_psql(session: AsyncSession) -> None:
    """The printed script carries its own guard, so a psql run refuses exactly as ``--apply`` does."""
    await _seed(session)
    before = await _snapshot(session)
    nested = await session.begin_nested()
    with pytest.raises(Exception, match="requeue refused"):
        await session.execute(text(tool.guard_sql(len(IN_SCOPE) + 1)))
    await nested.rollback()
    assert await _snapshot(session) == before


@pytest.mark.asyncio
async def test_requeue_is_idempotent(session: AsyncSession) -> None:
    await _seed(session)
    await tool.apply_requeue(session, len(IN_SCOPE))
    assert await tool.count_in_scope(session) == 0
    assert await tool.apply_requeue(session, 0) == (0, 0)


def test_psql_script_is_the_same_text_apply_executes() -> None:
    script = tool.psql_script(116)
    assert script.startswith("BEGIN;\n")
    assert script.endswith("COMMIT;\n")
    for statement in tool.apply_statements(116):
        assert statement in script
    assert "116" in tool.apply_statements(116)[0]
    assert all(tool.SELECTOR in s for s in (tool.COUNT_SQL, tool.UPDATE_SQL))
