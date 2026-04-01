"""arq task functions for 1001Tracklists search, scrape, and refresh."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from arq import Retry
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.tracklist_matcher import compute_match_confidence, parse_live_set_filename, should_auto_link
from phaze.services.tracklist_scraper import ScrapedTracklist, TracklistScraper


logger = logging.getLogger(__name__)


async def _store_scraped_tracklist(
    session: Any,
    scraped: ScrapedTracklist,
    file_id: uuid.UUID | None = None,
    confidence: int | None = None,
    auto_linked: bool = False,
) -> Any:
    """Upsert a Tracklist record and create a new version with tracks.

    If a Tracklist with the same external_id exists, update it and add a new version.
    Otherwise, create a new Tracklist.
    """
    # Check for existing tracklist by external_id
    result = await session.execute(select(Tracklist).where(Tracklist.external_id == scraped.external_id))
    tracklist = result.scalar_one_or_none()

    # Parse date string to date object
    tracklist_date = None
    if scraped.date:
        try:
            # Try common date formats
            for fmt in ("%Y-%m-%d", "%d %b %Y", "%B %d, %Y", "%m/%d/%Y"):
                try:
                    tracklist_date = datetime.strptime(scraped.date, fmt).date()  # noqa: DTZ007
                    break
                except ValueError:
                    continue
        except Exception:
            logger.debug("Could not parse date: %s", scraped.date)

    if tracklist is None:
        tracklist = Tracklist(
            external_id=scraped.external_id,
            source_url=scraped.source_url,
            artist=scraped.artist,
            event=scraped.event,
            date=tracklist_date,
        )
        session.add(tracklist)
        await session.flush()
        next_version = 1
    else:
        # Update metadata
        tracklist.artist = scraped.artist
        tracklist.event = scraped.event
        tracklist.date = tracklist_date
        tracklist.source_url = scraped.source_url
        # Get next version number
        version_result = await session.execute(
            select(TracklistVersion).where(TracklistVersion.tracklist_id == tracklist.id).order_by(TracklistVersion.version_number.desc()).limit(1)
        )
        latest = version_result.scalar_one_or_none()
        next_version = (latest.version_number + 1) if latest else 1

    # Set file linkage
    if file_id is not None:
        tracklist.file_id = file_id
        tracklist.match_confidence = confidence
        tracklist.auto_linked = auto_linked

    # Create new version
    version = TracklistVersion(
        tracklist_id=tracklist.id,
        version_number=next_version,
    )
    session.add(version)
    await session.flush()

    tracklist.latest_version_id = version.id

    # Create track rows
    for track_data in scraped.tracks:
        track = TracklistTrack(
            version_id=version.id,
            position=track_data.position,
            artist=track_data.artist,
            title=track_data.title,
            label=track_data.label,
            timestamp=track_data.timestamp,
            is_mashup=track_data.is_mashup,
            remix_info=track_data.remix_info,
        )
        session.add(track)

    return tracklist


async def search_tracklist(ctx: dict[str, Any], file_id: str) -> dict[str, Any]:
    """Search 1001Tracklists for a file and store/link matching results.

    Per D-16: parse filename first, fall back to FileMetadata tags.
    Per D-14: auto-link if confidence >= 90.
    """
    try:
        async with ctx["async_session"]() as session:
            # Load file with metadata
            result = await session.execute(
                select(FileRecord).options(selectinload(FileRecord.file_metadata)).where(FileRecord.id == uuid.UUID(file_id))
            )
            file_record = result.scalar_one_or_none()
            if file_record is None:
                return {"file_id": file_id, "results_found": 0, "auto_linked": False, "status": "not_found"}

            # Parse filename for artist/event/date signals
            parsed = parse_live_set_filename(file_record.original_filename)
            file_artist: str | None = None
            file_event: str | None = None
            file_date = None

            if parsed:
                file_artist, file_event, file_date = parsed
            elif file_record.file_metadata:
                file_artist = file_record.file_metadata.artist
                file_event = None  # No event info from tags

            # Build search query
            if file_artist and file_event:
                query = f"{file_artist} {file_event}"
            elif file_artist:
                query = file_artist
            else:
                return {"file_id": file_id, "results_found": 0, "auto_linked": False, "status": "no_query"}

            # Search and scrape
            scraper = TracklistScraper()
            try:
                results = await scraper.search(query)
                any_auto_linked = False

                for search_result in results:
                    scraped = await scraper.scrape_tracklist(search_result.url)
                    confidence = compute_match_confidence(
                        tracklist_artist=scraped.artist,
                        tracklist_event=scraped.event,
                        tracklist_date=None,  # Date parsing happens in store
                        file_artist=file_artist,
                        file_event=file_event,
                        file_date=file_date,
                    )

                    auto_link = should_auto_link(confidence)
                    if auto_link:
                        any_auto_linked = True

                    await _store_scraped_tracklist(
                        session,
                        scraped,
                        file_id=uuid.UUID(file_id) if auto_link else None,
                        confidence=confidence if auto_link else None,
                        auto_linked=auto_link,
                    )

                await session.commit()
                return {"file_id": file_id, "results_found": len(results), "auto_linked": any_auto_linked}
            finally:
                await scraper.close()

    except Exception as exc:
        raise Retry(defer=ctx["job_try"] * 10) from exc


async def scrape_and_store_tracklist(ctx: dict[str, Any], tracklist_id: str) -> dict[str, Any]:
    """Re-scrape an existing tracklist and create a new version.

    Used for manual re-scrape action and refresh jobs.
    """
    try:
        async with ctx["async_session"]() as session:
            result = await session.execute(select(Tracklist).where(Tracklist.id == uuid.UUID(tracklist_id)))
            tracklist = result.scalar_one_or_none()
            if tracklist is None:
                return {"tracklist_id": tracklist_id, "tracks_found": 0, "version": 0, "status": "not_found"}

            scraper = TracklistScraper()
            try:
                scraped = await scraper.scrape_tracklist(tracklist.source_url)
                await _store_scraped_tracklist(session, scraped)
                await session.commit()

                # Get the version number we just created
                version_result = await session.execute(
                    select(TracklistVersion)
                    .where(TracklistVersion.tracklist_id == tracklist.id)
                    .order_by(TracklistVersion.version_number.desc())
                    .limit(1)
                )
                latest = version_result.scalar_one_or_none()
                version_number = latest.version_number if latest else 0
                tracks_found = len(scraped.tracks)

                return {"tracklist_id": tracklist_id, "tracks_found": tracks_found, "version": version_number}
            finally:
                await scraper.close()

    except Exception as exc:
        raise Retry(defer=ctx["job_try"] * 10) from exc


async def refresh_tracklists(ctx: dict[str, Any]) -> dict[str, Any]:
    """Refresh stale and unresolved tracklists.

    Per D-10: find tracklists where file_id IS NULL (unresolved) or updated_at < 90 days ago (stale).
    Per TL-04: add randomized jitter between scrapes (60-300 seconds).
    """
    stale_threshold = datetime.now(tz=timezone.utc) - timedelta(days=90)
    refreshed = 0
    errors = 0

    try:
        async with ctx["async_session"]() as session:
            result = await session.execute(
                select(Tracklist).where(
                    (Tracklist.file_id.is_(None)) | (Tracklist.updated_at < stale_threshold)
                )
            )
            tracklists = list(result.scalars().all())

        for tl in tracklists:
            try:
                await scrape_and_store_tracklist(ctx, str(tl.id))
                refreshed += 1
            except Exception:
                logger.warning("Failed to refresh tracklist %s", tl.id, exc_info=True)
                errors += 1

            # Randomized jitter between scrapes (per D-10, TL-04)
            await asyncio.sleep(random.uniform(60, 300))  # noqa: S311

    except Exception:
        logger.exception("Error during tracklist refresh")

    return {"refreshed": refreshed, "errors": errors}
