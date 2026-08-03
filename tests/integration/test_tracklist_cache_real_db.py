"""``_find_cached_tracklists`` against a REAL Postgres connection (phaze-hu8v changes-requested).

The hermetic unit suite (``tests/identify/tasks/test_tracklist.py``) exercises
``_find_cached_tracklists`` against a ``MagicMock`` session, which proves the WHERE clause is
*constructed* correctly (via ``.compile()``) but not that the correlated EXISTS subquery
*executes* correctly against real Postgres -- exactly the gap that let the original
``latest_version_id.is_not(None)``-only condition ship with a cache-poisoning defect undetected:
a tracklist whose first-ever scrape parsed to zero tracks (soft-block, persisted Turnstile
interstitial, selector drift) still gets a non-null ``latest_version_id`` and was wrongly treated
as "already scraped successfully", permanently skipping every future re-fetch.

This module opens a REAL ``async_sessionmaker`` against the shared session-scoped ``async_engine``
fixture (``tests/conftest.py``, real Postgres via ``TEST_DATABASE_URL``) and seeds genuine
Tracklist/TracklistVersion/TracklistTrack rows, mirroring the pattern in
``test_tracklist_refresh_cron.py``.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.tasks.tracklist import _find_cached_tracklists


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


pytestmark = pytest.mark.integration


async def test_find_cached_tracklists_excludes_empty_first_scrape(async_engine: AsyncEngine) -> None:
    """A tracklist whose latest (and only) version has ZERO tracks is NOT treated as cached.

    Regression guard for the phaze-hu8v changes-requested defect: without the EXISTS-track
    predicate, this row's non-null ``latest_version_id`` alone would have wrongly classified it as
    "already scraped successfully", permanently skipping every future re-fetch of a tracklist that
    was never actually scraped -- a politeness cache silently converting a transient failure
    (soft-block / interstitial / selector drift on the first attempt) into permanent data loss.
    """
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tracklist_id = uuid.uuid4()
    version_id = uuid.uuid4()
    external_id = f"empty-{uuid.uuid4().hex[:12]}"

    async with session_factory() as session:
        session.add(
            Tracklist(
                id=tracklist_id,
                external_id=external_id,
                source_url="https://example.test/tracklist/empty",
                latest_version_id=version_id,
                status="approved",
            )
        )
        session.add(TracklistVersion(id=version_id, tracklist_id=tracklist_id, version_number=1))
        # Deliberately NO TracklistTrack rows -- the empty-first-scrape shape.
        await session.commit()

    try:
        async with session_factory() as session:
            cached = await _find_cached_tracklists(session, [external_id])

        assert external_id not in cached, "a zero-track latest version must NOT be treated as cached"
    finally:
        async with session_factory() as session:
            await session.execute(delete(TracklistVersion).where(TracklistVersion.id == version_id))
            await session.execute(delete(Tracklist).where(Tracklist.id == tracklist_id))
            await session.commit()


async def test_find_cached_tracklists_includes_successfully_scraped(async_engine: AsyncEngine) -> None:
    """A tracklist whose latest version HAS tracks IS treated as cached and returned."""
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tracklist_id = uuid.uuid4()
    version_id = uuid.uuid4()
    external_id = f"ok-{uuid.uuid4().hex[:12]}"

    async with session_factory() as session:
        session.add(
            Tracklist(
                id=tracklist_id,
                external_id=external_id,
                source_url="https://example.test/tracklist/ok",
                artist="Sven Väth",
                event="Time Warp",
                date=date(2024, 4, 14),
                latest_version_id=version_id,
                status="approved",
            )
        )
        session.add(TracklistVersion(id=version_id, tracklist_id=tracklist_id, version_number=1))
        session.add(TracklistTrack(version_id=version_id, position=1, artist="Some Artist", title="Some Title"))
        await session.commit()

    try:
        async with session_factory() as session:
            cached = await _find_cached_tracklists(session, [external_id])

        assert external_id in cached
        assert cached[external_id].artist == "Sven Väth"
        assert cached[external_id].event == "Time Warp"
    finally:
        async with session_factory() as session:
            await session.execute(delete(TracklistTrack).where(TracklistTrack.version_id == version_id))
            await session.execute(delete(TracklistVersion).where(TracklistVersion.id == version_id))
            await session.execute(delete(Tracklist).where(Tracklist.id == tracklist_id))
            await session.commit()


async def test_find_cached_tracklists_mixed_batch_excludes_only_the_empty_one(async_engine: AsyncEngine) -> None:
    """One bulk lookup over a mixed batch returns exactly the successfully-scraped rows.

    Exercises the EXISTS subquery correctness across MULTIPLE external_ids in a single query --
    the shape ``search_tracklist`` actually calls with (one result set per search).
    """
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    empty_tracklist_id = uuid.uuid4()
    empty_version_id = uuid.uuid4()
    empty_external_id = f"mixed-empty-{uuid.uuid4().hex[:12]}"
    ok_tracklist_id = uuid.uuid4()
    ok_version_id = uuid.uuid4()
    ok_external_id = f"mixed-ok-{uuid.uuid4().hex[:12]}"

    async with session_factory() as session:
        session.add(
            Tracklist(
                id=empty_tracklist_id,
                external_id=empty_external_id,
                source_url="https://example.test/tracklist/mixed-empty",
                latest_version_id=empty_version_id,
                status="approved",
            )
        )
        session.add(TracklistVersion(id=empty_version_id, tracklist_id=empty_tracklist_id, version_number=1))
        session.add(
            Tracklist(
                id=ok_tracklist_id,
                external_id=ok_external_id,
                source_url="https://example.test/tracklist/mixed-ok",
                latest_version_id=ok_version_id,
                status="approved",
            )
        )
        session.add(TracklistVersion(id=ok_version_id, tracklist_id=ok_tracklist_id, version_number=1))
        session.add(TracklistTrack(version_id=ok_version_id, position=1, artist="A", title="T"))
        await session.commit()

    try:
        async with session_factory() as session:
            cached = await _find_cached_tracklists(session, [empty_external_id, ok_external_id, "never-seen-id"])

        assert set(cached.keys()) == {ok_external_id}
    finally:
        async with session_factory() as session:
            await session.execute(delete(TracklistTrack).where(TracklistTrack.version_id == ok_version_id))
            await session.execute(delete(TracklistVersion).where(TracklistVersion.id.in_([empty_version_id, ok_version_id])))
            await session.execute(delete(Tracklist).where(Tracklist.id.in_([empty_tracklist_id, ok_tracklist_id])))
            await session.commit()
