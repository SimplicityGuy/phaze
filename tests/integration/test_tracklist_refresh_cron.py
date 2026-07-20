"""phaze-xpzp: ``refresh_tracklists`` against a REAL Postgres connection (aware/naive datetime bind).

The hermetic unit suite (``tests/identify/tasks/test_tracklist.py``) replaces the SAQ ctx's session
with a ``MagicMock``, so ``session.execute(...)`` never actually reaches asyncpg's parameter-binding
codec -- the aware-vs-naive datetime mismatch that made the monthly ``refresh_tracklists`` cron a
complete no-op (D-10: stale-refresh + unresolved predicates OR'd into ONE statement) was invisible to
CI by construction. This module opens a REAL ``async_sessionmaker`` against the shared session-scoped
``async_engine`` fixture (``tests/conftest.py``, real Postgres via ``TEST_DATABASE_URL``) -- each
``async with ctx["async_session"]()`` opens its OWN connection off that engine, exactly like
production's controller ctx -- so the bind is genuinely ENCODED by asyncpg, proving:

  1. the stale/unresolved SELECT binds a timezone-NAIVE threshold that matches the naive
     ``tracklists.updated_at`` column (``TimestampMixin``) without asyncpg raising ``DataError``, and
  2. an unresolved tracklist (``file_id IS NULL``) is actually selected and processed.

Only test 1 exercises the real bind; the control-flow assertion that a query failure surfaces in
``errors`` (rather than being swallowed into a success-looking ``{"refreshed": 0, "errors": 0}``) is
covered by the cheaper mocked-session unit test
(``test_refresh_tracklists_query_failure_is_reported_as_error`` in
``tests/identify/tasks/test_tracklist.py``).

Uses the session-scoped ``async_engine`` fixture (the repo's default real-Postgres harness)
rather than ``committed_db``, because this test needs no committed-row visibility across
sessions -- a per-test rollback is sufficient.

Historical note: this module previously avoided ``committed_db`` because that fixture's guard
was ``target_db.endswith("_test")``, which REJECTED per-developer databases named
``phaze_test_<dev>`` and silently skipped. That guard now lives in ``tests/db_guard.py``,
accepts a ``test`` segment anywhere in the name, and ERRORS instead of skipping, so the
workaround is no longer needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.tracklist import Tracklist
from phaze.tasks import tracklist as tracklist_task


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


pytestmark = pytest.mark.integration


def _make_ctx(engine: AsyncEngine) -> dict[str, Any]:
    """Build a controller-shaped ctx whose ``async_session`` is a REAL sessionmaker on ``engine``.

    Mirrors production's controller ctx (``ctx["async_session"]`` is a real ``async_sessionmaker``,
    not a mock) so ``refresh_tracklists``'s ``async with ctx["async_session"]() as session:`` opens a
    genuine asyncpg connection and the datetime bind is actually encoded.
    """
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm}


async def test_refresh_tracklists_binds_naive_threshold_against_real_db(
    async_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale/unresolved query must not raise asyncpg ``DataError`` and must select real rows.

    Before the fix, ``stale_threshold = datetime.now(tz=UTC) - timedelta(days=90)`` is TZ-AWARE while
    ``Tracklist.updated_at`` is a naive ``TIMESTAMP WITHOUT TIME ZONE`` column -- asyncpg's naive-timestamp
    codec raises ``DataError`` ("can't subtract offset-naive and offset-aware datetimes") at ENCODE time.
    The broad ``except Exception`` around the whole query then swallows it and returns the untouched
    ``{"refreshed": 0, "errors": 0}`` initial counters -- indistinguishable from "nothing to do". Seeding
    one UNRESOLVED tracklist (``file_id IS NULL``, matched by the OR'd D-10 predicate regardless of the
    threshold) means a working bind must select and process it.
    """
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tracklist_id = uuid.uuid4()

    async with session_factory() as session:
        session.add(
            Tracklist(
                id=tracklist_id,
                external_id=f"ext-{uuid.uuid4().hex[:12]}",
                source_url="https://example.test/tracklist",
                file_id=None,  # unresolved (D-10) -- matched independent of the stale threshold
                status="approved",
            )
        )
        await session.commit()

    try:
        # Avoid a real HTTP scrape and the 60-300s jitter sleep; the point of this test is the SELECT
        # bind, not the per-tracklist re-scrape body (covered by the hermetic unit suite).
        mock_scrape = AsyncMock(return_value={"tracklist_id": "x", "tracks_found": 0, "version": 1})
        monkeypatch.setattr(tracklist_task, "scrape_and_store_tracklist", mock_scrape)
        monkeypatch.setattr(tracklist_task.asyncio, "sleep", AsyncMock())

        ctx = _make_ctx(async_engine)
        result = await tracklist_task.refresh_tracklists(ctx)

        assert result == {"refreshed": 1, "errors": 0}, f"real-DB refresh did not process the seeded row: {result!r}"
        mock_scrape.assert_awaited_once()
    finally:
        async with session_factory() as session:
            await session.execute(delete(Tracklist).where(Tracklist.id == tracklist_id))
            await session.commit()
