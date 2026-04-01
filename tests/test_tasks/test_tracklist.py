"""Tests for 1001Tracklists arq task functions."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from phaze.tasks.tracklist import refresh_tracklists, scrape_and_store_tracklist, search_tracklist


def _make_ctx(job_try: int = 1) -> dict[str, Any]:
    """Create a minimal arq context dict with async_session factory."""
    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return {"job_try": job_try, "async_session": mock_session_factory, "_mock_session": mock_session}


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

    result = await search_tracklist(ctx, str(file_record.id))

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

    result = await search_tracklist(ctx, str(file_record.id))

    assert result["auto_linked"] is True
    mock_scraper.close.assert_awaited_once()


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

    result = await search_tracklist(ctx, str(file_record.id))

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

    result = await search_tracklist(ctx, str(file_record.id))

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

    session.execute.side_effect = [mock_tl_result, mock_existing_result, mock_version_result, mock_version_result]

    scraped = _make_scraped_tracklist()
    mock_scraper = AsyncMock()
    mock_scraper.scrape_tracklist.return_value = scraped
    mock_scraper_cls.return_value = mock_scraper

    result = await scrape_and_store_tracklist(ctx, str(tracklist_id))

    assert result["tracklist_id"] == str(tracklist_id)
    mock_scraper.scrape_tracklist.assert_awaited_once()
    mock_scraper.close.assert_awaited_once()


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


def test_worker_settings_contains_tracklist_functions() -> None:
    """WorkerSettings.functions includes search_tracklist and scrape_and_store_tracklist."""
    from phaze.tasks.worker import WorkerSettings

    func_names = [f.__name__ if hasattr(f, "__name__") else str(f) for f in WorkerSettings.functions]
    assert "search_tracklist" in func_names
    assert "scrape_and_store_tracklist" in func_names


def test_worker_settings_has_cron_jobs() -> None:
    """WorkerSettings.cron_jobs includes refresh_tracklists cron."""
    from phaze.tasks.worker import WorkerSettings

    assert hasattr(WorkerSettings, "cron_jobs")
    assert len(WorkerSettings.cron_jobs) >= 1
    # Check the cron job has the right function
    cron_job = WorkerSettings.cron_jobs[0]
    assert cron_job.coroutine.__name__ == "refresh_tracklists"
