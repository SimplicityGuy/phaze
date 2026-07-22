"""Tests for 1001Tracklists SAQ task functions."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from phaze.tasks.tracklist import EmptyScrapeError, _store_scraped_tracklist, refresh_tracklists, scrape_and_store_tracklist, search_tracklist


def _make_ctx() -> dict[str, Any]:
    """Create a minimal SAQ context dict with async_session factory."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()  # AsyncSession.add is sync; keep it non-async so no un-awaited-coroutine warning
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return {"async_session": mock_session_factory, "_mock_session": mock_session}


def _make_file_record(
    file_id: uuid.UUID | None = None,
    original_filename: str = "Artist - Live @ Coachella 2024.04.14.mp3",
) -> MagicMock:
    """Create a mock FileRecord."""
    record = MagicMock()
    record.id = file_id or uuid.uuid4()
    record.original_filename = original_filename
    record.file_metadata = None
    return record


def _make_search_result(external_id: str = "abc123") -> MagicMock:
    """Create a mock TracklistSearchResult."""
    result = MagicMock()
    result.external_id = external_id
    result.title = "Test Tracklist"
    result.url = f"https://www.1001tracklists.com/tracklist/{external_id}/test.html"
    result.artist = "Test Artist"
    result.date = "2024-04-14"
    return result


def _make_scraped_tracklist(external_id: str = "abc123") -> MagicMock:
    """Create a mock ScrapedTracklist."""
    scraped = MagicMock()
    scraped.external_id = external_id
    scraped.title = "Artist @ Coachella 2024"
    scraped.artist = "Artist"
    scraped.event = "Coachella"
    scraped.date = "2024-04-14"
    scraped.source_url = f"https://www.1001tracklists.com/tracklist/{external_id}/test.html"
    scraped.tracks = [
        MagicMock(position=1, artist="Track Artist", title="Track Title", label="Label", timestamp="00:00", is_mashup=False, remix_info=None),
        MagicMock(position=2, artist="Artist 2", title="Title 2", label=None, timestamp="05:30", is_mashup=False, remix_info=None),
    ]
    return scraped


@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
@patch("phaze.tasks.tracklist.compute_match_confidence", return_value=50)
@patch("phaze.tasks.tracklist.should_auto_link", return_value=False)
async def test_search_tracklist_processes_results(
    mock_auto_link: MagicMock,
    mock_confidence: MagicMock,
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
) -> None:
    """search_tracklist calls scraper.search and processes results."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    mock_parse.return_value = ("Artist", "Coachella", date(2024, 4, 14))

    # Session execute returns: file record, then tracklist lookup (None = new)
    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record

    mock_tl_result = MagicMock()
    mock_tl_result.scalar_one_or_none.return_value = None

    session.execute.return_value = mock_file_result

    search_result = _make_search_result()
    scraped = _make_scraped_tracklist()

    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = [search_result]
    mock_scraper.scrape_tracklist.return_value = scraped
    mock_scraper_cls.return_value = mock_scraper

    result = await search_tracklist(ctx, file_id=str(file_record.id))

    assert result["results_found"] == 1
    assert result["auto_linked"] is False
    mock_scraper.search.assert_awaited_once_with("Artist Coachella")
    mock_scraper.scrape_tracklist.assert_awaited_once()
    mock_scraper.close.assert_awaited_once()


@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
@patch("phaze.tasks.tracklist.compute_match_confidence", return_value=95)
@patch("phaze.tasks.tracklist.should_auto_link", return_value=True)
async def test_search_tracklist_auto_links(
    mock_auto_link: MagicMock,
    mock_confidence: MagicMock,
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
) -> None:
    """search_tracklist auto-links when confidence >= 90."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    mock_parse.return_value = ("Artist", "Coachella", date(2024, 4, 14))

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record
    mock_tl_result = MagicMock()
    mock_tl_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_file_result

    search_result = _make_search_result()
    scraped = _make_scraped_tracklist()

    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = [search_result]
    mock_scraper.scrape_tracklist.return_value = scraped
    mock_scraper_cls.return_value = mock_scraper

    result = await search_tracklist(ctx, file_id=str(file_record.id))

    assert result["auto_linked"] is True
    mock_scraper.close.assert_awaited_once()


@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
@patch("phaze.tasks.tracklist.should_auto_link", return_value=False)
async def test_search_tracklist_passes_parsed_scraped_date_to_scorer(
    mock_auto_link: MagicMock,
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
) -> None:
    """phaze-rkxy: the scraped date is parsed and passed to compute_match_confidence.

    Hardcoding tracklist_date=None made the Pitfall-3 date-mismatch cap dead in the ONLY
    auto-link path, so a wrong-date tracklist could auto-link on artist+event alone. The scorer
    must now receive the real scraped date so the cap can fire.
    """
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()
    mock_parse.return_value = ("Artist", "Coachella", date(2024, 4, 14))

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record
    session.execute.return_value = mock_file_result

    search_result = _make_search_result()
    scraped = _make_scraped_tracklist()
    scraped.date = "2019-04-13"  # a DIFFERENT year than the file -- the mismatch the cap guards

    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = [search_result]
    mock_scraper.scrape_tracklist.return_value = scraped
    mock_scraper_cls.return_value = mock_scraper

    with patch("phaze.tasks.tracklist.compute_match_confidence", return_value=42) as mock_conf:
        await search_tracklist(ctx, file_id=str(file_record.id))

    mock_conf.assert_called_once()
    assert mock_conf.call_args.kwargs["tracklist_date"] == date(2019, 4, 13)


@patch("phaze.tasks.tracklist._store_scraped_tracklist", new_callable=AsyncMock)
@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
@patch("phaze.tasks.tracklist.compute_match_confidence", return_value=100)
@patch("phaze.tasks.tracklist.should_auto_link", return_value=True)
async def test_search_tracklist_skips_auto_link_when_date_not_confirmed(
    mock_auto_link: MagicMock,
    mock_conf: MagicMock,
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
    mock_store: AsyncMock,
) -> None:
    """phaze-rkxy: even at confidence 100, a wrong-date tracklist must NOT auto-link.

    The file is from 2024; the scraped tracklist is from 2019. should_auto_link(100) is True, but
    the same-window date is not confirmed, so the store is called WITHOUT a file_id -- the tracklist
    is saved for manual review rather than silently auto-linked to the wrong-date file.
    """
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()
    mock_parse.return_value = ("Artist", "Coachella", date(2024, 4, 14))

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record
    session.execute.return_value = mock_file_result

    search_result = _make_search_result()
    scraped = _make_scraped_tracklist()
    scraped.date = "2019-04-13"  # different year -> outside the 3-day same-window
    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = [search_result]
    mock_scraper.scrape_tracklist.return_value = scraped
    mock_scraper_cls.return_value = mock_scraper

    result = await search_tracklist(ctx, file_id=str(file_record.id))

    assert result["auto_linked"] is False
    assert mock_store.await_count == 1
    assert mock_store.await_args.kwargs["file_id"] is None
    assert mock_store.await_args.kwargs["auto_linked"] is False


@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename", return_value=None)
async def test_search_tracklist_no_query(
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
) -> None:
    """search_tracklist returns early when no query can be built."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record(original_filename="random.mp3")
    file_record.file_metadata = None

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record
    session.execute.return_value = mock_file_result

    result = await search_tracklist(ctx, file_id=str(file_record.id))

    assert result["results_found"] == 0
    assert result["status"] == "no_query"


@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
async def test_search_tracklist_no_results(
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
) -> None:
    """search_tracklist handles zero results from scraper."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    mock_parse.return_value = ("Artist", "Coachella", date(2024, 4, 14))

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record
    session.execute.return_value = mock_file_result

    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = []
    mock_scraper_cls.return_value = mock_scraper

    result = await search_tracklist(ctx, file_id=str(file_record.id))

    assert result["results_found"] == 0
    assert result["auto_linked"] is False


@patch("phaze.tasks.tracklist.TracklistScraper")
async def test_scrape_and_store_tracklist(mock_scraper_cls: MagicMock) -> None:
    """scrape_and_store_tracklist creates new version with tracks."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    tracklist_id = uuid.uuid4()

    mock_tracklist = MagicMock()
    mock_tracklist.id = tracklist_id
    mock_tracklist.source_url = "https://www.1001tracklists.com/tracklist/abc/test.html"

    mock_tl_result = MagicMock()
    mock_tl_result.scalar_one_or_none.return_value = mock_tracklist

    # For the existing tracklist lookup in _store_scraped_tracklist
    mock_existing_result = MagicMock()
    mock_existing_result.scalar_one_or_none.return_value = mock_tracklist

    # For version lookup
    mock_version_result = MagicMock()
    mock_version = MagicMock()
    mock_version.version_number = 2
    mock_version_result.scalar_one_or_none.return_value = mock_version

    # Execute order: task's tracklist-by-id lookup, then inside _store_scraped_tracklist the
    # per-external_id advisory lock (phaze-5vmt), the existing-tracklist lookup, the version
    # lookup, and finally the task's own version lookup.
    session.execute.side_effect = [mock_tl_result, MagicMock(), mock_existing_result, mock_version_result, mock_version_result]

    scraped = _make_scraped_tracklist()
    mock_scraper = AsyncMock()
    mock_scraper.scrape_tracklist.return_value = scraped
    mock_scraper_cls.return_value = mock_scraper

    result = await scrape_and_store_tracklist(ctx, tracklist_id=str(tracklist_id))

    assert result["tracklist_id"] == str(tracklist_id)
    mock_scraper.scrape_tracklist.assert_awaited_once()
    mock_scraper.close.assert_awaited_once()


@patch("phaze.tasks.tracklist.TracklistScraper")
async def test_scrape_and_store_tracklist_parses_non_first_date_format(mock_scraper_cls: MagicMock) -> None:
    """A scraped date not in the first ("%Y-%m-%d") format falls through the ValueError branch to a later format."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    tracklist_id = uuid.uuid4()

    mock_tracklist = MagicMock()
    mock_tracklist.id = tracklist_id
    mock_tracklist.source_url = "https://www.1001tracklists.com/tracklist/abc/test.html"
    mock_tl_result = MagicMock()
    mock_tl_result.scalar_one_or_none.return_value = mock_tracklist
    mock_existing_result = MagicMock()
    mock_existing_result.scalar_one_or_none.return_value = mock_tracklist
    mock_version_result = MagicMock()
    mock_version = MagicMock()
    mock_version.version_number = 2
    mock_version_result.scalar_one_or_none.return_value = mock_version
    # +1 execute for the per-external_id advisory lock inside _store_scraped_tracklist (phaze-5vmt).
    session.execute.side_effect = [mock_tl_result, MagicMock(), mock_existing_result, mock_version_result, mock_version_result]

    scraped = _make_scraped_tracklist()
    scraped.date = "14 Apr 2024"  # "%d %b %Y" -- the FIRST format "%Y-%m-%d" raises ValueError -> continue -> this matches
    mock_scraper = AsyncMock()
    mock_scraper.scrape_tracklist.return_value = scraped
    mock_scraper_cls.return_value = mock_scraper

    result = await scrape_and_store_tracklist(ctx, tracklist_id=str(tracklist_id))
    assert result["tracklist_id"] == str(tracklist_id)


@patch("phaze.tasks.tracklist.scrape_and_store_tracklist")
@patch("phaze.tasks.tracklist.asyncio.sleep", new_callable=AsyncMock)
async def test_refresh_tracklists(mock_sleep: AsyncMock, mock_scrape: AsyncMock) -> None:
    """refresh_tracklists processes stale and unresolved tracklists."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]

    mock_tl_1 = MagicMock()
    mock_tl_1.id = uuid.uuid4()
    mock_tl_2 = MagicMock()
    mock_tl_2.id = uuid.uuid4()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_tl_1, mock_tl_2]
    session.execute.return_value = mock_result

    mock_scrape.return_value = {"tracklist_id": "x", "tracks_found": 5, "version": 1}

    result = await refresh_tracklists(ctx)

    assert result["refreshed"] == 2
    assert result["errors"] == 0
    assert mock_scrape.await_count == 2
    assert mock_sleep.await_count == 2


async def test_store_scraped_tracklist_creates_new_when_absent() -> None:
    """With no existing external_id match, a fresh Tracklist is created at version 1."""
    session = AsyncMock()
    session.add = MagicMock()  # AsyncSession.add is sync; keep it non-async so no un-awaited-coroutine warning
    no_match = MagicMock()
    no_match.scalar_one_or_none.return_value = None
    session.execute.return_value = no_match

    scraped = _make_scraped_tracklist(external_id="brand-new")
    result = await _store_scraped_tracklist(session, scraped)

    # The create branch was taken: the new Tracklist carries the scraped metadata ...
    assert result.external_id == "brand-new"
    assert result.artist == "Artist"
    assert result.event == "Coachella"
    assert result.date == date(2024, 4, 14)
    # ... and both the tracklist and its first version were flushed (add called for tl + version + 2 tracks).
    assert session.add.call_count == 4
    assert session.flush.await_count == 2


async def test_store_scraped_tracklist_swallows_non_valueerror_date() -> None:
    """A date value that makes strptime raise a non-ValueError (e.g. a non-str) is caught, leaving date None."""
    session = AsyncMock()
    session.add = MagicMock()  # AsyncSession.add is sync; keep it non-async so no un-awaited-coroutine warning
    no_match = MagicMock()
    no_match.scalar_one_or_none.return_value = None
    session.execute.return_value = no_match

    scraped = _make_scraped_tracklist(external_id="bad-date")
    scraped.date = 20240414  # int -> strptime raises TypeError -> outer except -> date stays None

    result = await _store_scraped_tracklist(session, scraped)

    assert result.date is None


async def test_store_scraped_tracklist_takes_advisory_lock() -> None:
    """The per-external_id advisory lock is acquired first, before the upsert read (phaze-5vmt)."""
    session = AsyncMock()
    session.add = MagicMock()
    no_match = MagicMock()
    no_match.scalar_one_or_none.return_value = None
    session.execute.return_value = no_match

    scraped = _make_scraped_tracklist(external_id="lock-me")
    await _store_scraped_tracklist(session, scraped)

    first_stmt = session.execute.call_args_list[0].args[0]
    assert "pg_advisory_xact_lock" in str(first_stmt)


async def test_store_scraped_tracklist_refuses_empty_rescrape_over_existing_tracks() -> None:
    """An empty (blocked) re-scrape of a tracklist that already has tracks raises, never clobbers (phaze-gfyr)."""
    session = AsyncMock()
    session.add = MagicMock()

    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.latest_version_id = uuid.uuid4()
    existing.artist = "Good Artist"
    existing.event = "Good Event"

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing
    count_result = MagicMock()
    count_result.scalar.return_value = 5  # existing latest version has tracks
    # Order: advisory lock, external_id lookup, latest-version track count.
    session.execute.side_effect = [MagicMock(), existing_result, count_result]

    scraped = _make_scraped_tracklist(external_id="blocked")
    scraped.tracks = []
    scraped.artist = None
    scraped.event = None
    scraped.date = None

    with pytest.raises(EmptyScrapeError):
        await _store_scraped_tracklist(session, scraped)

    # Metadata preserved, no new (empty) version appended.
    assert existing.artist == "Good Artist"
    assert existing.event == "Good Event"
    session.add.assert_not_called()


async def test_store_scraped_tracklist_empty_rescrape_allowed_when_no_prior_tracks() -> None:
    """An empty re-scrape is allowed when the existing tracklist has no prior version to protect (phaze-gfyr)."""
    session = AsyncMock()
    session.add = MagicMock()

    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.latest_version_id = None  # nothing to protect -> no track-count query needed

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing
    version_result = MagicMock()
    version_result.scalar_one_or_none.return_value = None  # next_version = 1
    # Order: advisory lock, external_id lookup, max-version lookup (no track-count query fires).
    session.execute.side_effect = [MagicMock(), existing_result, version_result]

    scraped = _make_scraped_tracklist(external_id="empty-ok")
    scraped.tracks = []

    result = await _store_scraped_tracklist(session, scraped)
    assert result is existing


async def test_store_scraped_tracklist_does_not_null_metadata_on_partial_scrape() -> None:
    """A scrape that resolves tracks but no artist must not null the existing artist (phaze-gfyr)."""
    session = AsyncMock()
    session.add = MagicMock()

    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.latest_version_id = uuid.uuid4()
    existing.artist = "Keep Me"
    existing.event = "Keep Event"

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing
    version_result = MagicMock()
    version_result.scalar_one_or_none.return_value = None
    # scraped has tracks -> the empty-guard short-circuits before any track-count query.
    session.execute.side_effect = [MagicMock(), existing_result, version_result]

    scraped = _make_scraped_tracklist(external_id="partial")
    scraped.artist = None  # scrape produced no artist
    scraped.event = None

    await _store_scraped_tracklist(session, scraped)

    assert existing.artist == "Keep Me"
    assert existing.event == "Keep Event"


async def test_store_scraped_tracklist_does_not_steal_link_from_another_file() -> None:
    """phaze-4a5w: an auto-link must NOT overwrite a tracklist already owned by a DIFFERENT file.

    Duplicate copies of the same set resolve to the same external_id. A later file's search that
    scores >= 90 previously flipped the existing tracklist's file_id, clobbering a manual link and
    stamping auto_linked=True over it. The existing linkage (and its provenance) must survive.
    """
    session = AsyncMock()
    session.add = MagicMock()

    owner_file_id = uuid.uuid4()
    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.latest_version_id = None
    existing.file_id = owner_file_id  # already MANUALLY linked to file A
    existing.match_confidence = 77
    existing.auto_linked = False

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing
    version_result = MagicMock()
    version_result.scalar_one_or_none.return_value = None
    # Order: advisory lock, external_id lookup, max-version lookup.
    session.execute.side_effect = [MagicMock(), existing_result, version_result]

    scraped = _make_scraped_tracklist(external_id="shared-set")

    other_file_id = uuid.uuid4()  # file B, a duplicate copy of the same set
    await _store_scraped_tracklist(session, scraped, file_id=other_file_id, confidence=99, auto_linked=True)

    # The existing link is untouched: still file A, still the manual confidence, still auto_linked=False.
    assert existing.file_id == owner_file_id
    assert existing.match_confidence == 77
    assert existing.auto_linked is False


async def test_store_scraped_tracklist_links_when_unowned() -> None:
    """phaze-4a5w: an auto-link still applies when the tracklist is unowned (file_id None)."""
    session = AsyncMock()
    session.add = MagicMock()

    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.latest_version_id = None
    existing.file_id = None  # unowned -- fair game
    existing.match_confidence = None
    existing.auto_linked = False

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing
    version_result = MagicMock()
    version_result.scalar_one_or_none.return_value = None
    session.execute.side_effect = [MagicMock(), existing_result, version_result]

    scraped = _make_scraped_tracklist(external_id="unowned-set")
    file_id = uuid.uuid4()
    await _store_scraped_tracklist(session, scraped, file_id=file_id, confidence=95, auto_linked=True)

    assert existing.file_id == file_id
    assert existing.match_confidence == 95
    assert existing.auto_linked is True


async def test_store_scraped_tracklist_relinks_same_file() -> None:
    """phaze-4a5w: re-linking the SAME file (file_id equal) refreshes confidence, not blocked."""
    session = AsyncMock()
    session.add = MagicMock()

    file_id = uuid.uuid4()
    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.latest_version_id = None
    existing.file_id = file_id
    existing.match_confidence = 90
    existing.auto_linked = True

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing
    version_result = MagicMock()
    version_result.scalar_one_or_none.return_value = None
    session.execute.side_effect = [MagicMock(), existing_result, version_result]

    scraped = _make_scraped_tracklist(external_id="same-file-set")
    await _store_scraped_tracklist(session, scraped, file_id=file_id, confidence=98, auto_linked=True)

    assert existing.file_id == file_id
    assert existing.match_confidence == 98


@patch("phaze.tasks.tracklist.scrape_and_store_tracklist")
@patch("phaze.tasks.tracklist.asyncio.sleep", new_callable=AsyncMock)
async def test_refresh_tracklists_filters_query_to_scrapeable_source(mock_sleep: AsyncMock, mock_scrape: AsyncMock) -> None:
    """The stale/unresolved SELECT restricts to source == '1001tracklists' (phaze-p1vy).

    Fingerprint-sourced tracklists (source='fingerprint', source_url='') are structurally
    un-rescrapeable; without this filter they re-enter the stale arm forever once aged past 90
    days, each attempt burning a guaranteed-failing scrape plus the 60-300s jitter sleep.
    """
    ctx = _make_ctx()
    session = ctx["_mock_session"]

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    session.execute.return_value = mock_result

    await refresh_tracklists(ctx)

    stmt = session.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "tracklists.source" in compiled


@patch("phaze.tasks.tracklist.scrape_and_store_tracklist")
@patch("phaze.tasks.tracklist.asyncio.sleep", new_callable=AsyncMock)
async def test_refresh_tracklists_skips_rows_with_no_source_url(mock_sleep: AsyncMock, mock_scrape: AsyncMock) -> None:
    """A selected row with a falsy source_url is skipped (defense-in-depth for phaze-p1vy).

    This exercises the in-loop guard directly; the query-level filter above is the primary fix.
    """
    ctx = _make_ctx()
    session = ctx["_mock_session"]

    mock_tl_no_url = MagicMock(id=uuid.uuid4(), source_url="", source="fingerprint")
    mock_tl_ok = MagicMock(id=uuid.uuid4(), source_url="https://example.com/tl", source="1001tracklists")
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_tl_no_url, mock_tl_ok]
    session.execute.return_value = mock_result

    mock_scrape.return_value = {"tracklist_id": "x", "tracks_found": 5, "version": 1}

    result = await refresh_tracklists(ctx)

    assert result == {"refreshed": 1, "errors": 0}
    mock_scrape.assert_awaited_once_with(ctx, tracklist_id=str(mock_tl_ok.id))
    assert mock_sleep.await_count == 1


@patch("phaze.tasks.tracklist.scrape_and_store_tracklist")
@patch("phaze.tasks.tracklist.asyncio.sleep", new_callable=AsyncMock)
async def test_refresh_tracklists_counts_per_item_failures(mock_sleep: AsyncMock, mock_scrape: AsyncMock) -> None:
    """A scrape failure on one tracklist increments errors but does not abort the sweep."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]

    mock_tl_ok = MagicMock(id=uuid.uuid4())
    mock_tl_bad = MagicMock(id=uuid.uuid4())
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_tl_ok, mock_tl_bad]
    session.execute.return_value = mock_result

    mock_scrape.side_effect = [{"tracklist_id": "ok"}, RuntimeError("scrape blew up")]

    result = await refresh_tracklists(ctx)

    assert result == {"refreshed": 1, "errors": 1}
    assert mock_scrape.await_count == 2
    # jitter sleep still runs after each attempt, success or failure
    assert mock_sleep.await_count == 2


@patch("phaze.tasks.tracklist.asyncio.sleep", new_callable=AsyncMock)
async def test_refresh_tracklists_reports_outer_failure_as_error(mock_sleep: AsyncMock) -> None:
    """phaze-xpzp: a failure loading the stale/unresolved set is logged AND counted as an error, not a raise.

    Previously this asserted the OLD (buggy) behavior -- a query failure (e.g. the aware-vs-naive
    ``DataError``) was swallowed by a broad ``except Exception`` into the untouched
    ``{"refreshed": 0, "errors": 0}`` initial counters, a return value indistinguishable from "there
    was simply nothing to refresh". SAQ then marked the job successful and the monthly cron silently
    never ran. The query failure must now surface in ``errors``.
    """
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    session.execute.side_effect = RuntimeError("db unreachable")

    result = await refresh_tracklists(ctx)

    assert result == {"refreshed": 0, "errors": 1}
    mock_sleep.assert_not_awaited()


async def test_search_tracklist_file_not_found() -> None:
    """search_tracklist returns not_found for non-existent file."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    result = await search_tracklist(ctx, file_id=str(uuid.uuid4()))

    assert result["status"] == "not_found"
    assert result["results_found"] == 0


@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename", return_value=None)
async def test_search_tracklist_metadata_fallback(
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
) -> None:
    """search_tracklist falls back to file_metadata artist when filename parse fails."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record(original_filename="unknown.mp3")
    # Set up metadata fallback
    file_record.file_metadata = MagicMock()
    file_record.file_metadata.artist = "Metadata Artist"

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record
    session.execute.return_value = mock_file_result

    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = []
    mock_scraper_cls.return_value = mock_scraper

    result = await search_tracklist(ctx, file_id=str(file_record.id))

    assert result["results_found"] == 0
    # Verify search was called with the metadata artist
    mock_scraper.search.assert_awaited_once_with("Metadata Artist")


async def test_scrape_and_store_tracklist_not_found() -> None:
    """scrape_and_store_tracklist returns not_found for non-existent tracklist."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    result = await scrape_and_store_tracklist(ctx, tracklist_id=str(uuid.uuid4()))

    assert result["status"] == "not_found"
    assert result["tracks_found"] == 0


@patch("phaze.tasks.tracklist.TracklistScraper")
async def test_scrape_and_store_tracklist_releases_connection_before_scrape(mock_scraper_cls: MagicMock) -> None:
    """phaze-igwi: no DB session is held across scrape_tracklist()'s rate-limit sleep + HTTP.

    Pre-igwi the session opened for the source_url read stayed open through scrape_tracklist()
    (~2-35s of network I/O), pinning a PgBouncer SESSION-mode connection idle-in-transaction; the
    refresh/rescrape fan-out drains the capped pool. The read session must CLOSE before the scrape and
    a FRESH session open only for the store. We record the session lifecycle interleaved with the
    scrape and assert the read session closes before the scrape and the write session opens after it.
    """
    tracklist_id = uuid.uuid4()

    mock_tracklist = MagicMock()
    mock_tracklist.id = tracklist_id
    mock_tracklist.source_url = "https://www.1001tracklists.com/tracklist/abc/test.html"

    mock_existing_result = MagicMock()
    mock_existing_result.scalar_one_or_none.return_value = mock_tracklist
    mock_version = MagicMock()
    mock_version.version_number = 2
    mock_version_result = MagicMock()
    mock_version_result.scalar_one_or_none.return_value = mock_version
    mock_tl_result = MagicMock()
    mock_tl_result.scalar_one_or_none.return_value = mock_tracklist

    events: list[str] = []
    session_count = 0

    def _factory() -> MagicMock:
        nonlocal session_count
        session_count += 1
        idx = session_count
        session = AsyncMock()
        session.add = MagicMock()
        if idx == 1:
            session.execute.side_effect = [mock_tl_result]
        else:
            # store: advisory lock, existing lookup, version lookup; then the task's version read-back.
            session.execute.side_effect = [MagicMock(), mock_existing_result, mock_version_result, mock_version_result]

        cm = MagicMock()

        async def _aenter(*_a: Any) -> AsyncMock:
            events.append(f"open{idx}")
            return session

        async def _aexit(*_a: Any) -> bool:
            events.append(f"close{idx}")
            return False

        cm.__aenter__ = _aenter
        cm.__aexit__ = _aexit
        return cm

    scraped = _make_scraped_tracklist()

    async def _scrape_recording(_url: str) -> Any:
        events.append("scrape")
        return scraped

    mock_scraper = AsyncMock()
    mock_scraper.scrape_tracklist.side_effect = _scrape_recording
    mock_scraper_cls.return_value = mock_scraper

    ctx = {"async_session": _factory}
    result = await scrape_and_store_tracklist(ctx, tracklist_id=str(tracklist_id))

    assert result["tracklist_id"] == str(tracklist_id)
    # Two distinct sessions (read, then write) -- not one held across the scrape.
    assert session_count == 2
    # The read session closes BEFORE the scrape; the write session opens only AFTER it.
    assert events.index("close1") < events.index("scrape")
    assert events.index("open2") > events.index("scrape")


def test_controller_settings_contains_tracklist_functions() -> None:
    """SAQ controller settings functions includes search_tracklist + scrape_and_store_tracklist (Phase 26 D-03)."""
    from phaze.tasks.controller import settings as controller_settings

    func_names = [f.__name__ if hasattr(f, "__name__") else str(f) for f in controller_settings["functions"]]
    assert "search_tracklist" in func_names
    assert "scrape_and_store_tracklist" in func_names


def test_controller_settings_has_cron_jobs() -> None:
    """SAQ controller settings cron_jobs includes refresh_tracklists cron (Phase 26 D-03)."""
    from phaze.tasks.controller import settings as controller_settings

    assert "cron_jobs" in controller_settings
    assert len(controller_settings["cron_jobs"]) >= 1
    # Check the cron job has the right function
    cron_job = controller_settings["cron_jobs"][0]
    assert cron_job.function.__name__ == "refresh_tracklists"
