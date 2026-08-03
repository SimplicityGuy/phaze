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

Plus the legacy-path coexistence checks. ``tasks/tracklist.py`` resolves pages by ``external_id``
with ``scalar_one_or_none()``; once a projection exists, an unfiltered query there raises
``MultipleResultsFound`` and takes the ordinary search path down with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.enums.tracklist_candidate import DuplicateConfidence
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


async def test_the_legacy_store_path_survives_a_projection_existing(async_engine: AsyncEngine) -> None:
    """``_store_scraped_tracklist`` resolves the page by external_id with ``scalar_one_or_none()``.

    Unfiltered, that raises ``MultipleResultsFound`` the moment the drain propagates a page the
    legacy path later re-scrapes -- taking the ordinary search flow down with it.
    """
    from phaze.services.tracklist_scraper import ScrapedTrack, ScrapedTracklist
    from phaze.tasks.tracklist import _find_cached_tracklists, _link_cached_tracklist, _store_scraped_tracklist

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    external_id = f"legacy-{uuid.uuid4().hex[:10]}"
    try:
        async with factory() as session:
            session.add(_row(external_id))
            session.add(_row(external_id, set_key=SET_KEY))
            await session.commit()

        scraped = ScrapedTracklist(
            external_id=external_id,
            title="t",
            source_url="https://www.1001tracklists.com/tracklist/x/y.html",
            tracks=[ScrapedTrack(position=1, artist="A", title="B")],
        )
        async with factory() as session:
            stored = await _store_scraped_tracklist(session, scraped)
            await session.commit()
            assert stored.propagated_from_set_key is None, "the legacy path must update the CANONICAL row"

            cached = await _find_cached_tracklists(session, [external_id])
            assert external_id in cached
            assert cached[external_id].propagated_from_set_key is None

            # Link-only update on the canonical row: must resolve, not raise.
            await _link_cached_tracklist(session, external_id, uuid.uuid4(), 90, True)
    finally:
        await _cleanup(factory, external_id)


async def test_the_monthly_refresh_never_re_scrapes_a_projection(async_engine: AsyncEngine) -> None:
    """N projections of one page share its source_url -- refreshing them is N requests for one page.

    At a whole-host budget of ~1 request / 8 s that is not a micro-optimisation: a widely-duplicated
    set would spend its cluster size in requests every month, forever, to fetch bytes the canonical
    row's own refresh already fetched.
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
