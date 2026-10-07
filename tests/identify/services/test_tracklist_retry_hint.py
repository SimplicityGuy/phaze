"""A persisted detail-page URL lets the retry of a transient render failure skip the search (phaze-w5fni).

**ZERO live requests.** Searches are served by the REAL ``TracklistScraper`` over an ``httpx.MockTransport``
returning a recorded results capture, and renders by a fake that reserves a host slot exactly as the real
renderer does. Host requests are counted where the real code counts them: at
``reserve_host_request_slot``, patched with a counter that does not sleep.

Per-outcome reasoning for what is persisted (the hint is a HINT, never a result):

* ``BLOCKED`` / ``RENDER_FAILED`` -- persisted. The page was never fetched successfully, so the choice of
  page is the one thing the attempt learned and a retry should not pay to re-derive it.
* ``DECOY`` -- NOT persisted. The site served a flagged-client page for that URL; offering it again would
  re-ask for the same decoy.
* ``PARSE_FAILED`` -- NOT persisted. The page WAS fetched and our selectors failed; the URL is not the problem.
* ``SEARCH_FAILED`` -- nothing to persist: no page was ever chosen.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy import func, select, update

from phaze.enums.tracklist_candidate import RETRY_HINT_OUTCOMES, TRANSIENT_MAX_ATTEMPTS, CacheDecision, LookupOutcome
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist
from phaze.models.tracklist_lookup_cache import TracklistLookupCache
from phaze.services import tracklist_drain, tracklist_scraper
from phaze.services.tracklist_drain import drain_once, perform_lookup, persist_lookup
from phaze.services.tracklist_lookup_cache import lookup, record_outcome, retry_confidence_for, retry_hint_for
from phaze.services.tracklist_render import RenderOutcome, RenderResult
from phaze.services.tracklist_scraper import TracklistScraper
from tests.identify.services.test_tracklist_decoy import fabricate_synthetic_decoy
from tests.identify.services.test_tracklist_drain import (
    ANCHOR_FILENAME,
    ANCHOR_ID,
    SEARCH_FIXTURES,
    FakeRenderer,
    FakeSearch,
    candidate_for,
    load_render,
    load_search,
    session_factory_for,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
ANCHOR_URL = next(r.url for r in load_search("time-warp-2024") if r.external_id == ANCHOR_ID)
WRONG_ID = "19h6nw7t"
"""A real capture of a DIFFERENT set (a BBC broadcast of an earlier Time Warp show): same artist and
festival as the anchor, which is what makes it a hard wrong-hint case rather than an easy one."""
WRONG_URL = f"https://www.1001tracklists.com/tracklist/{WRONG_ID}/sven-vath-bbc-radio-1-dance-presents-time-warp-2024-10-12.html"


class HostSlots:
    """Stands in for ``reserve_host_request_slot``: counts reservations by kind, never sleeps."""

    def __init__(self) -> None:
        self.kind = "search"
        self.reservations: list[str] = []

    async def reserve(self) -> None:
        self.reservations.append(self.kind)

    def count(self, kind: str) -> int:
        return self.reservations.count(kind)


class SlotRenderer:
    """A renderer that reserves one host slot per navigation, as ``tracklist_render`` does."""

    def __init__(self, slots: HostSlots, *, html: str = "", outcome: RenderOutcome = RenderOutcome.OK, attempts: int = 1) -> None:
        self._slots = slots
        self._html = html
        self._outcome = outcome
        self._attempts = attempts
        self.urls: list[str] = []

    async def render(self, url: str) -> RenderResult:
        self.urls.append(url)
        for _ in range(self._attempts):
            self._slots.kind = "render"
            await self._slots.reserve()
            self._slots.kind = "search"
        return RenderResult(url=url, outcome=self._outcome, html=self._html, attempts=self._attempts, elapsed_seconds=1.0, error="boom")


@pytest.fixture
def slots(monkeypatch: pytest.MonkeyPatch) -> HostSlots:
    counter = HostSlots()
    monkeypatch.setattr(tracklist_scraper, "reserve_host_request_slot", counter.reserve)
    monkeypatch.setattr(TracklistScraper, "_search_cache", tracklist_scraper._TTLCache(60.0))
    return counter


@pytest.fixture
async def real_search() -> Any:
    """The real scraper over a mock transport that serves the recorded results page."""
    page = (SEARCH_FIXTURES / "time-warp-2024.html").read_text(encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=page)))
    yield TracklistScraper(client=client)
    await client.aclose()


async def _seed(session: AsyncSession, make_file: Any) -> FileRecord:
    file = await make_file(original_filename=ANCHOR_FILENAME)
    session.add(FileMetadata(file_id=file.id, duration=3600.0))
    await session.flush()
    return file


async def _cache_row(session: AsyncSession) -> TracklistLookupCache:
    return (await session.execute(select(TracklistLookupCache))).scalar_one()


async def _make_retry_ready(session: AsyncSession) -> None:
    await session.execute(update(TracklistLookupCache).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    await session.flush()


class TestRetryAfterTransientFailure:
    async def test_retry_after_blocked_issues_no_search(self, session: AsyncSession, make_file: Any, slots: HostSlots, real_search: Any) -> None:
        """Zero search requests and exactly one render request on the retry, counted at the host slot."""
        await _seed(session, make_file)
        factory = session_factory_for(session)

        blocked = SlotRenderer(slots, outcome=RenderOutcome.INTERSTITIAL_PERSISTED)
        first = await drain_once(factory, search=real_search, renderer=blocked, limit=5, now=NOW)
        assert first.outcomes == {LookupOutcome.BLOCKED.value: 1}
        assert (slots.count("search"), slots.count("render")) == (1, 1), "the first attempt pays for the search AND the render"
        assert (await _cache_row(session)).retry_url == ANCHOR_URL

        await _make_retry_ready(session)
        slots.reservations.clear()
        retry = SlotRenderer(slots, html=load_render(ANCHOR_ID))
        second = await drain_once(factory, search=real_search, renderer=retry, limit=5, now=datetime.now(UTC))

        assert second.outcomes == {LookupOutcome.FOUND.value: 1}
        assert slots.count("search") == 0, "the retry must not search"
        assert slots.count("render") == 1, "the retry spends exactly one render"
        assert retry.urls == [ANCHOR_URL]
        assert second.host_requests == 1
        assert (await _cache_row(session)).retry_url is None, "cleared on success"

    async def test_a_hint_is_honoured_only_when_the_backoff_has_elapsed(self, session: AsyncSession) -> None:
        entry = await record_outcome(
            session, set_key="k" * 64, query_text="q", outcome=LookupOutcome.BLOCKED, source_url=ANCHOR_URL, retry_url=ANCHOR_URL, now=NOW
        )
        await session.flush()
        assert entry.expires_at is not None
        assert retry_hint_for(await lookup(session, entry.set_key, now=NOW)) is None, "inside the backoff"
        ready = await lookup(session, entry.set_key, now=entry.expires_at + timedelta(minutes=1))
        assert ready.decision is CacheDecision.TRANSIENT_RETRY_READY
        assert retry_hint_for(ready) == ANCHOR_URL


class TestPersistedUrlIsNotAResult:
    async def test_persisted_url_is_not_a_result(self, session: AsyncSession, make_file: Any) -> None:
        """Never FOUND, never FOUND in the cache, cleared on success and when the set is parked."""
        file = await _seed(session, make_file)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(file.id, file.sha256_hash)])

        # 1. A blocked attempt stores the hint -- as a BLOCKED row, which is not a result.
        blocked = await perform_lookup(
            candidate, search=FakeSearch("time-warp-2024"), renderer=FakeRenderer(outcome=RenderOutcome.INTERSTITIAL_PERSISTED)
        )
        await persist_lookup(session, candidate, blocked, now=NOW)
        await session.flush()
        verdict = await lookup(session, candidate.set_key, now=NOW)
        assert verdict.entry is not None
        assert verdict.entry.retry_url == ANCHOR_URL
        assert verdict.entry.outcome == LookupOutcome.BLOCKED.value
        assert verdict.decision is not CacheDecision.HIT_POSITIVE
        assert verdict.external_id is None, "a hint never answers 'what is the id of this set'"
        assert (await session.execute(select(func.count()).select_from(Tracklist))).scalar_one() == 0

        # 2. Re-rendering the hint with a still-blocked page is a transient, never a FOUND.
        again = await perform_lookup(
            candidate,
            search=FakeSearch(raises=AssertionError("no search")),
            renderer=FakeRenderer(outcome=RenderOutcome.TIMEOUT),
            retry_url=ANCHOR_URL,
        )
        assert again.outcome is LookupOutcome.RENDER_FAILED
        assert not again.is_found
        assert again.source_url == ANCHOR_URL, "the hint survives another transient"

        # 3. Cleared on success.
        found = await perform_lookup(
            candidate, search=FakeSearch(raises=AssertionError("no search")), renderer=FakeRenderer(html=load_render(ANCHOR_ID)), retry_url=ANCHOR_URL
        )
        assert found.outcome is LookupOutcome.FOUND
        await persist_lookup(session, candidate, found, now=NOW + timedelta(hours=1))
        await session.flush()
        assert (await _cache_row(session)).retry_url is None

    async def test_the_hint_is_cleared_when_the_set_is_parked(self, session: AsyncSession) -> None:
        moment = NOW
        for attempt in range(1, TRANSIENT_MAX_ATTEMPTS + 1):
            entry = await record_outcome(
                session, set_key="p" * 64, query_text="q", outcome=LookupOutcome.BLOCKED, source_url=ANCHOR_URL, retry_url=ANCHOR_URL, now=moment
            )
            await session.flush()
            assert entry.attempts == attempt
            if attempt < TRANSIENT_MAX_ATTEMPTS:
                assert entry.retry_url == ANCHOR_URL
                assert entry.expires_at is not None
                moment = entry.expires_at + timedelta(minutes=1)
        assert entry.retry_url is None, "the write that parks the set clears the hint"
        parked = await lookup(session, entry.set_key, now=moment + timedelta(days=30))
        assert parked.decision is CacheDecision.TRANSIENT_EXHAUSTED
        assert retry_hint_for(parked) is None

    @pytest.mark.parametrize("outcome", sorted(set(LookupOutcome) - RETRY_HINT_OUTCOMES, key=lambda o: o.value))
    async def test_only_blocked_and_render_failed_ever_store_a_hint(self, session: AsyncSession, outcome: LookupOutcome) -> None:
        """Including DECOY: a decoy page's URL is never reusable, and a later write clears an earlier hint."""
        await record_outcome(session, set_key="d" * 64, query_text="q", outcome=LookupOutcome.BLOCKED, retry_url=ANCHOR_URL, now=NOW)
        entry = await record_outcome(session, set_key="d" * 64, query_text="q", outcome=outcome, source_url=ANCHOR_URL, retry_url=ANCHOR_URL, now=NOW)
        assert entry.retry_url is None
        fresh = await record_outcome(session, set_key="e" * 64, query_text="q", outcome=outcome, source_url=ANCHOR_URL, retry_url=ANCHOR_URL, now=NOW)
        assert fresh.retry_url is None

    async def test_a_decoy_on_the_retry_is_a_decoy_and_drops_the_hint(self, session: AsyncSession, make_file: Any) -> None:
        file = await _seed(session, make_file)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(file.id, file.sha256_hash)])
        renderer = FakeRenderer(html=fabricate_synthetic_decoy(load_render(ANCHOR_ID)))

        attempt = await perform_lookup(candidate, search=FakeSearch(raises=AssertionError("no search")), renderer=renderer, retry_url=ANCHOR_URL)
        assert attempt.outcome is LookupOutcome.DECOY
        await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()
        assert (await _cache_row(session)).retry_url is None


class TestWrongPersistedUrlIsDiscarded:
    async def test_wrong_persisted_url_is_discarded(self, session: AsyncSession, make_file: Any) -> None:
        """A hint whose page fails the existing scoring falls back to a fresh search; the wrong page is never stored."""
        file = await _seed(session, make_file)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(file.id, file.sha256_hash)])

        class PageByUrl:
            def __init__(self) -> None:
                self.urls: list[str] = []

            async def render(self, url: str) -> RenderResult:
                self.urls.append(url)
                html = load_render(WRONG_ID if url == WRONG_URL else ANCHOR_ID)
                return RenderResult(url=url, outcome=RenderOutcome.OK, html=html, attempts=1, elapsed_seconds=1.0)

        renderer = PageByUrl()
        search = FakeSearch("time-warp-2024")
        attempt = await perform_lookup(candidate, search=search, renderer=renderer, retry_url=WRONG_URL)

        assert renderer.urls == [WRONG_URL, ANCHOR_URL], "the wrong page was looked at, discarded, then the searched page rendered"
        assert len(search.queries) == 1, "a fresh search ran"
        assert attempt.outcome is LookupOutcome.FOUND
        assert attempt.external_id == ANCHOR_ID
        assert attempt.host_requests == 3, "the discarded hint's render + the search + the real render, all counted"
        await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()
        stored = (await session.execute(select(Tracklist.external_id))).scalars().all()
        assert stored == [ANCHOR_ID], "the mismatched page was never stored"

    @pytest.mark.parametrize(
        ("url", "html"),
        [
            pytest.param(ANCHOR_URL, "<html><body>no headline here</body></html>", id="unreadable-page"),
            pytest.param("https://evil.example/tracklist/25fhn7c9/x.html", load_render(ANCHOR_ID), id="off-host-hint"),
        ],
    )
    async def test_an_unverifiable_hint_falls_back_to_search(self, url: str, html: str) -> None:
        search = FakeSearch("time-warp-2024")
        renderer = FakeRenderer(html=html)
        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=renderer, retry_url=url)
        assert len(search.queries) == 1
        assert attempt.external_id == ANCHOR_ID
        if url.startswith("https://evil"):
            assert renderer.urls == [ANCHOR_URL], "an off-allow-list hint is never rendered"

    async def test_a_no_tracklist_page_for_a_wrong_hint_is_not_cached_as_a_negative(self) -> None:
        """A wrong page with no track container must not become a 180-day NOT_FOUND for this set."""
        wrong = load_render(WRONG_ID)
        renderer = FakeRenderer(html=wrong, outcome=RenderOutcome.NO_TRACKLIST)
        search = FakeSearch("time-warp-2024")
        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=renderer, retry_url=WRONG_URL)
        assert len(search.queries) == 1
        assert renderer.urls == [WRONG_URL, ANCHOR_URL]
        assert attempt.outcome is LookupOutcome.NOT_FOUND, "the SEARCHED page's own verdict, not the wrong hint's"
        assert attempt.external_id == ANCHOR_ID


class TestDetailPageResult:
    def test_the_headline_reads_like_a_search_row(self) -> None:
        row = TracklistScraper.detail_page_result(load_render(ANCHOR_ID), ANCHOR_URL)
        assert row is not None
        assert (row.external_id, row.artist, row.date) == (ANCHOR_ID, "Sven Väth", "2024-10-25")
        assert row.event == "Time Warp, Maimarkthalle Mannheim, Germany"

    def test_the_trailing_date_wins_over_a_quoted_one(self) -> None:
        row = TracklistScraper.detail_page_result(load_render(WRONG_ID), WRONG_URL)
        assert row is not None
        assert row.date == "2024-10-12"

    def test_no_headline_is_unverifiable(self) -> None:
        assert TracklistScraper.detail_page_result("<html></html>", ANCHOR_URL) is None


class TestRetryKeepsTheOriginalConfidence:
    async def test_hint_retry_preserves_result_confidence(self, session: AsyncSession, make_file: Any, slots: HostSlots, real_search: Any) -> None:
        """A BLOCKED then a RENDER_FAILED hint retry leave the confidence the ORIGINAL search stored (review finding #3).

        The cache row's own ``result_confidence`` is the carrier: the transient write is the only thing
        that could erase it, and the retry now writes it back instead of ``None``. No new column.
        """
        await _seed(session, make_file)
        factory = session_factory_for(session)

        await drain_once(factory, search=real_search, renderer=SlotRenderer(slots, outcome=RenderOutcome.INTERSTITIAL_PERSISTED), limit=5, now=NOW)
        original = (await _cache_row(session)).result_confidence
        assert original is not None, "the searched path stored the chosen row's confidence"

        for outcome, expected in (
            (RenderOutcome.INTERSTITIAL_PERSISTED, LookupOutcome.BLOCKED),
            (RenderOutcome.TIMEOUT, LookupOutcome.RENDER_FAILED),
        ):
            await _make_retry_ready(session)
            slots.reservations.clear()
            report = await drain_once(factory, search=real_search, renderer=SlotRenderer(slots, outcome=outcome), limit=5, now=datetime.now(UTC))
            assert report.outcomes == {expected.value: 1}
            assert slots.count("search") == 0, "this was a hint retry"
            row = await _cache_row(session)
            await session.refresh(row)
            assert row.retry_url == ANCHOR_URL
            assert row.result_confidence == original

    async def test_the_confidence_only_travels_with_a_usable_hint(self, session: AsyncSession) -> None:
        entry = await record_outcome(
            session, set_key="c" * 64, query_text="q", outcome=LookupOutcome.BLOCKED, retry_url=ANCHOR_URL, result_confidence=77, now=NOW
        )
        await session.flush()
        assert entry.expires_at is not None
        assert retry_confidence_for(await lookup(session, entry.set_key, now=NOW)) is None, "inside the backoff"
        assert retry_confidence_for(await lookup(session, entry.set_key, now=entry.expires_at + timedelta(minutes=1))) == 77


class TestHintPathMatchesSearchedPath:
    @pytest.mark.parametrize("outcome", [o for o in RenderOutcome if o is not RenderOutcome.OK], ids=lambda o: o.value)
    async def test_hint_path_failure_attempts_match_searched_path(self, outcome: RenderOutcome) -> None:
        """Same outcome, external_id, source_url, confidence and detail for the same render, however it was reached."""
        html = load_render(ANCHOR_ID)
        searched = await perform_lookup(
            candidate_for(ANCHOR_FILENAME), search=FakeSearch("time-warp-2024"), renderer=FakeRenderer(html=html, outcome=outcome)
        )
        hinted = await perform_lookup(
            candidate_for(ANCHOR_FILENAME),
            search=FakeSearch(raises=AssertionError("no search")),
            renderer=FakeRenderer(html=html, outcome=outcome),
            retry_url=ANCHOR_URL,
            retry_confidence=searched.result_confidence,
        )
        assert searched.result_confidence is not None
        assert hinted.host_requests == searched.host_requests - 1, "the only difference is the search the hint skipped"
        assert hinted == replace(searched, host_requests=hinted.host_requests)

    async def test_a_raising_render_matches_the_searched_path(self) -> None:
        raising = RuntimeError("no display")
        searched = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=FakeSearch("time-warp-2024"), renderer=FakeRenderer(raises=raising))
        hinted = await perform_lookup(
            candidate_for(ANCHOR_FILENAME),
            search=FakeSearch(raises=AssertionError("no search")),
            renderer=FakeRenderer(raises=raising),
            retry_url=ANCHOR_URL,
            retry_confidence=searched.result_confidence,
        )
        assert searched.outcome is LookupOutcome.RENDER_FAILED
        assert hinted == replace(searched, host_requests=hinted.host_requests)
        assert hinted.host_requests == searched.host_requests - 1


class TestDrainUsesOnlyPublicScraperNames:
    def test_drain_does_not_import_private_scraper_names(self) -> None:
        """tracklist_drain.py may not import, or reach through ``TracklistScraper`` for, an underscore-prefixed name."""
        source = Path(tracklist_drain.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        private_imports = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "phaze.services.tracklist_scraper"
            for alias in node.names
            if alias.name.startswith("_")
        ]
        private_attributes = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "TracklistScraper"
            and node.attr.startswith("_")
        ]
        assert private_imports == []
        assert private_attributes == []


class TestDetailUrlExternalId:
    def test_an_allowed_detail_url_yields_its_id(self) -> None:
        assert TracklistScraper.detail_url_external_id(ANCHOR_URL) == ANCHOR_ID

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example/tracklist/25fhn7c9/x.html",
            "http://www.1001tracklists.com/tracklist/25fhn7c9/x.html",
            "https://www.1001tracklists.com/dj/x/",
        ],
    )
    def test_off_allow_list_or_idless_urls_yield_none(self, url: str) -> None:
        assert TracklistScraper.detail_url_external_id(url) is None
