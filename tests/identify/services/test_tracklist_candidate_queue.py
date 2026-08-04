"""The candidate funnel end to end: corpus -> queue, and the schedule that queue implies.

Two layers, deliberately:

* the PURE builder over fixture signals, where the funnel arithmetic and the ordering live;
* a DB layer over real ``files`` / ``metadata`` / ``tracklists`` / ``file_companions`` rows, which
  is the only way to prove the already-tracklisted exclusions actually match -- a broken EXISTS
  does not raise, it just quietly enqueues work the drain cannot afford.

Filenames are invented (CLAUDE.md forbids committing real archive names).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.enums.tracklist_candidate import CacheDecision, LookupOutcome
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist
from phaze.services.tracklist_candidate_queue import (
    DAILY_LOOKUP_CEILING,
    HOST_CRAWL_DELAY_SECONDS,
    HOST_REQUESTS_PER_DAY,
    MEDIA_FILE_TYPES,
    REQUESTS_PER_LOOKUP,
    build_candidate_queue,
    build_queue_from_signals,
    format_corpus_report,
    load_candidate_signals,
    project_drain_days,
)
from phaze.services.tracklist_candidates import CandidateSignals
from phaze.services.tracklist_lookup_cache import CacheVerdict, record_outcome


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def signals(filename: str, *, duration: float | None = None, **kwargs: object) -> CandidateSignals:
    return CandidateSignals(file_id=uuid.uuid4(), filename=filename, duration_seconds=duration, **kwargs)  # type: ignore[arg-type]


class TestBudgetArithmetic:
    """The numbers the whole molecule's schedule rests on. Pinned so a typo cannot move them."""

    def test_ceiling_is_derived_from_the_crawl_delay_not_hardcoded(self) -> None:
        assert HOST_CRAWL_DELAY_SECONDS == 8.0
        assert HOST_REQUESTS_PER_DAY == 10_800
        assert DAILY_LOOKUP_CEILING == int(10_800 / REQUESTS_PER_LOOKUP) == 4_320

    def test_projection(self) -> None:
        assert project_drain_days(4_320) == pytest.approx(1.0)
        assert project_drain_days(0) == 0.0

    def test_the_naive_pass_the_epic_warns_about(self) -> None:
        """~150,000 candidates with no dedup is the 35+ day figure from the epic's DESIGN."""
        assert project_drain_days(150_000) > 34.0

    def test_media_types_cover_audio_and_video_but_not_companions(self) -> None:
        assert {"mp3", "m4a", "flac", "mp4", "mkv"} <= MEDIA_FILE_TYPES
        assert "cue" not in MEDIA_FILE_TYPES
        assert "nfo" not in MEDIA_FILE_TYPES


class TestFunnel:
    def test_already_tracklisted_files_never_reach_the_queue(self) -> None:
        corpus = [
            signals("A - Live @ E 2024-04-12.mp3", duration=3600.0, has_scraped_tracklist=True),
            signals("B - Live @ E 2024-04-13.mp3", duration=3600.0, has_cue_companion=True),
            signals("C - Live @ E 2024-04-14.mp3", duration=3600.0, has_embedded_tracklist=True),
            signals("D - Live @ E 2024-04-15.mp3", duration=3600.0),
        ]
        queue = build_queue_from_signals(corpus, {})
        assert queue.stats.already_tracklisted == 3
        assert (queue.stats.skipped_scraped, queue.stats.skipped_cue, queue.stats.skipped_embedded) == (1, 1, 1)
        assert queue.stats.queued == 1

    def test_tracks_are_classified_out(self) -> None:
        corpus = [
            signals("A - Live @ E 2024-04-12.mp3", duration=3600.0),
            signals("01. Artist - Title (Original Mix).mp3", duration=360.0),
        ]
        queue = build_queue_from_signals(corpus, {})
        assert queue.stats.classified_live_set == 1
        assert queue.stats.classified_track == 1
        assert queue.stats.queued == 1

    def test_unknown_is_excluded_by_default_and_includable_on_request(self) -> None:
        corpus = [signals("audio.mp3")]
        assert build_queue_from_signals(corpus, {}).stats.queued == 0
        assert build_queue_from_signals(corpus, {}, include_unknown=True).stats.queued == 1
        assert build_queue_from_signals(corpus, {}).stats.classified_unknown == 1

    def test_duplicates_collapse_into_one_queue_entry_carrying_every_file(self) -> None:
        corpus = [
            signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0),
            signals("Artist_-_Live_@_Event_2024-04-12-WEB-FLAC-CREW.flac", duration=3608.0),
        ]
        queue = build_queue_from_signals(corpus, {})
        assert queue.stats.queued == 1
        assert queue.stats.collapse_ratio == pytest.approx(2.0)
        assert queue.entries[0].unique_set.file_count == 2
        assert queue.entries[0].unique_set.requests_saved == 1

    def test_widest_propagation_is_queued_first(self) -> None:
        corpus = [
            signals("Solo - Live @ Other 2024-05-01.mp3", duration=3600.0),
            signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0),
            signals("Artist - Live @ Event 2024-04-12.flac", duration=3605.0),
            signals("Artist - Live @ Event 2024-04-12.m4a", duration=3610.0),
        ]
        queue = build_queue_from_signals(corpus, {})
        assert [e.unique_set.file_count for e in queue.entries] == [3, 1]

    def test_cache_verdicts_move_sets_out_of_the_queue_and_are_counted(self) -> None:
        corpus = [
            signals("A - Live @ E 2024-04-12.mp3", duration=3600.0),
            signals("B - Live @ E 2024-04-13.mp3", duration=3600.0),
            signals("C - Live @ E 2024-04-14.mp3", duration=3600.0),
            signals("D - Live @ E 2024-04-15.mp3", duration=3600.0),
            signals("E - Live @ E 2024-04-16.mp3", duration=3600.0),
        ]
        keys = [u.unique_set.key for u in build_queue_from_signals(corpus, {}).entries]
        verdicts = {
            keys[0]: CacheVerdict(keys[0], CacheDecision.HIT_POSITIVE),
            keys[1]: CacheVerdict(keys[1], CacheDecision.SUPPRESSED_NEGATIVE),
            keys[2]: CacheVerdict(keys[2], CacheDecision.BACKOFF),
            keys[3]: CacheVerdict(keys[3], CacheDecision.TRANSIENT_EXHAUSTED),
        }
        queue = build_queue_from_signals(corpus, verdicts)
        assert queue.stats.queued == 1
        assert (queue.stats.cached_positive, queue.stats.cached_negative) == (1, 1)
        assert (queue.stats.cached_backoff, queue.stats.cached_exhausted) == (1, 1)
        assert len(queue.cached) == 4, "explained, not merely absent -- the admin UI needs the reason"

    def test_retryable_verdicts_stay_in_the_queue(self) -> None:
        corpus = [signals("A - Live @ E 2024-04-12.mp3", duration=3600.0)]
        key = build_queue_from_signals(corpus, {}).entries[0].unique_set.key
        for decision in (CacheDecision.NEGATIVE_EXPIRED, CacheDecision.TRANSIENT_RETRY_READY, CacheDecision.MISS):
            queue = build_queue_from_signals(corpus, {key: CacheVerdict(key, decision)})
            assert queue.stats.queued == 1, decision

    def test_empty_corpus_renders_without_dividing_by_zero(self) -> None:
        queue = build_queue_from_signals([], {})
        assert queue.stats.queued == 0
        assert queue.stats.collapse_ratio == 0.0
        assert queue.stats.projected_days == 0.0
        assert "QUEUED" in format_corpus_report(queue.stats)


class TestReport:
    def test_report_states_the_ceiling_and_both_projections(self) -> None:
        corpus = [
            signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0),
            signals("Artist - Live @ Event 2024-04-12.flac", duration=3605.0),
            signals("01. Artist - Title.mp3", duration=300.0),
        ]
        stats = build_queue_from_signals(corpus, {}).stats
        report = format_corpus_report(stats)
        assert "4,320 lookups/day" in report
        assert "projected drain" in report
        assert "without dedup or cache" in report
        assert stats.candidate_files == 2
        assert stats.days_without_dedup > stats.projected_days


@pytest.mark.asyncio
class TestAgainstTheDatabase:
    async def test_media_only_and_the_three_skip_sources(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        plain = await make_file(original_filename="Artist - Live @ Event 2024-04-12.mp3")
        session.add(FileMetadata(file_id=plain.id, duration=3600.0))

        scraped = await make_file(original_filename="Artist - Live @ Event 2024-04-13.mp3")
        session.add(FileMetadata(file_id=scraped.id, duration=3600.0))
        session.add(
            Tracklist(id=uuid.uuid4(), external_id=f"ext-{uuid.uuid4().hex[:10]}", source_url="https://x.test", file_id=scraped.id, status="approved")
        )

        cued = await make_file(original_filename="Artist - Live @ Event 2024-04-14.mp3")
        session.add(FileMetadata(file_id=cued.id, duration=3600.0))
        cue = await make_file(original_filename="Artist - Live @ Event 2024-04-14.cue", file_type="cue")
        session.add(FileCompanion(companion_id=cue.id, media_id=cued.id))

        embedded = await make_file(original_filename="Artist - Live @ Event 2024-04-15.mp3")
        session.add(FileMetadata(file_id=embedded.id, duration=3600.0, raw_tags={"comment": "00:00 a\n10:00 b\n20:00 c"}))
        await session.flush()

        loaded = {s.file_id: s for s in await load_candidate_signals(session)}
        assert cue.id not in loaded, "a .cue is not a media file and must never be a candidate"
        assert loaded[plain.id].already_tracklisted is False
        assert loaded[scraped.id].has_scraped_tracklist is True
        assert loaded[cued.id].has_cue_companion is True
        assert loaded[embedded.id].has_embedded_tracklist is True

    async def test_a_rejected_tracklist_does_not_suppress_a_fresh_lookup(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """A rejected tracklist is the operator saying "wrong set" -- that file needs a NEW lookup."""
        file = await make_file(original_filename="Artist - Live @ Event 2024-04-12.mp3")
        session.add(FileMetadata(file_id=file.id, duration=3600.0))
        session.add(
            Tracklist(id=uuid.uuid4(), external_id=f"ext-{uuid.uuid4().hex[:10]}", source_url="https://x.test", file_id=file.id, status="rejected")
        )
        await session.flush()

        loaded = {s.file_id: s for s in await load_candidate_signals(session)}
        assert loaded[file.id].has_scraped_tracklist is False

    async def test_metadata_is_optional(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """The outer join must survive a file the metadata stage has not reached yet."""
        file = await make_file(original_filename="Artist - Live @ Event 2024-04-12.mp3")
        await session.flush()
        loaded = {s.file_id: s for s in await load_candidate_signals(session)}
        assert loaded[file.id].duration_seconds is None

    async def test_agent_scoping(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename="Artist - Live @ Event 2024-04-12.mp3")
        await session.flush()
        assert [s.file_id for s in await load_candidate_signals(session, agent_id="test-fileserver")] == [file.id]
        assert await load_candidate_signals(session, agent_id="some-other-agent") == []

    async def test_derived_queries_are_threaded_through(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename="aaaa.mp3")
        await session.flush()
        loaded = await load_candidate_signals(session, derived_queries={file.id: "Artist Event 2024-04-12"})
        assert loaded[0].derived_query == "Artist Event 2024-04-12"

    async def test_rerun_after_a_recorded_outcome_yields_a_shorter_queue(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """The acceptance criterion, stated operationally: the cache prevents a re-query.

        Two distinct sets, one of which the drain has already answered. The second run must offer
        only the untouched one -- and the answered set must still be explained in ``cached``, not
        silently vanish.
        """
        for day, duration in ((12, 3600.0), (13, 5400.0)):
            file = await make_file(original_filename=f"Artist - Live @ Event 2024-04-{day}.mp3")
            session.add(FileMetadata(file_id=file.id, duration=duration))
        await session.flush()

        first = await build_candidate_queue(session, now=NOW)
        assert first.stats.queued == 2

        answered = first.entries[0].unique_set
        await record_outcome(
            session,
            set_key=answered.key,
            query_text=answered.query_text,
            outcome=LookupOutcome.FOUND,
            external_id="1001-abc",
            now=NOW,
        )

        second = await build_candidate_queue(session, now=NOW + timedelta(days=1))
        assert second.stats.queued == 1
        assert second.stats.cached_positive == 1
        assert answered.key not in {e.unique_set.key for e in second.entries}
        assert answered.key in {e.unique_set.key for e in second.cached}

    async def test_a_blocked_set_returns_to_the_queue_after_its_backoff(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """The end-to-end version of the honesty rule: a Turnstile block is not a negative."""
        file = await make_file(original_filename="Artist - Live @ Event 2024-04-12.mp3")
        session.add(FileMetadata(file_id=file.id, duration=3600.0))
        await session.flush()

        first = await build_candidate_queue(session, now=NOW)
        target = first.entries[0].unique_set
        await record_outcome(session, set_key=target.key, query_text=target.query_text, outcome=LookupOutcome.BLOCKED, now=NOW)

        during = await build_candidate_queue(session, now=NOW + timedelta(minutes=5))
        assert during.stats.queued == 0
        assert during.stats.cached_backoff == 1

        after = await build_candidate_queue(session, now=NOW + timedelta(hours=6))
        assert after.stats.queued == 1, "a blocked set must come back; only a real not_found stays out"

    async def test_queue_defaults_to_wall_clock(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename="Artist - Live @ Event 2024-04-12.mp3")
        session.add(FileMetadata(file_id=file.id, duration=3600.0))
        await session.flush()
        assert (await build_candidate_queue(session)).stats.queued == 1
