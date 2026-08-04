"""SAQ task functions for 1001Tracklists search, scrape, and refresh."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import random
from typing import Any
import uuid

from sqlalchemy import exists, func, select
from sqlalchemy.orm import selectinload
import structlog

from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.text_repair import repair_mojibake
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


async def _find_cached_tracklists(session: Any, external_ids: list[str]) -> dict[str, Tracklist]:
    """Load SUCCESSFULLY-scraped Tracklist rows for these external_ids, keyed by external_id.

    The persistent half of "cached and never re-fetched" (phaze-hu8v): a search result whose
    external_id already resolves to a Tracklist whose latest version has at least one track has
    already been scraped successfully, in this run or a prior one -- a tracklist for a past event
    doesn't change, so re-fetching its detail page over the network is a request the site's
    Crawl-delay-8 budget never needed to pay.

    A resolved ``latest_version_id`` alone is NOT sufficient and must not be used as the cache-hit
    condition: ``_store_scraped_tracklist``'s empty-rescrape guard (phaze-gfyr) only refuses an
    empty scrape when the tracklist ALREADY has tracks from a prior version -- a tracklist's FIRST
    scrape ever, if it soft-blocked/interstitial'd/hit selector drift and parsed to zero tracks,
    still creates a version with a non-null ``latest_version_id`` and zero tracks. Treating that as
    "cached" would permanently poison it: it would never be fetched again, in this run or any
    future one, silently converting a transient failure into permanent data loss (the review that
    caught this, phaze-hu8v changes-requested round). The EXISTS-track check below is exactly
    ``_latest_version_has_tracks``'s predicate, expressed as one bulk correlated subquery instead
    of N per-row round trips.

    phaze-fq9h.7: ``external_id`` is no longer globally unique -- the drain writes PROPAGATED
    projections of a page onto a unique set's duplicate files, and they share the page's id. Only
    canonical rows (``propagated_from_set_key IS NULL``) may key this dict; without the filter a
    projection could win the slot and this function would hand back a row whose file link belongs
    to a different file entirely.
    """
    if not external_ids:
        return {}
    result = await session.execute(
        select(Tracklist).where(
            Tracklist.external_id.in_(external_ids),
            Tracklist.propagated_from_set_key.is_(None),
            Tracklist.latest_version_id.is_not(None),
            exists(select(TracklistTrack.id).where(TracklistTrack.version_id == Tracklist.latest_version_id)),
        )
    )
    return {tl.external_id: tl for tl in result.scalars().all()}


def _apply_file_link(tracklist: Any, file_id: uuid.UUID, confidence: int | None, auto_linked: bool) -> None:
    """Link tracklist to file_id, unless it is already linked to a DIFFERENT file (phaze-4a5w).

    Shared by ``_store_scraped_tracklist`` (the fresh-scrape / re-scrape path) and
    ``_link_cached_tracklist`` (the cache-hit path, phaze-hu8v) -- both must honor the same
    "never steal a manual link" rule, so the check lives in exactly one place.
    """
    if tracklist.file_id is None or tracklist.file_id == file_id:
        tracklist.file_id = file_id
        tracklist.match_confidence = confidence
        tracklist.auto_linked = auto_linked
    else:
        logger.warning(
            "Refusing to steal tracklist already linked to another file",
            external_id=tracklist.external_id,
            tracklist_id=str(tracklist.id),
            existing_file_id=str(tracklist.file_id),
            candidate_file_id=str(file_id),
        )


async def _link_cached_tracklist(session: Any, external_id: str, file_id: uuid.UUID, confidence: int | None, auto_linked: bool) -> None:
    """Update file linkage on an ALREADY-scraped tracklist without creating a redundant version.

    Used when ``search_tracklist`` rediscovers (for a different file) a tracklist it has already
    scraped before (phaze-hu8v): the page's data doesn't change once published, so re-persisting an
    identical version on every rediscovery would just bloat tracklist_versions/tracklist_tracks for
    no benefit -- the only NEW information here is the file linkage itself.

    phaze-fq9h.7: scoped to the CANONICAL row. A bare ``external_id`` match now also returns the
    page's propagated projections (the drain writes one per duplicate file of a unique set), and
    ``scalar_one_or_none()`` would raise ``MultipleResultsFound`` on the first such projection.
    """
    result = await session.execute(select(Tracklist).where(Tracklist.external_id == external_id, Tracklist.propagated_from_set_key.is_(None)))
    tracklist = result.scalar_one_or_none()
    if tracklist is None:
        # Deleted between the cache-check read and this write -- rare race, nothing to link.
        logger.warning("Cached tracklist vanished before link-only update: %s", external_id)
        return
    _apply_file_link(tracklist, file_id, confidence, auto_linked)


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

    # Check for existing CANONICAL tracklist by external_id. phaze-fq9h.7 narrowed the UNIQUE on
    # external_id to a partial index over `propagated_from_set_key IS NULL`, because the drain
    # writes propagated projections of a page onto a unique set's duplicate files and they carry
    # the page's own id. Without this filter the query returns those projections too and
    # `scalar_one_or_none()` raises MultipleResultsFound -- and, worse, an unfiltered INSERT would
    # try to create a second canonical row for a page that already has one.
    result = await session.execute(select(Tracklist).where(Tracklist.external_id == scraped.external_id, Tracklist.propagated_from_set_key.is_(None)))
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
        _apply_file_link(tracklist, file_id, confidence, auto_linked)

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

    # phaze-1bcc: the OLD shape scraped-and-stored inside ONE transaction whose only commit was after
    # the whole result loop. _store_scraped_tracklist takes a transaction-scoped advisory lock on
    # hashtext(external_id) (phaze-5vmt), so after storing result #1 that lock was held across the
    # scrape of results #2..N -- each carrying a 2-5s rate-limit sleep + 30s-timeout HTTP fetch. That
    # (a) blocked any concurrent scrape touching an already-locked external_id for the whole remaining
    # loop while pinning a pooled connection, and (b) let two overlapping searches acquire shared locks
    # in opposite order -> a classic ABBA deadlock that Postgres aborts, discarding a whole batch.
    #
    # Restructure into three phases: (1) load the file + build the query in a short session, released
    # before any network I/O; (2) scrape ALL results with NO connection held; (3) store them in ONE
    # short transaction, in external_id-sorted order so every job acquires shared advisory locks in the
    # same order (no ABBA) and no lock is ever held across a network scrape.

    # 1. Load file + build query, then release the connection.
    async with ctx["async_session"]() as session:
        result = await session.execute(select(FileRecord).options(selectinload(FileRecord.file_metadata)).where(FileRecord.id == uuid.UUID(file_id)))
        file_record = result.scalar_one_or_none()
        if file_record is None:
            logger.info("tracklist search completed", file_id=file_id, status="not_found", results_found=0)
            return {"file_id": file_id, "results_found": 0, "auto_linked": False, "status": "not_found"}

        # Parse filename for artist/event/date signals. phaze-x4ux: repair mojibake on the file
        # side of the match BEFORE parsing/comparing -- FileRecord.original_filename is never
        # rewritten (it stays the byte-faithful on-disk record), so this repairs only the local
        # signal used for matching, not the stored column. `file_metadata.artist` is repaired
        # defensively too (metadata extraction already repairs new tags at ingest -- see
        # services/metadata.py::_first_str -- but this covers rows extracted before that fix
        # shipped; repair_mojibake is a no-op on already-clean text).
        parsed = parse_live_set_filename(repair_mojibake(file_record.original_filename))
        file_artist: str | None = None
        file_event: str | None = None
        file_date = None

        if parsed:
            file_artist, file_event, file_date = parsed
        elif file_record.file_metadata:
            file_artist = repair_mojibake(file_record.file_metadata.artist) if file_record.file_metadata.artist else None
            file_event = None  # No event info from tags

        # Build search query
        if file_artist and file_event:
            query = f"{file_artist} {file_event}"
        elif file_artist:
            query = file_artist
        else:
            logger.info("tracklist search completed", file_id=file_id, status="no_query", results_found=0)
            return {"file_id": file_id, "results_found": 0, "auto_linked": False, "status": "no_query"}

    # 2. Search + scrape every result with NO DB connection held (phaze-1bcc). Collect the scraped
    #    payloads plus their computed auto-link decision for the store phase.
    scraped_items: list[tuple[ScrapedTracklist, bool, int | None, bool]] = []
    scraper = TracklistScraper()
    try:
        results = await scraper.search(query)

        # phaze-hu8v: 1001Tracklists detail pages don't change once published -- before touching
        # the network again for ANY of these results, check which we've already scraped before (in
        # THIS run or a prior one). TracklistSearchResult.external_id is parsed straight from the
        # search-results href (see _parse_search_results), so this needs no scrape of its own. The
        # session is opened only for this one short bulk SELECT and closed immediately after, same
        # discipline as the file+query session above (phaze-1bcc) -- never held across a scrape.
        cached_by_external_id: dict[str, Tracklist] = {}
        if results:
            async with ctx["async_session"]() as session:
                cached_by_external_id = await _find_cached_tracklists(session, [r.external_id for r in results])

        for search_result in results:
            cached_tracklist = cached_by_external_id.get(search_result.external_id)
            from_cache = cached_tracklist is not None
            if cached_tracklist is not None:
                logger.debug("Skipping re-scrape of already-cached tracklist: %s", search_result.external_id)
                # Metadata was mojibake-repaired once at initial ingest (phaze-x4ux) and never
                # re-mutated after, so the stored values are already clean -- no repair needed here.
                # title="" is safe: Tracklist has no title column (it was never persisted from the
                # network scrape either), compute_match_confidence below scores only
                # artist/event/date and never reads title, and a cache-hit ScrapedTracklist never
                # reaches _store_scraped_tracklist (see the from_cache branch in the store loop
                # below), so this empty value is never written anywhere. Revisit if title ever
                # becomes a scoring signal.
                scraped = ScrapedTracklist(
                    external_id=cached_tracklist.external_id,
                    title="",
                    artist=cached_tracklist.artist,
                    event=cached_tracklist.event,
                    date=cached_tracklist.date.isoformat() if cached_tracklist.date else None,
                    source_url=cached_tracklist.source_url,
                )
            else:
                scraped = await scraper.scrape_tracklist(search_result.url)
                # phaze-x4ux: repair mojibake on the scraped side too -- 1001Tracklists page text is
                # an external source and can itself carry a mis-decode. Mutated in place (before both
                # the match-confidence scoring below and the eventual Tracklist row persistence in
                # _store_scraped_tracklist) so the repair happens ONCE at this ingest boundary rather
                # than being re-applied at every later read. `tracklists.search_vector` is a DB
                # GENERATED column over exactly `artist`/`event`, so this also keeps that index clean.
                if scraped.artist is not None:
                    scraped.artist = repair_mojibake(scraped.artist)
                if scraped.event is not None:
                    scraped.event = repair_mojibake(scraped.event)

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
            scraped_items.append((scraped, auto_link, confidence if auto_link else None, from_cache))
    finally:
        await scraper.close()

    # 3. Store all scraped results in ONE short transaction, sorted by external_id so overlapping
    #    concurrent searches acquire the shared per-external_id advisory locks in a CONSISTENT order
    #    (no ABBA deadlock) and no lock is ever held across a network scrape (phaze-1bcc).
    #
    # phaze-g2j3: EmptyScrapeError is a per-item protective refusal (phaze-gfyr) -- it fires when
    # ONE result's page soft-blocked/parsed to zero tracks over a tracklist that already has good
    # data. Letting it escape this loop aborts the `async with` without a commit, rolling back
    # every ALREADY-STORED sibling result in the same batch and forcing SAQ to retry the ENTIRE
    # search. The guard's purpose -- never overwrite good data with an empty version -- is fully
    # served by skipping just the offending item, so catch it per-item and keep storing the rest.
    file_uuid = uuid.UUID(file_id)
    stored_auto_linked = False
    async with ctx["async_session"]() as session:
        for scraped, auto_link, stored_confidence, from_cache in sorted(scraped_items, key=lambda item: item[0].external_id):
            if from_cache:
                # phaze-hu8v: already-scraped data, nothing new to persist except (maybe) the file
                # link -- see _link_cached_tracklist for why this skips creating a new version.
                if auto_link:
                    await _link_cached_tracklist(session, scraped.external_id, file_uuid, stored_confidence, auto_link)
                    stored_auto_linked = True
                continue
            try:
                await _store_scraped_tracklist(
                    session,
                    scraped,
                    file_id=file_uuid if auto_link else None,
                    confidence=stored_confidence,
                    auto_linked=auto_link,
                )
            except EmptyScrapeError:
                logger.warning(
                    "Skipping empty re-scrape within search batch store (sibling results preserved)",
                    file_id=file_id,
                    external_id=scraped.external_id,
                )
                continue
            if auto_link:
                stored_auto_linked = True
        await session.commit()

    logger.info(
        "tracklist search completed",
        file_id=file_id,
        results_found=len(results),
        auto_linked=stored_auto_linked,
    )
    return {"file_id": file_id, "results_found": len(results), "auto_linked": stored_auto_linked}


async def scrape_and_store_tracklist(ctx: dict[str, Any], *, tracklist_id: str) -> dict[str, Any]:
    """Re-scrape an existing tracklist and create a new version.

    Used for manual re-scrape action and refresh jobs.
    Retries with exponential backoff are handled by SAQ queue configuration.
    """
    logger.info("tracklist scrape started", tracklist_id=tracklist_id)
    tl_uuid = uuid.UUID(tracklist_id)

    # phaze-igwi: scrape_tracklist() sleeps 2-5s on the rate limiter then POSTs with a 30s timeout.
    # Holding the session's implicit transaction (a pinned PgBouncer SESSION-mode pooled connection)
    # idle-in-transaction across that ~2-35s of network I/O drains the capped pool -- refresh_tracklists
    # / rescrape fan these out across the corpus. So read the source_url in a short session, RELEASE
    # the connection before the scrape, then re-open a short session to store + commit.
    async with ctx["async_session"]() as session:
        result = await session.execute(select(Tracklist).where(Tracklist.id == tl_uuid))
        tracklist = result.scalar_one_or_none()
        if tracklist is None:
            logger.info("tracklist scrape completed", tracklist_id=tracklist_id, status="not_found", tracks_found=0)
            return {"tracklist_id": tracklist_id, "tracks_found": 0, "version": 0, "status": "not_found"}
        source_url = tracklist.source_url

    # Scrape with NO DB connection held (phaze-igwi).
    scraper = TracklistScraper()
    try:
        scraped = await scraper.scrape_tracklist(source_url)
    finally:
        await scraper.close()

    # Re-open a short session only for the write + the version read-back (phaze-igwi).
    async with ctx["async_session"]() as session:
        await _store_scraped_tracklist(session, scraped)
        await session.commit()

        # Get the version number we just created
        version_result = await session.execute(
            select(TracklistVersion).where(TracklistVersion.tracklist_id == tl_uuid).order_by(TracklistVersion.version_number.desc()).limit(1)
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


async def refresh_tracklists(ctx: dict[str, Any]) -> dict[str, Any]:
    """Refresh stale and unresolved tracklists.

    Per D-10: find tracklists where file_id IS NULL (unresolved) or updated_at < 90 days ago (stale).
    Per TL-04: add randomized jitter between scrapes (60-300 seconds).

    phaze-p1vy: restricted to ``source == "1001tracklists"``. HISTORICAL rows with
    ``source="fingerprint"`` and ``source_url=""`` may still exist: the retired audio-fingerprint
    scan path (phaze-0jpe removed it, and no writer produces such a row any more) stored them with
    no known source URL. They are structurally un-rescrapeable --
    ``TracklistScraper.scrape_tracklist("")`` always raises before storing anything, so
    ``updated_at`` never advances and, without this filter, every such surviving row re-enters the
    stale arm on every monthly run forever, each futile attempt still paying the scraper's
    rate-limit delay plus this loop's 60-300s jitter sleep. The filter is a positive allowlist on
    ``"1001tracklists"``, so it stays correct whether or not those rows are ever purged.

    phaze-fq9h.7: PROPAGATED rows are excluded too. They are projections of a canonical row onto a
    unique set's duplicate files and share its ``source_url``, so refreshing them would re-scrape
    the SAME page once per duplicate -- N requests against a whole-host budget of ~1 per 8s to
    fetch bytes the canonical row's own refresh already fetched. The canonical row is still swept;
    re-propagating its new version is the drain's job, and costs zero requests.
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
            result = await session.execute(
                select(Tracklist).where(
                    Tracklist.source == "1001tracklists",
                    Tracklist.propagated_from_set_key.is_(None),
                    (Tracklist.file_id.is_(None)) | (Tracklist.updated_at < stale_threshold),
                )
            )
            tracklists = list(result.scalars().all())
    except Exception:
        logger.exception("Error querying stale/unresolved tracklists")
        return {"refreshed": 0, "errors": 1}

    for tl in tracklists:
        # Defensive secondary guard (belt-and-suspenders with the source filter above): skip any
        # row that slipped through without a scrapeable source_url instead of burning a guaranteed-
        # failing scrape attempt plus the jitter sleep below.
        if not tl.source_url:
            logger.warning("Skipping tracklist with no source_url", tracklist_id=str(tl.id), source=tl.source)
            continue

        try:
            await scrape_and_store_tracklist(ctx, tracklist_id=str(tl.id))
            refreshed += 1
        except Exception:
            logger.warning("Failed to refresh tracklist %s", tl.id, exc_info=True)
            errors += 1

        # Randomized jitter between scrapes (per D-10, TL-04)
        await asyncio.sleep(random.uniform(60, 300))  # noqa: S311  # nosec B311

    return {"refreshed": refreshed, "errors": errors}
