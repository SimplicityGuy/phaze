"""The propagation schema against REAL Postgres DDL (phaze-fq9h.7).

Migration 051 narrows ``UNIQUE (external_id)`` to a PARTIAL unique index over canonical rows so a
tracklist can be projected onto a unique set's duplicate files. A partial index is exactly the kind
of constraint that a metadata-level assertion cannot check: whether the predicate is right, and
whether it still rejects what it used to reject, is a question only the database can answer.

Three things are pinned here, all of which fail SILENTLY rather than loudly if they regress:

1. **Two canonical rows for one page are still refused.** That was the whole content of the old
   global UNIQUE, and losing it would let the legacy scrape path and the drain each create their
   own "the" row for a page, with the file link on whichever one a given query happened to find.
2. **A canonical row plus its projections is accepted.** Without this the drain cannot propagate at
   all, and every duplicate file costs a fresh host request against a ~1-per-8s budget.
3. **The correction path works.** Dedup is heuristic now that fingerprinting is gone (phaze-0jpe),
   so a false merge is possible and must be reversible: deleting on ``propagated_from_set_key``
   must remove exactly the cluster's projections and leave the canonical scrape untouched.

Plus the canonical-resolution checks. Every consumer that resolves a page by ``external_id`` must
carry the ``propagated_from_set_key IS NULL`` filter; once a projection exists, an unfiltered
``scalar_one_or_none()`` raises ``MultipleResultsFound`` and takes its caller down with it.

phaze-2akf: those checks used to run against ``tasks/tracklist.py``'s legacy store/cache helpers,
which are retired with the legacy scrape path. They now run against the two consumers that remain
-- the drain's ``_resolve_canonical`` and the on-demand refresh's ``_resolve_targets`` -- which is
where the invariant now has to hold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.enums.tracklist_candidate import DuplicateConfidence, LookupOutcome
from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


pytestmark = pytest.mark.integration


SET_KEY = "a" * 64


def _row(external_id: str, *, set_key: str | None = None, file_id: uuid.UUID | None = None) -> Tracklist:
    return Tracklist(
        id=uuid.uuid4(),
        external_id=external_id,
        source_url="https://www.1001tracklists.com/tracklist/x/y.html",
        file_id=file_id,
        status="approved",
        propagated_from_set_key=set_key,
        propagation_confidence=DuplicateConfidence.EXACT.value if set_key else None,
    )


async def _seed_file(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """Insert a real ``files`` row -- ``tracklists.file_id`` carries a FK, so a bare uuid4 fails."""
    file_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            FileRecord(
                id=file_id,
                agent_id="test-fileserver",
                sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                original_path=f"/music/{file_id}/set.mp3",
                original_filename="set.mp3",
                current_path=f"/music/{file_id}.mp3",
                file_type="mp3",
                file_size=1_000_000,
            )
        )
        await session.commit()
    return file_id


async def _cleanup_files(factory: async_sessionmaker[AsyncSession], file_ids: list[uuid.UUID]) -> None:
    async with factory() as session:
        await session.execute(delete(FileRecord).where(FileRecord.id.in_(file_ids)))
        await session.commit()


async def _cleanup(factory: async_sessionmaker[AsyncSession], external_id: str) -> None:
    async with factory() as session:
        ids = (await session.execute(select(Tracklist.id).where(Tracklist.external_id == external_id))).scalars().all()
        versions = (await session.execute(select(TracklistVersion.id).where(TracklistVersion.tracklist_id.in_(ids)))).scalars().all()
        await session.execute(delete(TracklistTrack).where(TracklistTrack.version_id.in_(versions)))
        await session.execute(delete(TracklistVersion).where(TracklistVersion.id.in_(versions)))
        await session.execute(delete(Tracklist).where(Tracklist.external_id == external_id))
        await session.commit()


async def test_two_canonical_rows_for_one_page_are_still_refused(async_engine: AsyncEngine) -> None:
    """The old global UNIQUE's real content survives: one scraped row per 1001TL page."""
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    external_id = f"dup-{uuid.uuid4().hex[:10]}"
    try:
        async with factory() as session:
            session.add(_row(external_id))
            await session.commit()

        with pytest.raises(IntegrityError):
            async with factory() as session:
                session.add(_row(external_id))
                await session.commit()
    finally:
        await _cleanup(factory, external_id)


async def test_one_canonical_row_may_carry_many_projections(async_engine: AsyncEngine) -> None:
    """The propagation the whole drain rests on: same page id, one scrape, N duplicate files."""
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    external_id = f"prop-{uuid.uuid4().hex[:10]}"
    try:
        async with factory() as session:
            session.add(_row(external_id))
            for _ in range(3):
                session.add(_row(external_id, set_key=SET_KEY))
            await session.commit()

        async with factory() as session:
            rows = (await session.execute(select(Tracklist).where(Tracklist.external_id == external_id))).scalars().all()
        assert len(rows) == 4
        assert sum(1 for r in rows if r.propagated_from_set_key is None) == 1
    finally:
        await _cleanup(factory, external_id)


async def test_a_false_merge_is_reversible_by_deleting_on_the_set_key(async_engine: AsyncEngine) -> None:
    """The correction handle. Without it a wrong cluster is indistinguishable from a real result."""
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    external_id = f"undo-{uuid.uuid4().hex[:10]}"
    other_key = "b" * 64
    try:
        async with factory() as session:
            session.add(_row(external_id))
            session.add(_row(external_id, set_key=SET_KEY))
            session.add(_row(external_id, set_key=other_key))
            await session.commit()

        async with factory() as session:
            await session.execute(delete(Tracklist).where(Tracklist.propagated_from_set_key == SET_KEY))
            await session.commit()

        async with factory() as session:
            rows = (await session.execute(select(Tracklist).where(Tracklist.external_id == external_id))).scalars().all()
        keys = sorted(str(r.propagated_from_set_key) for r in rows)
        assert keys == ["None", other_key], "only the suspect cluster is removed; the scrape survives"
    finally:
        await _cleanup(factory, external_id)


async def test_the_drain_resolves_the_canonical_row_when_a_projection_exists(async_engine: AsyncEngine) -> None:
    """``_resolve_canonical`` must pick THE scraped row, not raise and not pick a projection.

    This is the phaze-fq9h.7 invariant at the point where it is easiest to break: a fresh lookup for
    a page that has already been propagated. Unfiltered, the ``scalar_one_or_none()`` inside raises
    ``MultipleResultsFound`` the moment a second row shares the external_id -- which is every
    re-lookup of every duplicated set.
    """
    from phaze.services.tracklist_drain import LookupAttempt, _resolve_canonical

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    external_id = f"canon-{uuid.uuid4().hex[:10]}"
    preferred_file_id = await _seed_file(factory)
    try:
        async with factory() as session:
            session.add(_row(external_id))
            session.add(_row(external_id, set_key=SET_KEY))
            await session.commit()

        attempt = LookupAttempt(
            set_key=SET_KEY,
            query_text="q",
            outcome=LookupOutcome.FOUND,
            external_id=external_id,
            source_url="https://www.1001tracklists.com/tracklist/x/y.html",
        )
        async with factory() as session:
            resolved = await _resolve_canonical(session, attempt=attempt, preferred_file_id=preferred_file_id)
            await session.commit()
        assert resolved.propagated_from_set_key is None, "a re-lookup must update the CANONICAL row"
    finally:
        await _cleanup(factory, external_id)
        await _cleanup_files(factory, [preferred_file_id])


async def test_the_refresh_sweep_resolves_only_the_canonical_row(async_engine: AsyncEngine) -> None:
    """``_resolve_targets`` never hands a projection back as a refresh target (phaze-2akf).

    Refreshing a projection directly would either re-fetch the same page once per duplicate -- N
    requests against a whole-host budget of ~1 per 8 s, for bytes the canonical row's own re-read
    already fetched -- or leave the canonical row untouched while reporting success.
    """
    from phaze.tasks.tracklist import _resolve_targets

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    external_id = f"target-{uuid.uuid4().hex[:10]}"
    file_id = await _seed_file(factory)
    try:
        async with factory() as session:
            canonical = _row(external_id)
            projection = _row(external_id, set_key=SET_KEY, file_id=file_id)
            session.add(canonical)
            session.add(projection)
            await session.commit()
            canonical_id, projection_id = canonical.id, projection.id

        async with factory() as session:
            # Naming the projection by id resolves to NOTHING...
            assert await _resolve_targets(session, [projection_id], []) == []
            # ...and naming the duplicate's FILE resolves to nothing either, because the projection
            # is what carries that file_id. The canonical row is reached by its own id.
            assert await _resolve_targets(session, [], [file_id]) == []
            targets = await _resolve_targets(session, [canonical_id], [])
        assert [t.id for t in targets] == [canonical_id]
    finally:
        await _cleanup(factory, external_id)
        await _cleanup_files(factory, [file_id])


async def test_a_canonical_scoped_sweep_selects_one_row_per_page(async_engine: AsyncEngine) -> None:
    """N projections of one page share its source_url -- acting on them is N reads for one page.

    At a whole-host budget of ~1 request / 8 s that is not a micro-optimisation: a widely-duplicated
    set would spend its cluster size in requests to fetch bytes the canonical row already covers.
    The monthly cron this predicate was written for is gone (phaze-2akf), but the predicate SHAPE --
    source allowlist + canonical-only -- is exactly what ``_resolve_targets`` now carries, so the
    DDL-level assertion that it selects one row per page is kept.
    """
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    external_id = f"refresh-{uuid.uuid4().hex[:8]}"
    try:
        async with factory() as session:
            session.add(_row(external_id))
            session.add(_row(external_id, set_key=SET_KEY))
            await session.commit()

        async with factory() as session:
            # The refresh sweep's own predicate: source allowlist + canonical + (unlinked | stale).
            selected = (
                (
                    await session.execute(
                        select(Tracklist).where(
                            Tracklist.external_id == external_id,
                            Tracklist.source == "1001tracklists",
                            Tracklist.propagated_from_set_key.is_(None),
                            Tracklist.file_id.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(selected) == 1
        assert selected[0].propagated_from_set_key is None
    finally:
        await _cleanup(factory, external_id)
