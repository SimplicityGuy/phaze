"""An unexpected exception from the post-render analysis is a transient PARSE_FAILED, not a drain abort (phaze-r87rh).

HONEST STATUS: this is a defensive backstop for an UNPROVEN trigger. A probe of ``assess_decoy`` and
``parse_tracklist_tracks`` over an empty string, plain text, NUL bytes and HTML nested 500 / 5,000 /
50,000 levels deep raised nothing, so no input is known to reach these handlers. The tests patch the
analysis steps to raise; they prove the handler's behaviour, not that any real page triggers it.

ZERO live requests: search and render are the in-repo fakes serving recorded captures.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select

from phaze.enums.tracklist_candidate import TRANSIENT_MAX_ATTEMPTS, CacheDecision, LookupOutcome
from phaze.models.tracklist import Tracklist, TracklistTrack
from phaze.models.tracklist_lookup_cache import TracklistLookupCache
from phaze.services import tracklist_drain
from phaze.services.tracklist_drain import perform_lookup, persist_lookup
from phaze.services.tracklist_lookup_cache import lookup
from phaze.services.tracklist_scraper import TracklistScraper
from tests.identify.services.test_tracklist_drain import (
    ANCHOR_FILENAME,
    ANCHOR_ID,
    NOW,
    FakeRenderer,
    FakeSearch,
    candidate_for,
    load_render,
    load_search,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


PAGE_MARKER = "PAGE-CONTENT-MARKER-91c3"
"""Stands in for third-party page text an exception message might quote; it must never reach ``detail``."""

ANCHOR_URL = next(r.url for r in load_search("time-warp-2024") if r.external_id == ANCHOR_ID)


def _raiser(exc: BaseException) -> Any:
    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return _raise


def _searched() -> tuple[FakeSearch, FakeRenderer]:
    return FakeSearch("time-warp-2024"), FakeRenderer(html=load_render(ANCHOR_ID))


async def _no_rows(session: AsyncSession) -> None:
    assert (await session.execute(select(func.count()).select_from(Tracklist))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(TracklistTrack))).scalar_one() == 0
    negatives = select(func.count()).select_from(TracklistLookupCache).where(TracklistLookupCache.outcome == LookupOutcome.NOT_FOUND.value)
    assert (await session.execute(negatives)).scalar_one() == 0


class TestUnexpectedExceptionIsParseFailed:
    @pytest.mark.parametrize("exc", [RuntimeError(PAGE_MARKER), ValueError(PAGE_MARKER)])
    async def test_unexpected_exception_in_decoy_check_is_parse_failed(
        self, monkeypatch: pytest.MonkeyPatch, session: AsyncSession, make_file: Any, exc: Exception
    ) -> None:
        file = await make_file(original_filename=ANCHOR_FILENAME)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(file.id, file.sha256_hash)])
        monkeypatch.setattr(tracklist_drain, "assess_decoy", _raiser(exc))

        search, renderer = _searched()
        attempt = await perform_lookup(candidate, search=search, renderer=renderer)

        assert attempt.outcome is LookupOutcome.PARSE_FAILED
        assert attempt.outcome.is_transient
        assert not attempt.is_found
        assert attempt.tracks == ()
        assert attempt.external_id == ANCHOR_ID, "the selected page is still recorded, for diagnosis"

        await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()
        await _no_rows(session)

    @pytest.mark.parametrize("exc", [RecursionError(PAGE_MARKER), RuntimeError(PAGE_MARKER)])
    async def test_unexpected_exception_in_parse_is_parse_failed(
        self, monkeypatch: pytest.MonkeyPatch, session: AsyncSession, make_file: Any, exc: Exception
    ) -> None:
        file = await make_file(original_filename=ANCHOR_FILENAME)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(file.id, file.sha256_hash)])
        monkeypatch.setattr(tracklist_drain, "parse_tracklist_tracks", _raiser(exc))

        search, renderer = _searched()
        attempt = await perform_lookup(candidate, search=search, renderer=renderer)

        assert attempt.outcome is LookupOutcome.PARSE_FAILED
        assert attempt.outcome.is_transient
        assert not attempt.is_found
        assert attempt.tracks == ()

        await persist_lookup(session, candidate, attempt, now=NOW)
        await session.flush()
        await _no_rows(session)

    async def test_hint_path_unexpected_exception_is_parse_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(TracklistScraper, "detail_page_result", _raiser(RuntimeError(PAGE_MARKER)))
        search = FakeSearch(raises=AssertionError("a raised page-scoring step must not fall through to a search"))

        attempt = await perform_lookup(
            candidate_for(ANCHOR_FILENAME),
            search=search,
            renderer=FakeRenderer(html=load_render(ANCHOR_ID)),
            retry_url=ANCHOR_URL,
            retry_confidence=88,
        )

        assert attempt.outcome is LookupOutcome.PARSE_FAILED
        assert attempt.outcome.is_transient
        assert search.queries == [], "the hint is not discarded as a mismatched page"
        assert attempt.source_url == ANCHOR_URL
        assert attempt.result_confidence == 88
        assert attempt.detail is not None
        assert "RuntimeError" in attempt.detail
        assert PAGE_MARKER not in attempt.detail

    async def test_hint_path_unexpected_exception_in_analysis_is_parse_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tracklist_drain, "assess_decoy", _raiser(RuntimeError(PAGE_MARKER)))

        attempt = await perform_lookup(
            candidate_for(ANCHOR_FILENAME),
            search=FakeSearch(raises=AssertionError("no search")),
            renderer=FakeRenderer(html=load_render(ANCHOR_ID)),
            retry_url=ANCHOR_URL,
        )

        assert attempt.outcome is LookupOutcome.PARSE_FAILED
        assert PAGE_MARKER not in (attempt.detail or "")

    @pytest.mark.parametrize("target", ["assess_decoy", "parse_tracklist_tracks"])
    async def test_detail_names_only_the_exception_type(self, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
        monkeypatch.setattr(tracklist_drain, target, _raiser(RuntimeError(PAGE_MARKER)))

        search, renderer = _searched()
        attempt = await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=renderer)

        assert attempt.detail is not None
        assert "RuntimeError" in attempt.detail
        assert PAGE_MARKER not in attempt.detail
        assert ANCHOR_URL not in attempt.detail
        assert ANCHOR_ID not in attempt.detail

    async def test_the_failure_is_logged_with_the_set_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(tracklist_drain.logger, "exception", lambda event, **kw: calls.append((event, kw)))
        monkeypatch.setattr(tracklist_drain, "assess_decoy", _raiser(RuntimeError(PAGE_MARKER)))
        candidate = candidate_for(ANCHOR_FILENAME)

        search, renderer = _searched()
        await perform_lookup(candidate, search=search, renderer=renderer)

        assert [kw.get("set_key") for _event, kw in calls] == [candidate.set_key]


class TestBaseExceptionsStillPropagate:
    @pytest.mark.parametrize("exc", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(1)])
    @pytest.mark.parametrize("target", ["assess_decoy", "parse_tracklist_tracks"])
    async def test_cancellation_still_propagates(self, monkeypatch: pytest.MonkeyPatch, target: str, exc: BaseException) -> None:
        monkeypatch.setattr(tracklist_drain, target, _raiser(exc))

        search, renderer = _searched()
        with pytest.raises(type(exc)):
            await perform_lookup(candidate_for(ANCHOR_FILENAME), search=search, renderer=renderer)

    async def test_cancellation_still_propagates_from_the_hint_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(TracklistScraper, "detail_page_result", _raiser(asyncio.CancelledError()))

        with pytest.raises(asyncio.CancelledError):
            await perform_lookup(
                candidate_for(ANCHOR_FILENAME),
                search=FakeSearch(raises=AssertionError("no search")),
                renderer=FakeRenderer(html=load_render(ANCHOR_ID)),
                retry_url=ANCHOR_URL,
            )


class TestRepeatedUnexpectedExceptionParks:
    async def test_repeated_unexpected_exception_parks_the_set(self, monkeypatch: pytest.MonkeyPatch, session: AsyncSession, make_file: Any) -> None:
        file = await make_file(original_filename=ANCHOR_FILENAME)
        candidate = candidate_for(ANCHOR_FILENAME, files=[(file.id, file.sha256_hash)])
        monkeypatch.setattr(tracklist_drain, "assess_decoy", _raiser(RuntimeError(PAGE_MARKER)))

        moment = NOW
        for attempt_number in range(1, TRANSIENT_MAX_ATTEMPTS + 1):
            search, renderer = _searched()
            attempt = await perform_lookup(candidate, search=search, renderer=renderer)
            assert attempt.outcome is LookupOutcome.PARSE_FAILED
            await persist_lookup(session, candidate, attempt, now=moment)
            await session.flush()

            verdict = await lookup(session, candidate.set_key, now=moment)
            assert verdict.entry is not None
            assert verdict.entry.outcome == LookupOutcome.PARSE_FAILED.value
            assert verdict.entry.attempts == attempt_number
            if attempt_number < TRANSIENT_MAX_ATTEMPTS:
                assert verdict.decision is CacheDecision.BACKOFF
                assert verdict.entry.expires_at is not None
                moment = verdict.entry.expires_at + timedelta(minutes=1)

        parked = await lookup(session, candidate.set_key, now=moment + timedelta(days=30))
        assert parked.decision is CacheDecision.TRANSIENT_EXHAUSTED
        assert not parked.should_query
        await _no_rows(session)
