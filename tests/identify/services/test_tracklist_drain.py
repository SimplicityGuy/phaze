"""The 1001Tracklists drain end to end (phaze-fq9h.7), against recorded pages only.

**ZERO live requests.** Every search in this file is served from the six real captures in
``tests/identify/fixtures/tracklist_search/`` (180 rows, 6 host requests) and every render from the
two real captures in ``tests/identify/fixtures/tracklist_render/`` (52-track anchor + 12-track
short listing). Those bytes cost real requests against an 8 s whole-host budget; they exist so the
drain can be developed and regression-tested without ever spending another one.

The suite is organized around the three things that can actually go wrong here, because the
sequencing itself is the easy part:

* **the honesty boundary** -- a transient failure recorded as a real negative is silent, permanent
  data loss, so every non-FOUND path is asserted for which SIDE of that line it lands on;
* **the propagation gate** -- propagating across a heuristic duplicate link writes a wrong
  tracklist onto a genuinely different set and then marks it tracklisted, so it is never revisited;
* **resumption** -- the acceptance criterion is that stopping and restarting re-spends nothing,
  which is a claim about the cache and the pre-spend re-check, not about a checkpoint file.

Filenames are invented text describing public events; no archive identifier appears here
(CLAUDE.md's conventions).
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import func, select

from phaze.enums.tracklist_candidate import TRANSIENT_OUTCOMES, CacheDecision, DuplicateConfidence, LookupOutcome
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.tracklist_candidate_queue import QueuedCandidate
from phaze.services.tracklist_candidates import CandidateSignals, group_unique_sets
from phaze.services.tracklist_drain import (
    DEFAULT_PROPAGATION_MIN_CONFIDENCE,
    DrainCandidate,
    DrainReport,
    LookupAttempt,
    build_drain_queue,
    drain_once,
    perform_lookup,
    persist_lookup,
)
from phaze.services.tracklist_lookup_cache import CacheVerdict, lookup, record_outcome
from phaze.services.tracklist_query import derive_query
from phaze.services.tracklist_render import RenderOutcome, RenderResult
from phaze.services.tracklist_scraper import DisallowedScrapeHostError, SearchParseFailureError, TracklistScraper, TracklistSearchResult


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


SEARCH_FIXTURES = Path(__file__).parent.parent / "fixtures" / "tracklist_search"
RENDER_FIXTURES = Path(__file__).parent.parent / "fixtures" / "tracklist_render"

ANCHOR_ID = "25fhn7c9"
"""Sven Väth @ Time Warp, Maimarkthalle Mannheim, 2024-10-25 -- 52 tracks, the spike's anchor."""
ANCHOR_TRACKS = 52

# Invented scene-style filenames for public events. `ANCHOR_FILENAME` derives to
# "Sven Vath At Time Warp Mannheim 2024" with an unambiguous 2024-10-25; against the
# `time-warp-2024` capture -- whose TOP row is a different artist at the same festival, same year --
# the scorer picks the anchor at rank 4. That pairing is the whole reason this suite is fixture-fed.
ANCHOR_FILENAME = "Sven_Vath-Live_At_Time_Warp_Mannheim-2024-10-25-WEB-FLAC-GRVMSTR.mp3"
NO_MATCH_FILENAME = "Zzyzx_Quorum-Live_At_Nonesuch_Festival-2019-06-01-WEB-MP3-GRVMSTR.mp3"

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------------
# Fixture-backed fakes. Neither ever touches the network.
# --------------------------------------------------------------------------------------------


def load_search(slug: str) -> list[TracklistSearchResult]:
    """Parse a captured search page through the REAL scraper parser, not a hand-rolled one."""
    return TracklistScraper()._parse_search_results((SEARCH_FIXTURES / f"{slug}.html").read_text(encoding="utf-8"))


def load_render(external_id: str) -> str:
    """Return a captured detail page's HTML."""
    return (RENDER_FIXTURES / f"{external_id}-ok.html").read_text(encoding="utf-8")


class FakeSearch:
    """Serves a recorded results page. Records every query so "no second request" is assertable."""

    def __init__(self, slug: str | None = None, *, raises: Exception | None = None, rows: Sequence[TracklistSearchResult] | None = None) -> None:
        self._rows = list(rows) if rows is not None else (load_search(slug) if slug else [])
        self._raises = raises
        self.queries: list[str] = []

    async def search(self, query: str) -> list[TracklistSearchResult]:
        self.queries.append(query)
        if self._raises is not None:
            raise self._raises
        return list(self._rows)


class FakeRenderer:
    """Serves a recorded detail page, or a chosen non-OK outcome. Records every URL."""

    def __init__(
        self,
        *,
        html: str | None = None,
        outcome: RenderOutcome = RenderOutcome.OK,
        attempts: int = 1,
        raises: Exception | None = None,
    ) -> None:
        self._html = html or ""
        self._outcome = outcome
        self._attempts = attempts
        self._raises = raises
        self.urls: list[str] = []

    async def render(self, url: str) -> RenderResult:
        self.urls.append(url)
        if self._raises is not None:
            raise self._raises
        return RenderResult(url=url, outcome=self._outcome, html=self._html, attempts=self._attempts, elapsed_seconds=1.0, error="boom")


def new_sha() -> str:
    """A distinct 64-hex content hash. Distinct matters: equal sha256 IS the EXACT duplicate link."""
    return uuid.uuid4().hex + uuid.uuid4().hex


def candidate_for(filename: str, *, files: Sequence[tuple[uuid.UUID, str]] | None = None) -> DrainCandidate:
    """Build a DrainCandidate the way ``build_drain_queue`` would.

    ``files`` is ``(file_id, sha256)`` pairs. The hash is explicit rather than defaulted because
    equal sha256 is exactly what makes a duplicate link ``EXACT``, and a suite that let every
    fixture share a blank hash would silently test the wrong confidence tier everywhere.
    """
    specs = list(files) if files is not None else [(uuid.uuid4(), new_sha())]
    signals = [
        CandidateSignals(file_id=file_id, filename=filename, sha256_hash=sha, duration_seconds=3600.0, derived_query=derive_query(filename).query)
        for file_id, sha in specs
    ]
    unique_set = group_unique_sets(signals)[0]
    queued = QueuedCandidate(unique_set=unique_set, verdict=CacheVerdict(set_key=unique_set.key, decision=CacheDecision.MISS))
    return DrainCandidate(queued=queued, derived=derive_query(filename))


def anchor_lookup(**kwargs: Any) -> tuple[FakeSearch, FakeRenderer]:
    """The happy path: the wrong-match capture plus the anchor's real detail page."""
    return FakeSearch("time-warp-2024"), FakeRenderer(html=load_render(ANCHOR_ID), **kwargs)


# --------------------------------------------------------------------------------------------
# perform_lookup: the derive -> search -> score -> render -> parse sequence
# --------------------------------------------------------------------------------------------


class TestPerformLookupHappyPath:
    async def test_the_wrong_top_row_is_beaten_and_the_anchor_is_parsed(self) -> None:
        """One lookup, end to end, on real bytes: 30 real rows in, 52 real tracks out.

        The capture's TOP row is a different artist at the same festival in the same year (see the
        fixtures README). A drain that took it would have rendered a page with no tracklist and
        recorded a confident-looking nothing -- the spike's original failure.
        """
        search, renderer = anchor_lookup()
        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=renderer)

        assert attempt.outcome is LookupOutcome.FOUND
        assert attempt.external_id == ANCHOR_ID
        assert len(attempt.tracks) == ANCHOR_TRACKS
        assert attempt.tracks[0].artist == "Sven Väth"
        assert attempt.result_confidence == 84
        assert renderer.urls == [next(r.url for r in load_search("time-warp-2024") if r.external_id == ANCHOR_ID)]

    async def test_metadata_comes_from_the_selected_row_not_from_our_reading_of_the_filename(self) -> None:
        """A mis-selection has to stay recognisable afterwards, so the SITE's description is stored."""
        search, renderer = anchor_lookup()
        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=renderer)

        selected = next(r for r in load_search("time-warp-2024") if r.external_id == ANCHOR_ID)
        assert attempt.artist == selected.artist
        assert attempt.event == selected.event
        assert attempt.date is not None and attempt.date.isoformat() == selected.date

    async def test_host_requests_counts_the_search_and_every_navigation(self) -> None:
        """The ~2.5-requests-per-lookup figure every projection divides by must be measurable."""
        search, renderer = anchor_lookup(attempts=3)
        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=renderer)
        assert attempt.host_requests == 4  # 1 search + 3 navigations

    async def test_site_text_is_mojibake_repaired_at_the_ingest_boundary(self) -> None:
        """phaze-x4ux, carried onto the drain: repair the SITE's text ONCE, where it enters.

        ``artist`` and ``event`` are the exact columns ``tracklists.search_vector`` is a GENERATED
        column over, so a double-encoded value here is not cosmetic -- it is INDEXED, and the file
        it belongs to becomes unfindable by its own artist's name. The file side of the match is
        repaired upstream (``derive_query`` / ``original_filename_repaired``); this is the other
        side, which is an external source that can carry its own mis-decode. The repair is
        idempotent, so the clean rows every other test in this module uses are unaffected.
        """
        rows = load_search("time-warp-2024")
        broken = replace(next(r for r in rows if r.external_id == ANCHOR_ID), artist="Sven VÃ¤th", event="Time WarpÃ©")
        search = FakeSearch(rows=[broken])
        renderer = FakeRenderer(html=load_render(ANCHOR_ID))

        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=renderer)

        assert attempt.outcome is LookupOutcome.FOUND
        assert attempt.artist == "Sven Väth"
        assert attempt.event == "Time Warpé"

    async def test_a_row_with_no_artist_or_event_text_survives_the_repair(self) -> None:
        """The repair must not turn a legitimately absent value into an empty string.

        ``TracklistSearchResult.artist``/``event`` are optional precisely because a real results
        page mixes set rows with promo/aftermovie rows that have neither. ``None`` and ``""`` mean
        different things to ``_append_version``'s never-null-out-a-good-value rule.
        """
        rows = load_search("time-warp-2024")
        anchor = next(r for r in rows if r.external_id == ANCHOR_ID)
        blanked = replace(anchor, event=None)
        search = FakeSearch(rows=[blanked])
        renderer = FakeRenderer(html=load_render(ANCHOR_ID))

        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=renderer)
        assert attempt.event is None

    async def test_the_short_listing_parses_too(self) -> None:
        """Two page shapes, so the parser is not proven only against the anchor."""
        rows = load_search("sven-vath-time-warp-2024")
        bbc = next(r for r in rows if r.external_id == "19h6nw7t")
        search = FakeSearch(rows=[bbc])
        renderer = FakeRenderer(html=load_render("19h6nw7t"))
        filename = "Sven_Vath-BBC_Radio_1_Dance_Presents_Time_Warp-2024-10-12-WEB-MP3-GRVMSTR.mp3"

        attempt = await perform_lookup(candidate_for(filename), search=search, renderer=renderer)
        assert attempt.outcome is LookupOutcome.FOUND
        assert len(attempt.tracks) == 12


class TestPerformLookupHonesty:
    """Which outcomes may suppress a future request, and which may never.

    This is the sharpest trap in the molecule: three of the four "no tracklist" shapes are
    statements about US, not about 1001Tracklists, and caching any of them as a negative deletes a
    set from the queue for the negative TTL because of a flaky browser.
    """

    async def test_a_genuinely_absent_set_is_a_definitive_negative(self) -> None:
        """The `no-such-set` capture returns 30 rows anyway -- there is no empty-page signal."""
        search = FakeSearch("no-such-set")
        renderer = FakeRenderer()
        attempt = await perform_lookup(candidate_for(NO_MATCH_FILENAME), search=search, renderer=renderer)

        assert attempt.outcome is LookupOutcome.NOT_FOUND
        assert attempt.outcome.is_definitive_negative
        assert renderer.urls == [], "nothing may be rendered when no candidate clears the bar"

    async def test_an_ambiguous_search_is_transient_not_a_negative(self) -> None:
        """Two indistinguishable candidates mean we could not ASK properly, not that it is absent."""
        rows = load_search("sven-vath-time-warp")
        search = FakeSearch(rows=rows)
        # A filename with no resolvable date: the recurring-festival shape where several editions
        # score alike and the margin gate refuses to pick.
        attempt = await perform_lookup(candidate_for("Sven_Vath-Live_At_Time_Warp-WEB-FLAC-GRVMSTR.mp3"), search=search, renderer=FakeRenderer())

        assert attempt.outcome.is_transient
        assert attempt.outcome is not LookupOutcome.NOT_FOUND

    @pytest.mark.parametrize(
        ("render_outcome", "expected"),
        [
            (RenderOutcome.NO_TRACKLIST, LookupOutcome.NOT_FOUND),
            (RenderOutcome.INTERSTITIAL_PERSISTED, LookupOutcome.BLOCKED),
            (RenderOutcome.TIMEOUT, LookupOutcome.RENDER_FAILED),
            (RenderOutcome.NAVIGATION_FAILED, LookupOutcome.RENDER_FAILED),
        ],
    )
    async def test_render_outcomes_keep_blocked_out_of_not_found(self, render_outcome: RenderOutcome, expected: LookupOutcome) -> None:
        """Only a page that RENDERED and has no track list is a real "1001TL does not have this"."""
        search = FakeSearch("time-warp-2024")
        renderer = FakeRenderer(outcome=render_outcome)
        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=renderer)

        assert attempt.outcome is expected
        assert attempt.external_id == ANCHOR_ID, "the id we were about to render is worth recording even on failure"
        assert attempt.tracks == ()

    async def test_a_partial_parse_is_a_stale_selector_not_a_short_tracklist(self) -> None:
        """`parse_tracklist_tracks` raises rather than returning a truncated list; we record PARSE_FAILED."""
        html = load_render(ANCHOR_ID).replace("trackValue", "trackValueMOVED").replace("trackFormat", "trackFormatMOVED")
        search = FakeSearch("time-warp-2024")
        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=FakeRenderer(html=html))

        assert attempt.outcome is LookupOutcome.PARSE_FAILED
        assert attempt.outcome.is_transient

    async def test_a_page_with_no_track_containers_at_all_is_also_parse_failed(self) -> None:
        """Zero containers after an OK render is our selectors drifting -- phaze-2akf's exact shape.

        Deliberately NOT a negative. Caching it as one would erase sets from the queue in
        proportion to how broken the parser is, and the breakage would be invisible because the
        queue would simply get shorter.
        """
        search = FakeSearch("time-warp-2024")
        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=FakeRenderer(html="<html><body>nothing</body></html>"))

        assert attempt.outcome is LookupOutcome.PARSE_FAILED
        assert attempt.detail is not None and "zero track rows" in attempt.detail

    @pytest.mark.parametrize(
        "error",
        [SearchParseFailureError(30), DisallowedScrapeHostError("https://evil.test/tracklist/x"), RuntimeError("unexpected")],
    )
    async def test_a_raising_search_records_a_transient_rather_than_escaping(self, error: Exception) -> None:
        """An escaping exception loses the record of a request that has ALREADY been spent."""
        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=FakeSearch(raises=error), renderer=FakeRenderer())

        assert attempt.outcome is LookupOutcome.SEARCH_FAILED
        assert attempt.outcome.is_transient
        assert attempt.host_requests == 1

    async def test_a_raising_renderer_records_a_transient_rather_than_escaping(self) -> None:
        search = FakeSearch("time-warp-2024")
        renderer = FakeRenderer(raises=DisallowedScrapeHostError("https://evil.test/tracklist/x"))
        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=renderer)

        assert attempt.outcome is LookupOutcome.RENDER_FAILED
        assert attempt.outcome.is_transient
        assert attempt.host_requests == 2

    async def test_a_no_signal_query_is_refused_without_ever_spending_the_search(self) -> None:
        """phaze-97uw8: a bare-date/noise-only filename never reaches the host at all.

        `select_result` refuses any derived query with neither an artist nor an event before it
        looks at results -- a pure function of `derived`, which `perform_lookup` already holds
        before it would issue the search. Checking it first means the predetermined SEARCH_FAILED
        never costs a host request against the shared crawl-delay budget.
        """
        search = FakeSearch("time-warp-2024")  # would answer if asked -- asserting it never IS
        attempt = await perform_lookup(candidate_for("2024-10-25.mp3"), search=search, renderer=FakeRenderer())

        assert attempt.outcome is LookupOutcome.SEARCH_FAILED
        assert attempt.outcome.is_transient
        assert attempt.host_requests == 0
        assert search.queries == [], "no-signal queries must never spend a host request"

    async def test_no_non_found_path_ever_claims_more_than_it_knows(self) -> None:
        """Sweep: every outcome this module can produce is either FOUND, a definitive negative, or transient."""
        producible = {
            LookupOutcome.FOUND,
            LookupOutcome.NOT_FOUND,
            LookupOutcome.SEARCH_FAILED,
            LookupOutcome.RENDER_FAILED,
            LookupOutcome.BLOCKED,
            LookupOutcome.PARSE_FAILED,
        }
        assert producible == set(LookupOutcome)
        assert producible - {LookupOutcome.FOUND, LookupOutcome.NOT_FOUND} == TRANSIENT_OUTCOMES


# --------------------------------------------------------------------------------------------
# Priority
# --------------------------------------------------------------------------------------------


class TestPriority:
    """The bead's ordering -- operator-flagged / recent / high-value first -- made total and stable."""

    @staticmethod
    def _candidate(*, files: int = 1, flagged: bool = False, added_at: datetime | None = None, decision: CacheDecision = CacheDecision.MISS) -> Any:
        base = candidate_for(
            f"Artist{files}{flagged}{added_at} - Live @ Event 2024-04-12.mp3", files=[(uuid.uuid4(), new_sha()) for _ in range(files)]
        )
        queued = QueuedCandidate(unique_set=base.unique_set, verdict=CacheVerdict(set_key=base.set_key, decision=decision))
        return DrainCandidate(queued=queued, derived=base.derived, added_at=added_at, flagged=flagged)

    def test_flagged_outranks_everything(self) -> None:
        flagged = self._candidate(flagged=True)
        wide = self._candidate(files=8)
        assert sorted([wide, flagged], key=lambda c: c.priority)[0] is flagged

    def test_never_asked_outranks_a_re_check(self) -> None:
        """An expired negative is a re-ask of a question already paid for; new ground wins."""
        fresh = self._candidate()
        recheck = self._candidate(decision=CacheDecision.NEGATIVE_EXPIRED)
        assert sorted([recheck, fresh], key=lambda c: c.priority)[0] is fresh

    def test_more_files_served_outranks_fewer(self) -> None:
        wide = self._candidate(files=4)
        narrow = self._candidate(files=1)
        assert sorted([narrow, wide], key=lambda c: c.priority)[0] is wide

    def test_recently_added_outranks_older_and_unknown_sorts_last(self) -> None:
        new = self._candidate(added_at=NOW)
        old = self._candidate(added_at=NOW - timedelta(days=400))
        unknown = self._candidate(added_at=None)
        ordered = sorted([unknown, old, new], key=lambda c: c.priority)
        assert ordered[0] is new
        assert ordered[-1] is unknown, "a set with no known insertion time must never jump the queue"

    def test_the_order_is_total_so_a_restart_resumes_the_same_sequence(self) -> None:
        candidates = [self._candidate(files=2) for _ in range(5)]
        shuffled = sorted(candidates, key=lambda c: c.set_key[::-1])
        first = [c.set_key for c in sorted(candidates, key=lambda c: c.priority)]
        second = [c.set_key for c in sorted(shuffled, key=lambda c: c.priority)]
        assert first == second


# --------------------------------------------------------------------------------------------
# Persistence and propagation (real Postgres)
# --------------------------------------------------------------------------------------------


def session_factory_for(session: AsyncSession) -> Callable[[], Any]:
    """A drain-shaped session factory that hands back the test's savepoint-nested session.

    The drain deliberately opens and closes MANY short sessions (never one held across network
    I/O), and the test harness deliberately runs everything inside one rolled-back outer
    transaction. This adapter reconciles the two without weakening either: `commit()` inside the
    drain releases a savepoint, so its writes are visible to the assertions and still discarded at
    teardown.
    """

    @contextlib.asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        yield session

    return _factory


async def _tracks_for(session: AsyncSession, tracklist: Tracklist) -> int:
    return (
        await session.execute(select(func.count()).select_from(TracklistTrack).where(TracklistTrack.version_id == tracklist.latest_version_id))
    ).scalar_one()


class TestPersistence:
    async def test_a_found_lookup_writes_one_tracklist_with_its_tracks(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename=ANCHOR_FILENAME)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(file.id, file.sha256_hash)])
        search, renderer = anchor_lookup()
        attempt = await perform_lookup(candidate, search=search, renderer=renderer)

        result = await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()

        stored = (await session.execute(select(Tracklist).where(Tracklist.external_id == ANCHOR_ID))).scalars().all()
        assert len(stored) == 1
        assert stored[0].file_id == file.id
        assert stored[0].propagated_from_set_key is None, "a directly-scraped row is never marked propagated"
        assert await _tracks_for(session, stored[0]) == ANCHOR_TRACKS
        assert result.tracks_written == ANCHOR_TRACKS

    async def test_every_outcome_reaches_the_cache_including_the_failures(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """An outcome that is not recorded is a request that gets spent again on the next pass."""
        file = await make_file(original_filename=ANCHOR_FILENAME)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(file.id, file.sha256_hash)])
        attempt = await perform_lookup(
            candidate, search=FakeSearch("time-warp-2024"), renderer=FakeRenderer(outcome=RenderOutcome.INTERSTITIAL_PERSISTED)
        )

        await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()

        verdict = await lookup(session, candidate.set_key, now=NOW)
        assert verdict.entry is not None
        assert verdict.entry.outcome == LookupOutcome.BLOCKED.value
        assert verdict.decision is CacheDecision.BACKOFF, "a block earns a backoff, never the negative TTL"
        assert (await session.execute(select(func.count()).select_from(Tracklist))).scalar_one() == 0

    async def test_a_definitive_negative_is_cached_and_suppresses_the_next_pass(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename=NO_MATCH_FILENAME)
        candidate = candidate_for(NO_MATCH_FILENAME, files=[(file.id, file.sha256_hash)])
        attempt = await perform_lookup(candidate, search=FakeSearch("no-such-set"), renderer=FakeRenderer())

        await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()

        assert (await lookup(session, candidate.set_key, now=NOW)).decision is CacheDecision.SUPPRESSED_NEGATIVE

    async def test_replaying_the_same_attempt_adds_a_version_not_a_second_row(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """A crash between the write and the commit must be harmless to replay."""
        file = await make_file(original_filename=ANCHOR_FILENAME)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(file.id, file.sha256_hash)])
        search, renderer = anchor_lookup()
        attempt = await perform_lookup(candidate, search=search, renderer=renderer)

        await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()
        await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()

        rows = (await session.execute(select(Tracklist).where(Tracklist.external_id == ANCHOR_ID))).scalars().all()
        assert len(rows) == 1
        versions = (await session.execute(select(TracklistVersion).where(TracklistVersion.tracklist_id == rows[0].id))).scalars().all()
        assert sorted(v.version_number for v in versions) == [1, 2]
        assert await _tracks_for(session, rows[0]) == ANCHOR_TRACKS

    async def test_a_page_owned_by_another_file_is_never_stolen(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """phaze-4a5w: the existing link may be one a human accepted. Serve this set by projection.

        Reachable because the legacy ``search_tracklist`` path can already have scraped and linked
        this page for a file outside the queued set -- that file is already tracklisted, so it is
        not in the set, but it owns the canonical row.
        """
        owner = await make_file(original_filename="Someone else - other.mp3")
        session.add(Tracklist(id=uuid.uuid4(), external_id=ANCHOR_ID, source_url="https://x.test", file_id=owner.id, status="approved"))
        await session.flush()

        file = await make_file(original_filename=ANCHOR_FILENAME)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(file.id, file.sha256_hash)])
        search, renderer = anchor_lookup()
        attempt = await perform_lookup(candidate, search=search, renderer=renderer)

        await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()

        rows = {r.file_id: r for r in (await session.execute(select(Tracklist).where(Tracklist.external_id == ANCHOR_ID))).scalars().all()}
        assert rows[owner.id].propagated_from_set_key is None, "the existing owner keeps the canonical row"
        assert rows[file.id].propagated_from_set_key == candidate.set_key, "our file is served by a marked projection"


class TestPropagation:
    @staticmethod
    async def _two_files(make_file: Any, *, identical_bytes: bool) -> tuple[Any, Any]:
        """Two copies of one set: byte-identical (EXACT) or merely same-query-and-duration."""
        shared = uuid.uuid4().hex + uuid.uuid4().hex
        first = await make_file(original_filename=ANCHOR_FILENAME, sha256=shared)
        second = await make_file(
            original_filename="Sven_Vath-Live_At_Time_Warp_Mannheim-2024-10-25-WEB-MP3-OTHERCRW.mp3",
            sha256=shared if identical_bytes else (uuid.uuid4().hex + uuid.uuid4().hex),
        )
        return first, second

    async def test_one_lookup_serves_every_exact_duplicate(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """The acceptance criterion: one tracklist written, propagated, ZERO extra requests."""
        first, second = await self._two_files(make_file, identical_bytes=True)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(first.id, first.sha256_hash), (second.id, second.sha256_hash)])
        assert candidate.unique_set.file_count == 2

        search, renderer = anchor_lookup()
        attempt = await perform_lookup(candidate, search=search, renderer=renderer)
        result = await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()

        assert search.queries == [candidate.derived.query], "exactly one search for the whole set"
        assert len(renderer.urls) == 1, "exactly one render for the whole set"
        assert result.propagated_files == 1

        rows = {r.file_id: r for r in (await session.execute(select(Tracklist).where(Tracklist.external_id == ANCHOR_ID))).scalars().all()}
        assert set(rows) == {first.id, second.id}
        # Which of the two the grouper picks as canonical is its business (a deterministic choice
        # over the cluster, not a property this test should pin); the invariant is that exactly one
        # row is the scrape and the other is a marked projection of it.
        duplicate = next(file_id for file_id in rows if file_id != candidate.unique_set.canonical_file_id)
        assert rows[candidate.unique_set.canonical_file_id].propagated_from_set_key is None
        propagated = rows[duplicate]
        assert propagated.propagated_from_set_key == candidate.set_key
        assert propagated.propagation_confidence == DuplicateConfidence.EXACT.value
        assert await _tracks_for(session, propagated) == ANCHOR_TRACKS, "a projection is a complete tracklist, not a stub"

    async def test_a_heuristic_duplicate_is_withheld_and_counted(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """The false-merge guard: below EXACT, the duplicate gets nothing and the refusal is counted.

        Two different nights of one residency are exactly this shape, and a wrong tracklist written
        here would also mark the file tracklisted -- so it would never be revisited.
        """
        first, second = await self._two_files(make_file, identical_bytes=False)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(first.id, first.sha256_hash), (second.id, second.sha256_hash)])
        canonical_id = candidate.unique_set.canonical_file_id
        duplicate_id = next(m.file_id for m in candidate.unique_set.members if m.file_id != canonical_id)
        assert next(m for m in candidate.unique_set.members if m.file_id == duplicate_id).confidence is not DuplicateConfidence.EXACT

        search, renderer = anchor_lookup()
        attempt = await perform_lookup(candidate, search=search, renderer=renderer)
        result = await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()

        assert result.propagated_files == 0
        assert result.propagation_skipped == 1
        rows = (await session.execute(select(Tracklist).where(Tracklist.external_id == ANCHOR_ID))).scalars().all()
        assert [r.file_id for r in rows] == [canonical_id]

    async def test_the_gate_is_configurable_and_records_the_tier_it_used(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """Loosening the gate must stay auditable from the ROW, not from the config of the day."""
        first, second = await self._two_files(make_file, identical_bytes=False)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(first.id, first.sha256_hash), (second.id, second.sha256_hash)])
        canonical_id = candidate.unique_set.canonical_file_id
        member = next(m for m in candidate.unique_set.members if m.file_id != canonical_id)

        search, renderer = anchor_lookup()
        attempt = await perform_lookup(candidate, search=search, renderer=renderer)
        await persist_lookup(session, candidate, attempt, propagation_min=DuplicateConfidence.LOW, now=NOW)
        await session.flush()

        rows = {r.file_id: r for r in (await session.execute(select(Tracklist).where(Tracklist.external_id == ANCHOR_ID))).scalars().all()}
        assert rows[member.file_id].propagation_confidence == member.confidence.value
        assert member.confidence is not DuplicateConfidence.EXACT, "the point of this test is a tier the default gate refuses"

    def test_the_default_gate_is_the_strictest_tier(self) -> None:
        assert DEFAULT_PROPAGATION_MIN_CONFIDENCE is DuplicateConfidence.EXACT

    async def test_replaying_a_propagation_versions_the_projection_rather_than_duplicating_it(
        self,
        session: AsyncSession,
        make_file,  # type: ignore[no-untyped-def]
    ) -> None:
        """Replay safety has to hold for the PROJECTIONS too, not just the canonical row.

        A crash between the write and the commit replays the whole candidate, propagation included.
        A second row per duplicate file would make the file's tracklist ambiguous to every consumer
        that reads ``Tracklist.file_id``.
        """
        first, second = await self._two_files(make_file, identical_bytes=True)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(first.id, first.sha256_hash), (second.id, second.sha256_hash)])
        search, renderer = anchor_lookup()
        attempt = await perform_lookup(candidate, search=search, renderer=renderer)

        await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()
        second_result = await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()

        assert second_result.propagated_files == 1
        rows = (await session.execute(select(Tracklist).where(Tracklist.external_id == ANCHOR_ID))).scalars().all()
        assert len(rows) == 2, "two files, two rows -- never four"
        duplicate_id = next(file_id for file_id in (first.id, second.id) if file_id != candidate.unique_set.canonical_file_id)
        projection = next(r for r in rows if r.file_id == duplicate_id)
        versions = (await session.execute(select(TracklistVersion).where(TracklistVersion.tracklist_id == projection.id))).scalars().all()
        assert sorted(v.version_number for v in versions) == [1, 2]
        assert await _tracks_for(session, projection) == ANCHOR_TRACKS

    async def test_a_propagated_file_reads_as_tracklisted_to_the_rest_of_phaze(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """The reason propagation writes a real row: otherwise the pipeline re-queries the file.

        ``stage_status.done_clause(Stage.TRACKLIST)`` and ``pipeline.get_untracked_files`` both mean
        "a Tracklist row points at this file". A propagation they cannot see is not a saved request,
        it is a file the very next operator sweep spends a live request on.
        """
        from phaze.enums.stage import Stage
        from phaze.models.file import FileRecord
        from phaze.services.stage_status import done_clause

        first, second = await self._two_files(make_file, identical_bytes=True)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(first.id, first.sha256_hash), (second.id, second.sha256_hash)])
        search, renderer = anchor_lookup()
        await persist_lookup(session, candidate, await perform_lookup(candidate, search=search, renderer=renderer), now=NOW)
        await session.flush()

        done = (
            (await session.execute(select(FileRecord.id).where(FileRecord.id.in_([first.id, second.id]), done_clause(Stage.TRACKLIST))))
            .scalars()
            .all()
        )
        assert set(done) == {first.id, second.id}


# --------------------------------------------------------------------------------------------
# The pass: queue, budget, resumption
# --------------------------------------------------------------------------------------------


class TestDrainPass:
    @staticmethod
    async def _seed(make_file: Any, session: AsyncSession, filename: str, *, duration: float = 3600.0) -> Any:
        file = await make_file(original_filename=filename)
        session.add(FileMetadata(file_id=file.id, duration=duration))
        await session.flush()
        return file

    async def test_the_queue_uses_derived_queries_and_orders_by_priority(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """The phaze-fq9h.2 seam is plugged in: scene noise never reaches the search query."""
        file = await self._seed(make_file, session, ANCHOR_FILENAME)
        queue = await build_drain_queue(session, now=NOW)

        entry = next(c for c in queue.entries if file.id in {m.file_id for m in c.unique_set.members})
        assert entry.derived.query == "Sven Vath At Time Warp Mannheim 2024"
        assert "GRVMSTR" not in entry.derived.query
        assert entry.added_at is not None

    async def test_added_at_is_correct_when_load_added_at_is_chunked(
        self,
        session: AsyncSession,
        make_file,
        monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
    ) -> None:
        """phaze-1x31w: ``_load_added_at`` chunks its ``FileRecord.id.in_(...)`` the same way
        ``lookup_many`` chunks ``set_key.in_(...)`` -- forced here to a chunk size of 1 (several
        files, several SELECTs) to prove the merged result is unaffected by the split."""
        import phaze.services.tracklist_drain as drain_module

        monkeypatch.setattr(drain_module, "IN_CLAUSE_CHUNK_SIZE", 1)

        for index in range(3):
            await self._seed(make_file, session, f"Artist{index} - Live @ Event 2024-04-1{index}.mp3")

        queue = await build_drain_queue(session, now=NOW)

        assert len(queue.entries) == 3
        assert all(entry.added_at is not None for entry in queue.entries)

    async def test_flagged_files_reach_the_front_of_the_queue(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        for index in range(4):
            await self._seed(make_file, session, f"Artist{index} - Live @ Event 2024-04-1{index}.mp3")
        wanted = await self._seed(make_file, session, "Zed - Live @ Late Event 2024-09-09.mp3")

        queue = await build_drain_queue(session, flagged_file_ids=[wanted.id], now=NOW)
        assert queue.entries[0].flagged is True
        assert wanted.id in {m.file_id for m in queue.entries[0].unique_set.members}

    async def test_target_file_ids_restrict_the_slice_to_that_exact_set(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """A per-file action may spend its slice only on the named file's unique set.

        Priority alone is insufficient when another persisted/operator flag exists: both candidates
        share the first sort tier, so a generic ``limit=1`` slice could honestly prioritize work
        while still answering the wrong file. Targeting filters after the normal funnel, preserving
        classification and cache policy while making the negative case safe (no target means no
        unrelated request).
        """
        other = await self._seed(make_file, session, "Alpha - Live @ Early Event 2024-01-01.mp3")
        wanted = await self._seed(make_file, session, "Zed - Live @ Late Event 2024-09-09.mp3")

        queue = await build_drain_queue(
            session,
            flagged_file_ids=[other.id, wanted.id],
            target_file_ids=[wanted.id],
            now=NOW,
        )

        assert len(queue.entries) == 1
        assert wanted.id in {member.file_id for member in queue.entries[0].unique_set.members}
        assert other.id not in {member.file_id for member in queue.entries[0].unique_set.members}

        missing_target = await build_drain_queue(session, target_file_ids=[uuid.uuid4()], now=NOW)
        assert missing_target.entries == ()

    async def test_a_pass_spends_at_most_its_budget(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        for index in range(3):
            await self._seed(make_file, session, f"Artist{index} - Live @ Event 2024-04-1{index}.mp3")
        search = FakeSearch("no-such-set")

        report = await drain_once(session_factory_for(session), search=search, renderer=FakeRenderer(), limit=2, now=NOW)

        assert report.queued == 3
        assert report.attempted == 2
        assert len(search.queries) == 2

    async def test_stopping_and_restarting_re_spends_nothing(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """THE acceptance criterion. No checkpoint file -- the cache IS the resumption state."""
        for index in range(4):
            await self._seed(make_file, session, f"Artist{index} - Live @ Event 2024-04-1{index}.mp3")

        first_search = FakeSearch("no-such-set")
        first = await drain_once(session_factory_for(session), search=first_search, renderer=FakeRenderer(), limit=2, now=NOW)
        assert first.attempted == 2

        second_search = FakeSearch("no-such-set")
        second = await drain_once(session_factory_for(session), search=second_search, renderer=FakeRenderer(), limit=10, now=NOW)

        assert second.queued == 2, "the two already-answered sets are gone from the queue"
        assert second.attempted == 2
        assert set(first_search.queries).isdisjoint(second_search.queries), "no query is ever repeated"

    async def test_a_set_answered_between_the_snapshot_and_the_attempt_is_skipped(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """The pre-spend re-check, exercised by answering a set MID-PASS.

        A pass over tens of thousands of sets holds a snapshot that is hours old by the time it
        reaches its tail; an overlapping restart, or the operator answering a set from the admin
        UI, lands in exactly this window. Without the re-check every such set costs one host
        request to re-learn something already recorded.
        """
        await self._seed(make_file, session, "Artist0 - Live @ Event 2024-04-10.mp3")
        await self._seed(make_file, session, "Artist1 - Live @ Event 2024-04-11.mp3")

        queue = await build_drain_queue(session, now=NOW)
        assert len(queue.entries) == 2
        second = queue.entries[1]

        class AnsweringSearch(FakeSearch):
            """Records a FOUND for the pass's NEXT candidate while the current one is in flight."""

            async def search(self, query: str) -> list[TracklistSearchResult]:
                rows = await super().search(query)
                if len(self.queries) == 1:
                    await record_outcome(
                        session,
                        set_key=second.set_key,
                        query_text=second.derived.query,
                        outcome=LookupOutcome.FOUND,
                        external_id="answered-elsewhere",
                        now=NOW,
                    )
                    await session.flush()
                return rows

        search = AnsweringSearch("no-such-set")
        report = await drain_once(session_factory_for(session), search=search, renderer=FakeRenderer(), limit=10, now=NOW)

        assert report.attempted == 1
        assert report.skipped_cached == 1
        assert second.derived.query not in search.queries

    async def test_should_stop_ends_the_pass_between_candidates(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        for index in range(3):
            await self._seed(make_file, session, f"Artist{index} - Live @ Event 2024-04-1{index}.mp3")
        search = FakeSearch("no-such-set")
        stop_after = 1

        def should_stop() -> bool:
            return len(search.queries) >= stop_after

        report = await drain_once(session_factory_for(session), search=search, renderer=FakeRenderer(), limit=10, should_stop=should_stop, now=NOW)

        assert report.stopped_early is True
        assert report.attempted == 1

    async def test_a_full_pass_reports_what_it_spent_and_what_it_wrote(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        await self._seed(make_file, session, ANCHOR_FILENAME)
        search, renderer = anchor_lookup()

        report = await drain_once(session_factory_for(session), search=search, renderer=renderer, limit=5, now=NOW)

        assert report.found == 1
        assert report.tracks_written == ANCHOR_TRACKS
        assert report.host_requests == 2
        assert report.outcomes == {LookupOutcome.FOUND.value: 1}
        assert report.as_dict()["found"] == 1

    async def test_an_empty_corpus_is_an_ordinary_zero_pass(self, session: AsyncSession) -> None:
        report = await drain_once(session_factory_for(session), search=FakeSearch(), renderer=FakeRenderer(), limit=5, now=NOW)
        assert report.as_dict() == DrainReport().as_dict()


class TestTally:
    """The report's arithmetic, isolated from the network and the database."""

    def test_every_candidate_lands_in_exactly_one_bucket(self) -> None:
        from phaze.services.tracklist_drain import PersistResult, _tally

        report = DrainReport()
        for outcome in (LookupOutcome.FOUND, LookupOutcome.NOT_FOUND, LookupOutcome.BLOCKED, LookupOutcome.PARSE_FAILED):
            _tally(report, LookupAttempt(set_key="k", query_text="q", outcome=outcome, host_requests=2), PersistResult())

        assert (report.found, report.not_found, report.transient) == (1, 1, 2)
        assert report.attempted == report.found + report.not_found + report.transient
        assert report.host_requests == 8
