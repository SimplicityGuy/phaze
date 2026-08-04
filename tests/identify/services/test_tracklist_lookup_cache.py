"""The persisted 1001TL lookup cache: positives, negatives, and the honesty of the distinction.

The tests that matter most here are the NEGATIVE ones -- specifically, the ones asserting that a
Turnstile block or a render crash is NOT remembered as "1001TL does not have this set". That
confusion would remove real sets from a months-long, rate-capped drain with no error and no way to
notice, so it is pinned from several directions rather than once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from phaze.enums.tracklist_candidate import TRANSIENT_MAX_ATTEMPTS, CacheDecision, LookupOutcome
from phaze.models.tracklist_lookup_cache import TracklistLookupCache
from phaze.services.tracklist_lookup_cache import (
    NEGATIVE_TTL_DAYS,
    TRANSIENT_BACKOFF_BASE_MINUTES,
    TRANSIENT_BACKOFF_MAX_HOURS,
    _backoff_delay,
    compute_expires_at,
    lookup,
    lookup_many,
    purge_expired_negatives,
    record_outcome,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


class TestOutcomeTaxonomy:
    """The enum-level guarantees the storage layer relies on."""

    def test_only_not_found_is_a_definitive_negative(self) -> None:
        definitive = [o for o in LookupOutcome if o.is_definitive_negative]
        assert definitive == [LookupOutcome.NOT_FOUND]

    def test_every_non_found_outcome_is_either_definitive_or_transient(self) -> None:
        """No outcome may fall between the two -- an unclassified one would get silent treatment."""
        for outcome in LookupOutcome:
            if outcome is LookupOutcome.FOUND:
                continue
            assert outcome.is_definitive_negative != outcome.is_transient, outcome

    def test_queryable_decisions(self) -> None:
        assert CacheDecision.MISS.should_query
        assert CacheDecision.NEGATIVE_EXPIRED.should_query
        assert CacheDecision.TRANSIENT_RETRY_READY.should_query
        assert not CacheDecision.HIT_POSITIVE.should_query
        assert not CacheDecision.SUPPRESSED_NEGATIVE.should_query
        assert not CacheDecision.BACKOFF.should_query
        assert not CacheDecision.TRANSIENT_EXHAUSTED.should_query


class TestExpiryPolicy:
    def test_positive_never_expires(self) -> None:
        assert compute_expires_at(LookupOutcome.FOUND, 1, NOW) is None

    def test_negative_gets_the_ttl(self) -> None:
        assert compute_expires_at(LookupOutcome.NOT_FOUND, 1, NOW) == NOW + timedelta(days=NEGATIVE_TTL_DAYS)

    def test_transient_backoff_doubles_and_is_capped(self) -> None:
        assert _backoff_delay(1) == timedelta(minutes=TRANSIENT_BACKOFF_BASE_MINUTES)
        assert _backoff_delay(2) == timedelta(minutes=TRANSIENT_BACKOFF_BASE_MINUTES * 2)
        assert _backoff_delay(99) == timedelta(hours=TRANSIENT_BACKOFF_MAX_HOURS)

    def test_transient_backoff_is_hours_not_months(self) -> None:
        """A blocked set must come back the same day, not after the negative TTL."""
        assert compute_expires_at(LookupOutcome.BLOCKED, 1, NOW) < NOW + timedelta(days=1)


@pytest.mark.asyncio
class TestRecordAndLookup:
    async def test_miss_on_an_unknown_key(self, session: AsyncSession) -> None:
        verdict = await lookup(session, "never-seen", now=NOW)
        assert verdict.decision is CacheDecision.MISS
        assert verdict.should_query

    async def test_positive_is_a_free_answer_forever(self, session: AsyncSession) -> None:
        await record_outcome(
            session,
            set_key="k-positive",
            query_text="artist event 2024-04-12",
            outcome=LookupOutcome.FOUND,
            external_id="1001-abc",
            source_url="https://www.1001tracklists.com/tracklist/abc",
            result_confidence=95,
            now=NOW,
        )
        verdict = await lookup(session, "k-positive", now=NOW + timedelta(days=3650))
        assert verdict.decision is CacheDecision.HIT_POSITIVE
        assert not verdict.should_query
        assert verdict.external_id == "1001-abc"

    async def test_negative_is_suppressed_then_re_queried_after_the_ttl(self, session: AsyncSession) -> None:
        await record_outcome(session, set_key="k-negative", query_text="q", outcome=LookupOutcome.NOT_FOUND, now=NOW)

        inside = await lookup(session, "k-negative", now=NOW + timedelta(days=NEGATIVE_TTL_DAYS - 1))
        assert inside.decision is CacheDecision.SUPPRESSED_NEGATIVE
        assert not inside.should_query

        after = await lookup(session, "k-negative", now=NOW + timedelta(days=NEGATIVE_TTL_DAYS + 1))
        assert after.decision is CacheDecision.NEGATIVE_EXPIRED
        assert after.should_query, "1001TL gains tracklists over time; a negative is not a tombstone"

    @pytest.mark.parametrize("outcome", sorted(LookupOutcome, key=str))
    async def test_no_outcome_but_not_found_ever_suppresses_as_a_negative(self, session: AsyncSession, outcome: LookupOutcome) -> None:
        """THE honesty invariant, swept over every outcome including ones added later."""
        await record_outcome(session, set_key=f"k-{outcome.value}", query_text="q", outcome=outcome, now=NOW)
        verdict = await lookup(session, f"k-{outcome.value}", now=NOW + timedelta(days=NEGATIVE_TTL_DAYS - 1))
        if outcome is LookupOutcome.NOT_FOUND:
            assert verdict.decision is CacheDecision.SUPPRESSED_NEGATIVE
        else:
            assert verdict.decision is not CacheDecision.SUPPRESSED_NEGATIVE

    async def test_a_persisted_turnstile_block_comes_back_after_its_backoff(self, session: AsyncSession) -> None:
        """A block is a statement about US. It must not cost the set its place in the queue."""
        await record_outcome(session, set_key="k-blocked", query_text="q", outcome=LookupOutcome.BLOCKED, detail="turnstile persisted", now=NOW)

        during = await lookup(session, "k-blocked", now=NOW + timedelta(minutes=1))
        assert during.decision is CacheDecision.BACKOFF
        assert not during.should_query

        later = await lookup(session, "k-blocked", now=NOW + timedelta(hours=2))
        assert later.decision is CacheDecision.TRANSIENT_RETRY_READY
        assert later.should_query

    async def test_repeated_transients_park_rather_than_becoming_a_negative(self, session: AsyncSession) -> None:
        for attempt in range(TRANSIENT_MAX_ATTEMPTS):
            await record_outcome(
                session, set_key="k-flaky", query_text="q", outcome=LookupOutcome.RENDER_FAILED, now=NOW + timedelta(hours=attempt * 30)
            )
        verdict = await lookup(session, "k-flaky", now=NOW + timedelta(days=30))
        assert verdict.decision is CacheDecision.TRANSIENT_EXHAUSTED
        assert not verdict.should_query
        assert verdict.entry is not None
        assert verdict.entry.attempts == TRANSIENT_MAX_ATTEMPTS
        assert verdict.entry.outcome == LookupOutcome.RENDER_FAILED.value, "parked, NOT rewritten as not_found"

    async def test_attempts_increment_and_backoff_grows_with_them(self, session: AsyncSession) -> None:
        first = await record_outcome(session, set_key="k-grow", query_text="q", outcome=LookupOutcome.BLOCKED, now=NOW)
        assert first.attempts == 1
        assert first.expires_at == NOW + _backoff_delay(1)

        second = await record_outcome(session, set_key="k-grow", query_text="q", outcome=LookupOutcome.BLOCKED, now=NOW)
        assert second.attempts == 2
        assert second.expires_at == NOW + _backoff_delay(2), "SQL backoff must match the Python policy"

    async def test_backoff_sql_matches_python_policy_across_attempts(self, session: AsyncSession) -> None:
        """The SQL branch computes ``base * 2**stored``; the Python branch ``base * 2**(n-1)``.

        They are the same number only because the stored count is pre-increment. This walks the
        whole ladder, including the 24h cap, so a change to either side that breaks the identity
        fails here instead of silently changing how fast a flaky set returns.
        """
        for attempt in range(1, TRANSIENT_MAX_ATTEMPTS + 1):
            entry = await record_outcome(session, set_key="k-ladder", query_text="q", outcome=LookupOutcome.SEARCH_FAILED, now=NOW)
            assert entry.attempts == attempt
            assert entry.expires_at == compute_expires_at(LookupOutcome.SEARCH_FAILED, attempt, NOW)

    async def test_a_later_positive_overwrites_an_earlier_negative(self, session: AsyncSession) -> None:
        await record_outcome(session, set_key="k-flip", query_text="q", outcome=LookupOutcome.NOT_FOUND, now=NOW)
        await record_outcome(session, set_key="k-flip", query_text="q", outcome=LookupOutcome.FOUND, external_id="1001-xyz", now=NOW)
        verdict = await lookup(session, "k-flip", now=NOW)
        assert verdict.decision is CacheDecision.HIT_POSITIVE
        assert verdict.external_id == "1001-xyz"
        assert verdict.entry is not None
        assert verdict.entry.expires_at is None

    async def test_external_id_is_only_exposed_on_a_positive(self, session: AsyncSession) -> None:
        await record_outcome(session, set_key="k-neg2", query_text="q", outcome=LookupOutcome.NOT_FOUND, now=NOW)
        assert (await lookup(session, "k-neg2", now=NOW)).external_id is None

    async def test_unknown_stored_outcome_falls_back_to_querying(self, session: AsyncSession) -> None:
        """A row from a newer version must never be read as "skip this set"."""
        session.add(
            TracklistLookupCache(
                set_key="k-future",
                query_text="q",
                outcome="invented_in_2027",
                attempts=1,
                first_attempted_at=NOW,
                last_attempted_at=NOW,
            )
        )
        await session.flush()
        verdict = await lookup(session, "k-future", now=NOW)
        assert verdict.decision is CacheDecision.MISS
        assert verdict.should_query

    async def test_naive_expires_at_is_treated_as_utc_not_a_crash(self, session: AsyncSession) -> None:
        session.add(
            TracklistLookupCache(
                set_key="k-naive",
                query_text="q",
                outcome=LookupOutcome.NOT_FOUND.value,
                attempts=1,
                first_attempted_at=NOW,
                last_attempted_at=NOW,
                expires_at=datetime(2026, 9, 1, 12, 0),  # deliberately NAIVE -- the point of the test
            )
        )
        await session.flush()
        assert (await lookup(session, "k-naive", now=NOW)).decision is CacheDecision.SUPPRESSED_NEGATIVE

    async def test_lookup_many_is_one_round_trip_and_defaults_absent_keys_to_miss(self, session: AsyncSession) -> None:
        await record_outcome(session, set_key="k-a", query_text="q", outcome=LookupOutcome.FOUND, external_id="x", now=NOW)
        verdicts = await lookup_many(session, ["k-a", "k-b"], now=NOW)
        assert verdicts["k-a"].decision is CacheDecision.HIT_POSITIVE
        assert verdicts["k-b"].decision is CacheDecision.MISS

    async def test_lookup_many_with_no_keys_touches_nothing(self, session: AsyncSession) -> None:
        assert await lookup_many(session, [], now=NOW) == {}

    async def test_lookup_defaults_to_wall_clock(self, session: AsyncSession) -> None:
        await record_outcome(session, set_key="k-clock", query_text="q", outcome=LookupOutcome.FOUND, external_id="x")
        assert (await lookup(session, "k-clock")).decision is CacheDecision.HIT_POSITIVE


@pytest.mark.asyncio
class TestPurge:
    async def test_only_expired_negatives_are_purged(self, session: AsyncSession) -> None:
        await record_outcome(session, set_key="p-expired", query_text="q", outcome=LookupOutcome.NOT_FOUND, now=NOW - timedelta(days=400))
        await record_outcome(session, set_key="p-fresh", query_text="q", outcome=LookupOutcome.NOT_FOUND, now=NOW)
        await record_outcome(session, set_key="p-positive", query_text="q", outcome=LookupOutcome.FOUND, external_id="x", now=NOW)
        await record_outcome(session, set_key="p-blocked", query_text="q", outcome=LookupOutcome.BLOCKED, now=NOW - timedelta(days=400))

        removed = await purge_expired_negatives(session, now=NOW)
        assert removed == 1

        remaining = set((await session.execute(select(TracklistLookupCache.set_key))).scalars())
        assert remaining == {"p-fresh", "p-positive", "p-blocked"}, "a parked transient's attempt count must survive"

    async def test_purge_defaults_to_wall_clock_and_returns_zero_on_an_empty_table(self, session: AsyncSession) -> None:
        assert await purge_expired_negatives(session) == 0
