"""SAQ task functions for 1001Tracklists search, scrape, and refresh."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import random
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
import structlog

from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.tracklist_matcher import compute_match_confidence, parse_live_set_filename, should_auto_link
from phaze.services.tracklist_scraper import ScrapedTracklist, TracklistScraper


logger = structlog.get_logger(__name__)


class EmptyScrapeError(RuntimeError):
    """Raised when a re-scrape yields zero tracks for a tracklist that already has data.

    Signals a failed/blocked scrape so SAQ retries instead of silently overwriting good
    tracklist data with an empty version (phaze-gfyr).
    """

    def __init__(self, external_id: str) -> None:
        super().__init__(f"Refusing to overwrite tracklist {external_id!r} with an empty re-scrape")
        self.external_id = external_id


def _parse_scraped_date(raw: str | None) -> date | None:
    """Parse a scraped date string into a ``date``, trying the known 1001Tracklists formats.

    Returns ``None`` when there is no date or none of the formats match. Shared by the auto-link
    scorer (``search_tracklist``) and the store path so the SAME date signal feeds both the
    Pitfall-3 date-mismatch cap and the persisted ``Tracklist.date`` (phaze-rkxy).
    """
    if not raw:
        return None
    try:
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%B %d, %Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    except Exception:
        logger.debug("Could not parse date: %s", raw)
    return None


async def _latest_version_has_tracks(session: Any, tracklist: Any) -> bool:
    """Return True if the tracklist's current latest version has at least one track."""
    if tracklist.latest_version_id is None:
        return False
    result = await session.execute(select(func.count()).select_from(TracklistTrack).where(TracklistTrack.version_id == tracklist.latest_version_id))
    return (result.scalar() or 0) > 0


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
    # Serialize concurrent upserts keyed on external_id (phaze-5vmt). Two scrape jobs for
    # different files can resolve to the SAME external_id and race this check-then-act
    # read-modify-write: both could INSERT the same external_id (UNIQUE violation) or both read
    # the same max(version_number) and write duplicate versions, orphaning one version's tracks.
    # A transaction-scoped advisory lock on hashtext(external_id) makes the upsert atomic without
    # taking a row lock (the row may not exist yet on the insert path). It is released on commit.
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(scraped.external_id))))

    # Check for existing tracklist by external_id
    result = await session.execute(select(Tracklist).where(Tracklist.external_id == scraped.external_id))
    tracklist = result.scalar_one_or_none()

    # Parse date string to date object (shared helper -- see search_tracklist scorer, phaze-rkxy)
    tracklist_date = _parse_scraped_date(scraped.date)

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
        # phaze-gfyr: a failed/bot-blocked re-scrape parses to zero tracks and None metadata.
        # If the existing tracklist already has a non-empty latest version, refuse to overwrite
        # it with an empty version — raise so SAQ retries instead of silently destroying data.
        if not scraped.tracks and await _latest_version_has_tracks(session, tracklist):
            logger.warning(
                "Refusing empty re-scrape over existing tracklist data",
                external_id=scraped.external_id,
                tracklist_id=str(tracklist.id),
            )
            raise EmptyScrapeError(scraped.external_id)
        # Update metadata — but never null out good values when the scrape produced nothing
        # (phaze-gfyr): only overwrite fields the scrape actually resolved.
        if scraped.artist is not None:
            tracklist.artist = scraped.artist
        if scraped.event is not None:
            tracklist.event = scraped.event
        if tracklist_date is not None:
            tracklist.date = tracklist_date
        tracklist.source_url = scraped.source_url
        # Get next version number
        version_result = await session.execute(
            select(TracklistVersion).where(TracklistVersion.tracklist_id == tracklist.id).order_by(TracklistVersion.version_number.desc()).limit(1)
        )
        latest = version_result.scalar_one_or_none()
        next_version = (latest.version_number + 1) if latest else 1

    # Set file linkage -- but NEVER steal a tracklist already owned by a DIFFERENT file (phaze-4a5w).
    # This archive holds duplicate copies of the same live set (dedup is a core feature), so two
    # files can resolve to the same external_id. Assigning file_id unconditionally here let a later
    # file's auto-link silently flip an existing tracklist's file_id -- including one a human had
    # MANUALLY accepted (auto_linked=False) -- stamping auto_linked=True over the manual provenance
    # and vanishing the tracklist from the original file's every view with no audit trail. Only take
    # the link when the row is unowned (file_id None) or already points at this same file; otherwise
    # log and leave the existing link intact for manual review.
    if file_id is not None:
        if tracklist.file_id is None or tracklist.file_id == file_id:
            tracklist.file_id = file_id
            tracklist.match_confidence = confidence
            tracklist.auto_linked = auto_linked
        else:
            logger.warning(
                "Refusing to steal tracklist already linked to another file",
                external_id=scraped.external_id,
                tracklist_id=str(tracklist.id),
                existing_file_id=str(tracklist.file_id),
                candidate_file_id=str(file_id),
            )

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


async def search_tracklist(ctx: dict[str, Any], *, file_id: str) -> dict[str, Any]:
    """Search 1001Tracklists for a file and store/link matching results.

    Per D-16: parse filename first, fall back to FileMetadata tags.
    Per D-14: auto-link if confidence >= 90.
    Retries with exponential backoff are handled by SAQ queue configuration.
    """
    logger.info("tracklist search started", file_id=file_id)
    async with ctx["async_session"]() as session:
        # Load file with metadata
        result = await session.execute(select(FileRecord).options(selectinload(FileRecord.file_metadata)).where(FileRecord.id == uuid.UUID(file_id)))
        file_record = result.scalar_one_or_none()
        if file_record is None:
            logger.info("tracklist search completed", file_id=file_id, status="not_found", results_found=0)
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
            logger.info("tracklist search completed", file_id=file_id, status="no_query", results_found=0)
            return {"file_id": file_id, "results_found": 0, "auto_linked": False, "status": "no_query"}

        # Search and scrape
        scraper = TracklistScraper()
        try:
            results = await scraper.search(query)
            any_auto_linked = False

            for search_result in results:
                scraped = await scraper.scrape_tracklist(search_result.url)
                # phaze-rkxy: pass the scraped date so the Pitfall-3 date-mismatch cap actually
                # fires in the auto-link path. Hardcoding None here made the cap dead and let a
                # wrong-date tracklist auto-link on artist+event alone.
                scraped_date = _parse_scraped_date(scraped.date)
                confidence = compute_match_confidence(
                    tracklist_artist=scraped.artist,
                    tracklist_event=scraped.event,
                    tracklist_date=scraped_date,
                    file_artist=file_artist,
                    file_event=file_event,
                    file_date=file_date,
                )

                # phaze-rkxy: an auto-link MUST be corroborated by a confirmed same-window date.
                # compute_match_confidence's Pitfall-3 cap only fires when BOTH dates are present, so
                # guard the remaining holes here -- a missing scraped date, a missing file date (the
                # metadata-fallback path, where file_event is also None), or a >3-day gap. Without
                # this, a perfect artist+event match (score 100) auto-links a wrong-date tracklist
                # with zero date corroboration, exactly the false auto-link the cap was meant to block.
                date_confirmed = scraped_date is not None and file_date is not None and abs((scraped_date - file_date).days) <= 3
                auto_link = should_auto_link(confidence) and date_confirmed
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
            logger.info(
                "tracklist search completed",
                file_id=file_id,
                results_found=len(results),
                auto_linked=any_auto_linked,
            )
            return {"file_id": file_id, "results_found": len(results), "auto_linked": any_auto_linked}
        finally:
            await scraper.close()


async def scrape_and_store_tracklist(ctx: dict[str, Any], *, tracklist_id: str) -> dict[str, Any]:
    """Re-scrape an existing tracklist and create a new version.

    Used for manual re-scrape action and refresh jobs.
    Retries with exponential backoff are handled by SAQ queue configuration.
    """
    logger.info("tracklist scrape started", tracklist_id=tracklist_id)
    async with ctx["async_session"]() as session:
        result = await session.execute(select(Tracklist).where(Tracklist.id == uuid.UUID(tracklist_id)))
        tracklist = result.scalar_one_or_none()
        if tracklist is None:
            logger.info("tracklist scrape completed", tracklist_id=tracklist_id, status="not_found", tracks_found=0)
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

            logger.info(
                "tracklist scrape completed",
                tracklist_id=tracklist_id,
                tracks_found=tracks_found,
                version=version_number,
            )
            return {"tracklist_id": tracklist_id, "tracks_found": tracks_found, "version": version_number}
        finally:
            await scraper.close()


async def refresh_tracklists(ctx: dict[str, Any]) -> dict[str, Any]:
    """Refresh stale and unresolved tracklists.

    Per D-10: find tracklists where file_id IS NULL (unresolved) or updated_at < 90 days ago (stale).
    Per TL-04: add randomized jitter between scrapes (60-300 seconds).
    """
    # phaze-xpzp: bind a NAIVE threshold. ``tracklists.updated_at`` (TimestampMixin) is a
    # ``TIMESTAMP WITHOUT TIME ZONE`` column; asyncpg's naive-timestamp codec raises DataError
    # ("can't subtract offset-naive and offset-aware datetimes") at bind-encode time when handed a
    # tz-aware datetime, which previously made every monthly run fail on the SELECT below.
    stale_threshold = (datetime.now(tz=UTC) - timedelta(days=90)).replace(tzinfo=None)
    refreshed = 0
    errors = 0

    # phaze-xpzp: the query is split out of the per-tracklist loop's try/except so a query failure
    # (e.g. a bad bind, a connection drop) is reported in ``errors`` instead of being swallowed by a
    # broad ``except Exception`` into the untouched ``{"refreshed": 0, "errors": 0}`` initial
    # counters -- a return value indistinguishable from "there was simply nothing to refresh", which
    # let SAQ mark the job successful while the cron silently never ran.
    try:
        async with ctx["async_session"]() as session:
            result = await session.execute(select(Tracklist).where((Tracklist.file_id.is_(None)) | (Tracklist.updated_at < stale_threshold)))
            tracklists = list(result.scalars().all())
    except Exception:
        logger.exception("Error querying stale/unresolved tracklists")
        return {"refreshed": 0, "errors": 1}

    for tl in tracklists:
        try:
            await scrape_and_store_tracklist(ctx, tracklist_id=str(tl.id))
            refreshed += 1
        except Exception:
            logger.warning("Failed to refresh tracklist %s", tl.id, exc_info=True)
            errors += 1

        # Randomized jitter between scrapes (per D-10, TL-04)
        await asyncio.sleep(random.uniform(60, 300))  # noqa: S311  # nosec B311

    return {"refreshed": refreshed, "errors": errors}
