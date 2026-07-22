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
    async with ctx["async_session"]() as session:
        # Load tracklist
        result = await session.execute(select(Tracklist).where(Tracklist.id == uuid.UUID(tracklist_id)))
        tracklist = result.scalar_one_or_none()
        if tracklist is None:
            logger.info("discogs match completed", tracklist_id=tracklist_id, status="not_found")
            return {"tracklist_id": tracklist_id, "status": "not_found"}

        # Load tracks for latest version
        tracks_result = await session.execute(select(TracklistTrack).where(TracklistTrack.version_id == tracklist.latest_version_id))
        tracks = tracks_result.scalars().all()

        # Filter eligible tracks (both artist AND title non-null and non-empty per D-02)
        eligible = [t for t in tracks if t.artist and t.title]
        skipped = len(tracks) - len(eligible)

        # Delete old candidate links for eligible tracks (preserve accepted links -- pitfall 3)
        for track in eligible:
            await session.execute(
                delete(DiscogsLink).where(
                    DiscogsLink.track_id == track.id,
                    DiscogsLink.status == "candidate",
                )
            )

        # Create client and match with bounded concurrency. The client is owned by this
        # try/finally so close() always runs -- including on a DB error in the store loop
        # below or (belt-and-suspenders, now that search_releases degrades internally) any
        # exception that still escapes matching -- instead of only on the success path,
        # which previously leaked the httpx.AsyncClient and its connection pool on failure.
        client = DiscogsographyClient(base_url=settings.discogsography_url)
        try:
            semaphore = asyncio.Semaphore(settings.discogs_match_concurrency)

            async def _match_one(track: TracklistTrack) -> list[dict[str, Any]]:
                async with semaphore:
                    return await match_track_to_discogs(client, track)

            # Match all eligible tracks concurrently
            match_results = await asyncio.gather(*[_match_one(t) for t in eligible])

            # Store candidates
            candidates_created = 0
            for track, candidates in zip(eligible, match_results, strict=True):
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
        finally:
            await client.close()

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
