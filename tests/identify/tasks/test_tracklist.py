"""Tests for 1001Tracklists SAQ task functions."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from phaze.tasks.tracklist import (
    EmptyScrapeError,
    _apply_file_link,
    _find_cached_tracklists,
    _link_cached_tracklist,
    _store_scraped_tracklist,
    refresh_tracklists,
    scrape_and_store_tracklist,
    search_tracklist,
)


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


def _make_cached_tracklist(
    external_id: str = "abc123",
    artist: str = "Artist",
    event: str = "Coachella",
    tracklist_date: date | None = date(2024, 4, 14),
    file_id: uuid.UUID | None = None,
    latest_version_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock already-scraped Tracklist row (phaze-hu8v cache-hit fixtures)."""
    tracklist = MagicMock()
    tracklist.id = uuid.uuid4()
    tracklist.external_id = external_id
    tracklist.artist = artist
    tracklist.event = event
    tracklist.date = tracklist_date
    tracklist.source_url = f"https://www.1001tracklists.com/tracklist/{external_id}/test.html"
    tracklist.file_id = file_id
    tracklist.match_confidence = None
    tracklist.auto_linked = False
    tracklist.latest_version_id = latest_version_id or uuid.uuid4()
    return tracklist


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

    HISTORICAL rows from the retired fingerprint-scan path (source='fingerprint', source_url='')
    are structurally
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


@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.compute_match_confidence", return_value=50)
@patch("phaze.tasks.tracklist.should_auto_link", return_value=False)
async def test_search_tracklist_repairs_mojibake_filename_before_parsing(
    mock_auto_link: MagicMock,
    mock_confidence: MagicMock,
    mock_scraper_cls: MagicMock,
) -> None:
    """phaze-x4ux: the filename signal fed to parse_live_set_filename/the search query is repaired.

    `FileRecord.original_filename` stays byte-faithful (never rewritten); only the local
    matching/query-building signal is repaired.
    """
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record(
        original_filename="Carl Cox, Umek, Dj Rush, Chris Liebing, Sven VÃƒÂ¤th - Live @ Timewarp 2003.04.14.mp3",
    )
    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record
    session.execute.return_value = mock_file_result

    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = []
    mock_scraper_cls.return_value = mock_scraper

    await search_tracklist(ctx, file_id=str(file_record.id))

    # parse_live_set_filename is real here (not patched): it only matches the v1.0 pattern
    # "{Artist} - Live @ {Event} {YYYY.MM.DD}.{ext}", which requires the correctly-decoded
    # "Väth", not the mojibake -- so a successful parse proves the repair happened upstream.
    query = mock_scraper.search.await_args.args[0]
    assert "Väth" in query
    assert "Ã" not in query
    assert file_record.original_filename == "Carl Cox, Umek, Dj Rush, Chris Liebing, Sven VÃƒÂ¤th - Live @ Timewarp 2003.04.14.mp3"


@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
@patch("phaze.tasks.tracklist.compute_match_confidence", return_value=50)
@patch("phaze.tasks.tracklist.should_auto_link", return_value=False)
async def test_search_tracklist_repairs_mojibake_on_scraped_side(
    mock_auto_link: MagicMock,
    mock_confidence: MagicMock,
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
) -> None:
    """phaze-x4ux: scraped.artist/event (1001Tracklists, an external source) get repaired too.

    Repairing happens BEFORE compute_match_confidence is scored AND before the tracklist row
    would be persisted (_store_scraped_tracklist reads the same, now-repaired, attributes).
    """
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()
    mock_parse.return_value = ("Sven Väth", "Timewarp", date(2003, 4, 14))

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record
    mock_cache_check_result = MagicMock()  # unused return value; .scalars().all() iterates empty -> no cache hit
    mock_advisory_lock_result = MagicMock()  # unused return value (session.execute(select(pg_advisory_xact_lock(...))))
    mock_tl_result = MagicMock()
    mock_tl_result.scalar_one_or_none.return_value = None
    session.execute.side_effect = [mock_file_result, mock_cache_check_result, mock_advisory_lock_result, mock_tl_result]

    search_result = _make_search_result()
    scraped = _make_scraped_tracklist()
    scraped.artist = "Sven VÃƒÂ¤th"
    scraped.event = "TimewarpÃƒÂ©dition"

    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = [search_result]
    mock_scraper.scrape_tracklist.return_value = scraped
    mock_scraper_cls.return_value = mock_scraper

    await search_tracklist(ctx, file_id=str(file_record.id))

    mock_confidence.assert_called_once()
    assert mock_confidence.call_args.kwargs["tracklist_artist"] == "Sven Väth"
    assert mock_confidence.call_args.kwargs["tracklist_event"] == "Timewarpédition"
    # The scraped object itself was mutated in place, so the eventual
    # _store_scraped_tracklist persistence sees the repaired attributes too.
    assert scraped.artist == "Sven Väth"
    assert scraped.event == "Timewarpédition"


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


@patch("phaze.tasks.tracklist._store_scraped_tracklist", new_callable=AsyncMock)
@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
@patch("phaze.tasks.tracklist.compute_match_confidence", return_value=10)
@patch("phaze.tasks.tracklist.should_auto_link", return_value=False)
async def test_search_tracklist_scrapes_all_then_stores_sorted_without_holding_locks(
    _mock_auto_link: MagicMock,
    _mock_conf: MagicMock,
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
    mock_store: AsyncMock,
) -> None:
    """phaze-1bcc: every result is scraped BEFORE the store transaction opens, and stores run in
    external_id-sorted order.

    Pre-1bcc the task stored each result inside one long transaction, holding _store_scraped_tracklist's
    per-external_id advisory xact-lock across the scrape of every LATER result -- cross-job blocking plus
    an ABBA deadlock between two overlapping searches that locked shared ids in opposite order. The fix
    scrapes all results with NO connection held, then stores them in ONE short transaction sorted by
    external_id so overlapping jobs acquire shared locks in a consistent order.
    """
    file_record = _make_file_record()
    mock_parse.return_value = ("Artist", "Coachella", date(2024, 4, 14))

    events: list[str] = []

    # Results arrive in a deliberately UNSORTED order; the store phase must sort them.
    unsorted_ids = ["ccc", "aaa", "bbb"]
    search_results = [_make_search_result(external_id=eid) for eid in unsorted_ids]
    scraped_by_url = {r.url: _make_scraped_tracklist(external_id=eid) for eid, r in zip(unsorted_ids, search_results, strict=True)}

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record

    session_count = 0

    def _factory() -> MagicMock:
        nonlocal session_count
        session_count += 1
        idx = session_count
        session = AsyncMock()
        session.add = MagicMock()
        session.execute.return_value = mock_file_result
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

    async def _scrape_recording(url: str) -> Any:
        events.append(f"scrape:{scraped_by_url[url].external_id}")
        return scraped_by_url[url]

    async def _store_recording(_session: Any, scraped: Any, **_kwargs: Any) -> Any:
        events.append(f"store:{scraped.external_id}")
        return scraped

    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = search_results
    mock_scraper.scrape_tracklist.side_effect = _scrape_recording
    mock_scraper_cls.return_value = mock_scraper
    mock_store.side_effect = _store_recording

    ctx = {"async_session": _factory}
    result = await search_tracklist(ctx, file_id=str(file_record.id))

    assert result["results_found"] == 3
    # Three sessions: one short read session (file+query), one short cache-check session
    # (phaze-hu8v -- session2 here has no cache hits since mock_file_result.scalars().all()
    # iterates empty), one short store session -- never one held across the scrape loop.
    assert session_count == 3
    stores = [e for e in events if e.startswith("store:")]
    scrapes = [e for e in events if e.startswith("scrape:")]
    # The cache-check session opens and closes before any scrape, and the store session opens
    # only after every scrape (no lock/txn/connection held across a scrape).
    assert events.index("open2") > events.index("close1")
    assert events.index("close2") < events.index(scrapes[0])
    assert events.index("open3") > events.index(scrapes[-1])
    assert all(events.index("close1") < events.index(s) for s in scrapes)
    # Stores run in external_id-sorted order (consistent lock ordering -> no ABBA deadlock).
    assert stores == ["store:aaa", "store:bbb", "store:ccc"]


@patch("phaze.tasks.tracklist._store_scraped_tracklist", new_callable=AsyncMock)
@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
@patch("phaze.tasks.tracklist.compute_match_confidence", return_value=10)
@patch("phaze.tasks.tracklist.should_auto_link", return_value=False)
async def test_search_tracklist_one_empty_rescrape_does_not_roll_back_siblings(
    _mock_auto_link: MagicMock,
    _mock_conf: MagicMock,
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
    mock_store: AsyncMock,
) -> None:
    """phaze-g2j3: a single EmptyScrapeError in the batch store loop must not roll back the
    already-stored sibling results, and the whole job must not fail.

    Pre-fix, phase 3 stored every result in ONE transaction with no per-item error handling: the
    guard EmptyScrapeError (phaze-gfyr) raised for ONE poisoned result escaped the ``async with``
    block, discarding every OTHER already-stored good result in the same batch and forcing SAQ to
    retry the ENTIRE search. The fix catches it per item and keeps going.
    """
    file_record = _make_file_record()
    mock_parse.return_value = ("Artist", "Coachella", date(2024, 4, 14))

    ids = ["aaa", "bbb", "ccc"]
    search_results = [_make_search_result(external_id=eid) for eid in ids]
    scraped_by_url = {r.url: _make_scraped_tracklist(external_id=eid) for eid, r in zip(ids, search_results, strict=True)}

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record

    session = AsyncMock()
    session.add = MagicMock()
    session.execute.return_value = mock_file_result
    session.commit = AsyncMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    async def _scrape_recording(url: str) -> Any:
        return scraped_by_url[url]

    stored: list[str] = []

    async def _store_recording(_session: Any, scraped: Any, **_kwargs: Any) -> Any:
        # "bbb" is the poisoned sibling: its page soft-blocked over existing data.
        if scraped.external_id == "bbb":
            raise EmptyScrapeError(scraped.external_id)
        stored.append(scraped.external_id)
        return scraped

    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = search_results
    mock_scraper.scrape_tracklist.side_effect = _scrape_recording
    mock_scraper_cls.return_value = mock_scraper
    mock_store.side_effect = _store_recording

    ctx = {"async_session": lambda: cm}
    result = await search_tracklist(ctx, file_id=str(file_record.id))

    # The good siblings were both attempted and the job reports success, not a failure.
    assert result["results_found"] == 3
    assert stored == ["aaa", "ccc"]
    # The one transaction commits despite the mid-loop skip -- the good stores are NOT rolled back.
    session.commit.assert_awaited_once()


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


def test_refresh_tracklists_cron_has_unbounded_timeout() -> None:
    """refresh_tracklists' CronJob must carry an explicit unbounded (0) timeout (phaze-tkd0).

    Regression guard: with no explicit timeout, SAQ's Worker.schedule() enqueues the cron Job at
    the SAQ-library default (10s) and apply_project_job_defaults (a before_enqueue hook that also
    fires for cron-scheduled jobs) only raises that to worker_job_timeout (600s). The task body
    loops over every stale/unresolved tracklist with a 60-300s jitter sleep plus an 8-12s
    rate-limited scrape per item (~200s/item average) -- any run touching more than ~3 candidates
    was hard-cancelled at the 600s wall and never reached the rest of the matched set. An explicit
    ``timeout=0`` is NOT a SAQ default, so apply_project_job_defaults leaves it alone (mirrors the
    scan_directory pattern).
    """
    from phaze.tasks.controller import settings as controller_settings
    from phaze.tasks.tracklist import refresh_tracklists

    refresh_crons = [cj for cj in controller_settings["cron_jobs"] if cj.function is refresh_tracklists]
    assert len(refresh_crons) == 1, "refresh_tracklists must be registered as exactly one CronJob"
    assert refresh_crons[0].timeout == 0


# --- phaze-hu8v: persistent "cached and never re-fetched" tests -----------------------------


async def test_find_cached_tracklists_returns_empty_for_empty_ids_without_querying() -> None:
    """An empty external_ids list short-circuits without touching the session (phaze-hu8v)."""
    session = AsyncMock()

    result = await _find_cached_tracklists(session, [])

    assert result == {}
    session.execute.assert_not_called()


async def test_find_cached_tracklists_keys_by_external_id() -> None:
    """Rows with a resolved latest_version_id come back keyed by external_id."""
    session = AsyncMock()
    tl_a = _make_cached_tracklist(external_id="aaa")
    tl_b = _make_cached_tracklist(external_id="bbb")
    scalars_result = MagicMock()
    scalars_result.all.return_value = [tl_a, tl_b]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute.return_value = execute_result

    result = await _find_cached_tracklists(session, ["aaa", "bbb", "ccc"])

    assert result == {"aaa": tl_a, "bbb": tl_b}
    # Only ONE bulk query for the whole batch, not one per external_id.
    session.execute.assert_awaited_once()


async def test_find_cached_tracklists_filters_to_resolved_latest_version() -> None:
    """The query requires a resolved ``latest_version_id`` AND at least one track (phaze-hu8v).

    A Tracklist row can exist with no version yet (created but never successfully scraped) --
    excluded by ``latest_version_id IS NOT NULL``. A row CAN also have a resolved
    ``latest_version_id`` whose version has ZERO tracks (a first-ever scrape that soft-blocked or
    hit selector drift, phaze-gfyr's empty-rescrape guard only protects a SECOND scrape over
    EXISTING data) -- excluded by the correlated EXISTS-track subquery. Both conditions are
    asserted against the COMPILED SQL here (proving the query shape); real execution semantics
    against Postgres are covered by ``tests/integration/test_tracklist_cache_real_db.py``, since a
    mock can't catch a correlated-subquery mistake the way a real database can.
    """
    session = AsyncMock()
    execute_result = MagicMock()
    session.execute.return_value = execute_result

    await _find_cached_tracklists(session, ["some-id"])

    compiled = str(session.execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "latest_version_id IS NOT NULL" in compiled
    assert "external_id IN" in compiled
    assert "EXISTS" in compiled
    assert "tracklist_tracks" in compiled


def test_apply_file_link_links_when_unowned() -> None:
    """phaze-4a5w (shared helper, phaze-hu8v): links onto an unowned tracklist."""
    tracklist = MagicMock()
    tracklist.file_id = None
    file_id = uuid.uuid4()

    _apply_file_link(tracklist, file_id, confidence=95, auto_linked=True)

    assert tracklist.file_id == file_id
    assert tracklist.match_confidence == 95
    assert tracklist.auto_linked is True


def test_apply_file_link_does_not_steal_existing_link() -> None:
    """phaze-4a5w (shared helper, phaze-hu8v): refuses to overwrite a DIFFERENT file's link."""
    tracklist = MagicMock()
    owner_file_id = uuid.uuid4()
    tracklist.file_id = owner_file_id
    tracklist.match_confidence = 77
    tracklist.auto_linked = False

    _apply_file_link(tracklist, uuid.uuid4(), confidence=99, auto_linked=True)

    assert tracklist.file_id == owner_file_id
    assert tracklist.match_confidence == 77
    assert tracklist.auto_linked is False


async def test_link_cached_tracklist_links_when_unowned() -> None:
    """A cache-hit link-only update applies file linkage without creating a version (phaze-hu8v)."""
    session = AsyncMock()
    tracklist = _make_cached_tracklist(external_id="unowned-cached", file_id=None)
    result = MagicMock()
    result.scalar_one_or_none.return_value = tracklist
    session.execute.return_value = result

    file_id = uuid.uuid4()
    await _link_cached_tracklist(session, "unowned-cached", file_id, confidence=92, auto_linked=True)

    assert tracklist.file_id == file_id
    assert tracklist.match_confidence == 92
    assert tracklist.auto_linked is True
    session.add.assert_not_called()  # no new version/track rows -- link-only


async def test_link_cached_tracklist_does_not_steal_existing_link() -> None:
    """A cache-hit link-only update never steals a manually-linked tracklist (phaze-4a5w/hu8v)."""
    session = AsyncMock()
    owner_file_id = uuid.uuid4()
    tracklist = _make_cached_tracklist(external_id="owned-cached", file_id=owner_file_id)
    tracklist.match_confidence = 60
    tracklist.auto_linked = False
    result = MagicMock()
    result.scalar_one_or_none.return_value = tracklist
    session.execute.return_value = result

    await _link_cached_tracklist(session, "owned-cached", uuid.uuid4(), confidence=99, auto_linked=True)

    assert tracklist.file_id == owner_file_id
    assert tracklist.match_confidence == 60
    assert tracklist.auto_linked is False


async def test_link_cached_tracklist_missing_row_logs_and_returns() -> None:
    """A race where the cached row vanishes before the link write is handled gracefully."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result

    # Must not raise.
    await _link_cached_tracklist(session, "vanished", uuid.uuid4(), confidence=90, auto_linked=True)


@patch("phaze.tasks.tracklist._find_cached_tracklists")
@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
@patch("phaze.tasks.tracklist.compute_match_confidence", return_value=50)
@patch("phaze.tasks.tracklist.should_auto_link", return_value=False)
async def test_search_tracklist_skips_network_scrape_for_cached_result(
    _mock_auto_link: MagicMock,
    mock_confidence: MagicMock,
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
    mock_find_cached: AsyncMock,
) -> None:
    """A search result whose external_id is already cached is never re-fetched (phaze-hu8v).

    This is the acceptance criterion itself: "scraped tracklists are cached and never
    re-fetched" -- scrape_tracklist() must not be awaited at all for the cached result.
    """
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()
    mock_parse.return_value = ("Artist", "Coachella", date(2024, 4, 14))

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record
    session.execute.return_value = mock_file_result

    search_result = _make_search_result(external_id="already-scraped")
    cached_tracklist = _make_cached_tracklist(external_id="already-scraped", artist="Cached Artist", event="Cached Event")
    mock_find_cached.return_value = {"already-scraped": cached_tracklist}

    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = [search_result]
    mock_scraper_cls.return_value = mock_scraper

    result = await search_tracklist(ctx, file_id=str(file_record.id))

    assert result["results_found"] == 1
    mock_scraper.scrape_tracklist.assert_not_awaited()
    mock_confidence.assert_called_once()
    assert mock_confidence.call_args.kwargs["tracklist_artist"] == "Cached Artist"
    assert mock_confidence.call_args.kwargs["tracklist_event"] == "Cached Event"
    assert mock_confidence.call_args.kwargs["tracklist_date"] == date(2024, 4, 14)


@patch("phaze.tasks.tracklist._link_cached_tracklist", new_callable=AsyncMock)
@patch("phaze.tasks.tracklist._store_scraped_tracklist", new_callable=AsyncMock)
@patch("phaze.tasks.tracklist._find_cached_tracklists")
@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
@patch("phaze.tasks.tracklist.compute_match_confidence", return_value=100)
@patch("phaze.tasks.tracklist.should_auto_link", return_value=True)
async def test_search_tracklist_cache_hit_auto_link_uses_link_only_path(
    _mock_auto_link: MagicMock,
    _mock_confidence: MagicMock,
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
    mock_find_cached: AsyncMock,
    mock_store: AsyncMock,
    mock_link_cached: AsyncMock,
) -> None:
    """A cache-hit that auto-links calls the link-only path, never the version-creating store
    path (phaze-hu8v) -- re-persisting identical data on every rediscovery would bloat
    tracklist_versions/tracklist_tracks for zero benefit.
    """
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()
    mock_parse.return_value = ("Artist", "Coachella", date(2024, 4, 14))

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record
    session.execute.return_value = mock_file_result

    search_result = _make_search_result(external_id="cached-autolink")
    cached_tracklist = _make_cached_tracklist(external_id="cached-autolink", tracklist_date=date(2024, 4, 14))
    mock_find_cached.return_value = {"cached-autolink": cached_tracklist}

    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = [search_result]
    mock_scraper_cls.return_value = mock_scraper

    result = await search_tracklist(ctx, file_id=str(file_record.id))

    assert result["auto_linked"] is True
    mock_scraper.scrape_tracklist.assert_not_awaited()
    mock_store.assert_not_awaited()
    mock_link_cached.assert_awaited_once()
    assert mock_link_cached.await_args.args[1] == "cached-autolink"
    assert mock_link_cached.await_args.args[2] == file_record.id


@patch("phaze.tasks.tracklist._find_cached_tracklists")
@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
@patch("phaze.tasks.tracklist.compute_match_confidence", return_value=50)
@patch("phaze.tasks.tracklist.should_auto_link", return_value=False)
async def test_search_tracklist_mixed_cache_hit_and_miss_only_scrapes_uncached(
    _mock_auto_link: MagicMock,
    _mock_confidence: MagicMock,
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
    mock_find_cached: AsyncMock,
) -> None:
    """Of two results, only the one NOT already cached triggers a network scrape (phaze-hu8v)."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()
    mock_parse.return_value = ("Artist", "Coachella", date(2024, 4, 14))

    mock_file_result = MagicMock()
    mock_file_result.scalar_one_or_none.return_value = file_record
    session.execute.return_value = mock_file_result

    cached_result = _make_search_result(external_id="cached-one")
    fresh_result = _make_search_result(external_id="fresh-one")
    cached_tracklist = _make_cached_tracklist(external_id="cached-one")
    mock_find_cached.return_value = {"cached-one": cached_tracklist}

    fresh_scraped = _make_scraped_tracklist(external_id="fresh-one")
    mock_scraper = AsyncMock()
    mock_scraper.search.return_value = [cached_result, fresh_result]
    mock_scraper.scrape_tracklist.return_value = fresh_scraped
    mock_scraper_cls.return_value = mock_scraper

    result = await search_tracklist(ctx, file_id=str(file_record.id))

    assert result["results_found"] == 2
    mock_scraper.scrape_tracklist.assert_awaited_once_with(fresh_result.url)
    # The bulk cache-check is called with BOTH external_ids, in one call, before any scrape.
    mock_find_cached.assert_awaited_once_with(session, ["cached-one", "fresh-one"])


@patch("phaze.tasks.tracklist._find_cached_tracklists", new_callable=AsyncMock)
@patch("phaze.tasks.tracklist.TracklistScraper")
@patch("phaze.tasks.tracklist.parse_live_set_filename")
async def test_search_tracklist_no_results_skips_cache_check(
    mock_parse: MagicMock,
    mock_scraper_cls: MagicMock,
    mock_find_cached: AsyncMock,
) -> None:
    """No search results means no cache-check query is ever issued (phaze-hu8v)."""
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

    await search_tracklist(ctx, file_id=str(file_record.id))

    mock_find_cached.assert_not_awaited()
