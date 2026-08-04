"""The on-demand refresh -> drain re-admission loop, against a REAL Postgres connection.

WHY THIS MODULE EXISTS, AND WHAT IT REPLACED (phaze-2akf)
---------------------------------------------------------
Two integration modules used to live here and both were tied to the retired legacy scrape path:

* ``test_tracklist_refresh_cron.py`` proved (phaze-xpzp) that the monthly cron's stale-threshold
  SELECT bound a timezone-NAIVE datetime that asyncpg would actually encode. There is no monthly
  cron any more and no ``updated_at < threshold`` predicate to bind, so that specific defect is
  structurally unreachable; the class of defect it caught -- "a MagicMock session never reaches
  asyncpg's codec, so a bind bug is invisible to CI by construction" -- is why this replacement is
  ALSO an integration test rather than a unit one.
* ``test_tracklist_cache_real_db.py`` proved (phaze-hu8v) that the persistent cache's correlated
  EXISTS subquery really executed, so a zero-track FIRST scrape could not be mistaken for "already
  scraped successfully" and permanently suppress every future re-fetch. That cache is gone with
  ``_find_cached_tracklists``; its successor is ``tracklist_lookup_cache``, where the same defect
  is impossible rather than guarded -- a zero-track lookup is recorded as ``PARSE_FAILED``, a
  TRANSIENT, and never as ``FOUND``.

What is asserted here is the loop those two half-covered between them, end to end and against real
Postgres: an operator refresh makes a set the drain would otherwise never look at again eligible
again, exactly once, and the drain retires the request when it answers it.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.enums.tracklist_candidate import CacheDecision, LookupOutcome
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.models.tracklist_lookup_cache import TracklistLookupCache
from phaze.models.tracklist_priority_flag import TracklistPriorityFlag
from phaze.services.tracklist_drain import DrainCandidate, LookupAttempt, build_drain_queue, persist_lookup
from phaze.services.tracklist_lookup_cache import lookup, record_outcome
from phaze.services.tracklist_query import derive_query
from phaze.tasks.tracklist import refresh_tracklists


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


pytestmark = pytest.mark.integration


# A filename the classifier reads as a LIVE_SET, so the file genuinely reaches the drain funnel.
# An invented example in the repo's documented placeholder style -- never a real archive name.
LIVE_SET_FILENAME = "Artist - Time Warp 2024-04-14 (Live @ Mannheim).mp3"


def _ctx(session_factory: Any) -> dict[str, Any]:
    return {"async_session": session_factory}


@contextlib.asynccontextmanager
async def _seeded(engine: AsyncEngine) -> AsyncIterator[tuple[Any, uuid.UUID, Tracklist]]:
    """Seed a file + its canonical tracklist + a positive cache row, and clean up afterwards.

    Committed (not savepoint-nested) because the drain opens its OWN sessions off the engine, the
    same way production's controller ctx does -- which is the whole point of running this against
    real Postgres rather than a mock.
    """
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    file_id = uuid.uuid4()
    tracklist_id = uuid.uuid4()
    version_id = uuid.uuid4()
    external_id = f"refresh-{uuid.uuid4().hex[:12]}"
    set_key = ""

    async with session_factory() as session:
        session.add(
            FileRecord(
                id=file_id,
                agent_id="test-fileserver",
                sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                original_path=f"/music/{uuid.uuid4().hex}/{LIVE_SET_FILENAME}",
                original_filename=LIVE_SET_FILENAME,
                current_path=f"/music/{LIVE_SET_FILENAME}",
                file_type="mp3",
                file_size=900_000_000,
            )
        )
        await session.flush()
        # A long duration is what makes the classifier call this a LIVE_SET rather than a TRACK.
        session.add(FileMetadata(id=uuid.uuid4(), file_id=file_id, duration=7200.0))
        tracklist = Tracklist(
            id=tracklist_id,
            external_id=external_id,
            source_url=f"https://www.1001tracklists.com/tracklist/{external_id}/x.html",
            file_id=file_id,
            status="approved",
            latest_version_id=version_id,
        )
        session.add(tracklist)
        session.add(TracklistVersion(id=version_id, tracklist_id=tracklist_id, version_number=1))
        await session.flush()
        session.add(TracklistTrack(version_id=version_id, position=1, artist="A", title="T"))
        await session.commit()

    # The cache key is the unique set's key, which the drain derives from the file itself. Read it
    # back from the queue rather than recomputing it here, so the test can never disagree with the
    # production key derivation.
    async with session_factory() as session:
        # The file is already tracklisted, so it only appears once the flag re-admits it -- which is
        # exactly what the refresh does. Flag, read the key, then unflag to restore the start state.
        session.add(TracklistPriorityFlag(file_id=file_id, created_at=datetime.now(UTC), updated_at=datetime.now(UTC)))
        await session.commit()
    async with session_factory() as session:
        queue = await build_drain_queue(session)
        matching = [entry for entry in queue.entries if any(m.file_id == file_id for m in entry.unique_set.members)]
        assert matching, "the seeded file must reach the drain funnel once re-admitted"
        set_key = matching[0].set_key
    async with session_factory() as session:
        await session.execute(delete(TracklistPriorityFlag).where(TracklistPriorityFlag.file_id == file_id))
        await record_outcome(
            session,
            set_key=set_key,
            query_text=derive_query(LIVE_SET_FILENAME).query,
            outcome=LookupOutcome.FOUND,
            external_id=external_id,
        )
        await session.commit()

    try:
        async with session_factory() as session:
            tracklist = (await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))).scalar_one()
        yield session_factory, file_id, tracklist
    finally:
        async with session_factory() as session:
            await session.execute(delete(TracklistPriorityFlag).where(TracklistPriorityFlag.file_id == file_id))
            await session.execute(delete(TracklistLookupCache).where(TracklistLookupCache.set_key == set_key))
            await session.execute(delete(TracklistTrack).where(TracklistTrack.version_id == version_id))
            await session.execute(delete(TracklistVersion).where(TracklistVersion.id == version_id))
            await session.execute(delete(Tracklist).where(Tracklist.id == tracklist_id))
            await session.execute(delete(FileMetadata).where(FileMetadata.file_id == file_id))
            await session.execute(delete(FileRecord).where(FileRecord.id == file_id))
            await session.commit()


async def test_an_answered_set_is_not_queued_until_a_refresh_asks_for_it(async_engine: AsyncEngine) -> None:
    """The baseline the refresh has to overcome, proved rather than assumed.

    A file with a tracklist is removed by the funnel's already-tracklisted filter BEFORE
    classification runs, and its set is separately suppressed by a positive cache row. Both gates
    are real, and the refresh has to clear both -- so this asserts the "before" state at the level
    that matters: the set is absent from ``DrainQueue.entries``.
    """
    async with _seeded(async_engine) as (session_factory, file_id, _tracklist):
        async with session_factory() as session:
            queue = await build_drain_queue(session)
        assert not any(any(m.file_id == file_id for m in entry.unique_set.members) for entry in queue.entries)


async def test_refresh_re_admits_the_set_and_the_drain_retires_the_request(async_engine: AsyncEngine) -> None:
    """The whole loop: refresh -> queued -> answered -> quiet again.

    The last step is not decoration. Since phaze-2akf a priority flag on an already-tracklisted file
    is the re-admission override, a flag left standing after the drain answers the set would
    re-queue it on every future pass forever -- a permanent re-fetch loop against a host budget of
    ~1 request / 8 s, which is the failure mode the retired cron had. ``persist_lookup`` clears the
    flag on a definitive outcome, and this asserts that it really does.
    """
    async with _seeded(async_engine) as (session_factory, file_id, tracklist):
        result = await refresh_tracklists(_ctx(session_factory), tracklist_ids=[str(tracklist.id)])
        assert result["refreshed"] == 1
        assert result["cache_entries_cleared"] == 1

        async with session_factory() as session:
            queue = await build_drain_queue(session)
        queued = [entry for entry in queue.entries if any(m.file_id == file_id for m in entry.unique_set.members)]
        assert queued, "a refreshed set must re-enter the drain queue"
        candidate: DrainCandidate = queued[0]
        assert candidate.flagged, "and it must sort to the front as an operator-flagged request"

        # The drain answers it. Any definitive outcome retires the request; NOT_FOUND is used here
        # because it writes no tracklist, which keeps this test about the flag rather than about
        # persistence (that is covered in tests/identify/services/test_tracklist_drain.py).
        attempt = _not_found_attempt(candidate)
        async with session_factory() as session:
            await persist_lookup(session, candidate, attempt)
            await session.commit()

        async with session_factory() as session:
            still_flagged = (await session.execute(select(TracklistPriorityFlag.file_id).where(TracklistPriorityFlag.file_id == file_id))).first()
            verdict = await lookup(session, candidate.set_key)
            queue_after = await build_drain_queue(session)

        assert still_flagged is None, "an answered request must not stay flagged"
        assert verdict.decision is CacheDecision.SUPPRESSED_NEGATIVE
        assert not any(any(m.file_id == file_id for m in entry.unique_set.members) for entry in queue_after.entries)


def _not_found_attempt(candidate: DrainCandidate) -> LookupAttempt:
    """Build the LookupAttempt a clean "1001TL has nothing for this set" lookup would produce."""
    return LookupAttempt(set_key=candidate.set_key, query_text=candidate.derived.query, outcome=LookupOutcome.NOT_FOUND)
