"""SAQ task for matching tracklist tracks to Discogs releases."""

from __future__ import annotations

import asyncio
from typing import Any
import uuid

from sqlalchemy import delete, select
import structlog

from phaze.config import settings
from phaze.models.discogs_link import DiscogsLink
from phaze.models.tracklist import Tracklist, TracklistTrack
from phaze.services.discogs_matcher import DiscogsographyClient, match_track_to_discogs


logger = structlog.get_logger(__name__)


async def match_tracklist_to_discogs(ctx: dict[str, Any], *, tracklist_id: str) -> dict[str, Any]:
    """Match all eligible tracks in a tracklist to Discogs releases.

    For each track with non-null artist AND title:
    1. Delete existing 'candidate' DiscogsLink rows (preserve 'accepted' links)
    2. Search discogsography API for matches
    3. Store top 3 candidates as DiscogsLink rows

    Uses asyncio.Semaphore to bound concurrent requests per discogs_match_concurrency setting.
    """
    logger.info("discogs match started", tracklist_id=tracklist_id)

    # phaze-xdu1: the OLD shape opened ONE transaction that DELETEd every candidate link, then held
    # it open across the asyncio.gather of network calls to discogsography (seconds-to-minutes for a
    # large tracklist), committing only at the end. Those uncommitted DELETEs row-locked the candidate
    # rows for the whole match run, so a concurrent accept/dismiss/bulk-link request blocked on the
    # row lock for the task's lifetime and then 500'd with StaleDataError when the task's DELETE
    # finally committed the row out from under it. Split into read -> network -> short write phases so
    # no DB connection (and no candidate row lock) is held across the network gather.

    # 1. Load the tracklist + eligible tracks in a short session, then release the connection. The
    #    loaded column attributes (id/artist/title) stay readable on the detached instances after close.
    async with ctx["async_session"]() as session:
        result = await session.execute(select(Tracklist).where(Tracklist.id == uuid.UUID(tracklist_id)))
        tracklist = result.scalar_one_or_none()
        if tracklist is None:
            logger.info("discogs match completed", tracklist_id=tracklist_id, status="not_found")
            return {"tracklist_id": tracklist_id, "status": "not_found"}

        tracks_result = await session.execute(select(TracklistTrack).where(TracklistTrack.version_id == tracklist.latest_version_id))
        tracks = tracks_result.scalars().all()

        # Filter eligible tracks (both artist AND title non-null and non-empty per D-02)
        eligible = [t for t in tracks if t.artist and t.title]
        skipped = len(tracks) - len(eligible)

    # 2. Match all eligible tracks concurrently with NO DB connection held (phaze-xdu1). The client is
    #    owned by this try/finally so close() always runs -- including on any exception that escapes
    #    matching -- instead of only on the success path, which previously leaked the httpx.AsyncClient.
    client = DiscogsographyClient(base_url=settings.discogsography_url)
    try:
        semaphore = asyncio.Semaphore(settings.discogs_match_concurrency)

        async def _match_one(track: TracklistTrack) -> list[dict[str, Any]]:
            async with semaphore:
                return await match_track_to_discogs(client, track)

        match_results = await asyncio.gather(*[_match_one(t) for t in eligible])
    finally:
        await client.close()

    # 3. Re-open a short transaction to swap candidates atomically: DELETE the old candidate links for
    #    the eligible tracks (preserve accepted links -- pitfall 3) and INSERT the fresh ones, committed
    #    together. No network runs in this span, so the candidate row locks are held only briefly --
    #    a concurrent accept/dismiss sees either the old or the new candidate set, never a lock held
    #    across the whole match run (phaze-xdu1).
    candidates_created = 0
    async with ctx["async_session"]() as session:
        for track, candidates in zip(eligible, match_results, strict=True):
            await session.execute(
                delete(DiscogsLink).where(
                    DiscogsLink.track_id == track.id,
                    DiscogsLink.status == "candidate",
                )
            )
            for candidate in candidates:
                link = DiscogsLink(
                    track_id=track.id,
                    discogs_release_id=candidate["discogs_release_id"],
                    discogs_artist=candidate.get("discogs_artist"),
                    discogs_title=candidate.get("discogs_title"),
                    discogs_label=candidate.get("discogs_label"),
                    discogs_year=candidate.get("discogs_year"),
                    confidence=candidate["confidence"],
                    status="candidate",
                )
                session.add(link)
                candidates_created += 1

        await session.commit()

    logger.info(
        "discogs match completed",
        tracklist_id=tracklist_id,
        tracks_matched=len(eligible),
        tracks_skipped=skipped,
        candidates_created=candidates_created,
    )

    return {
        "tracklist_id": tracklist_id,
        "tracks_matched": len(eligible),
        "tracks_skipped": skipped,
        "candidates_created": candidates_created,
    }
