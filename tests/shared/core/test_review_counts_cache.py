"""Review poll parity and failure semantics on the same data as the stage readers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers.pipeline.dashboard_stats import _build_dag_context
from phaze.services.dedup import count_duplicate_groups
from phaze.services.junk_review_page import count_open_groups
from phaze.services.pipeline import review_counts
from phaze.services.proposal_queries import get_proposal_stats
from phaze.services.review import count_cue_review_candidates
from tests.review.routers.test_cue import _create_approved_tracklist_with_file


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_counts, "_review_cache", {"reviewPending": 0, "reviewDedupe": 0, "reviewJunk": 0, "reviewCue": 0})
    monkeypatch.setattr(review_counts, "_review_cache_measured_at", 0.0)
    monkeypatch.setattr(review_counts, "_review_cache_available", False)


@pytest.mark.asyncio
async def test_cached_dag_review_counts_equal_stage_pages(session: AsyncSession) -> None:
    _, original = await _create_approved_tracklist_with_file(session, artist="Test Artist", event="Test Event")
    duplicate = FileRecord(
        id=uuid.uuid4(),
        agent_id=original.agent_id,
        sha256_hash=original.sha256_hash,
        original_path="/synthetic/duplicate.mp3",
        original_filename="duplicate.mp3",
        current_path="/synthetic/duplicate.mp3",
        file_type="mp3",
        file_size=original.file_size,
    )
    session.add(duplicate)
    await session.flush()
    session.add(RenameProposal(file_id=duplicate.id, proposed_filename="renamed.mp3", status=ProposalStatus.PENDING))
    session.add(
        CompanionJunkReview(
            agent_id=original.agent_id,
            original_path="/synthetic/info.nfo",
            sha256_hash="a" * 64,
            file_type="nfo",
            file_size=120,
            reason="known_stamp",
            content_group="a" * 64,
        )
    )
    await session.flush()
    await review_counts.refresh_review_counts()
    dag = (await _build_dag_context(SimpleNamespace(), session, {}))["dag"]
    assert dag["reviewCountsKnown"] == 1
    assert dag["reviewPending"] == (await get_proposal_stats(session)).pending == 1
    assert dag["reviewDedupe"] == await count_duplicate_groups(session) == 1
    assert dag["reviewJunk"] == await count_open_groups(session) == 1
    assert dag["reviewCue"] == await count_cue_review_candidates(session) == 1


@pytest.mark.asyncio
async def test_failed_refresh_retains_last_good_but_poll_marks_unknown(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    good = {"reviewPending": 11, "reviewDedupe": 7, "reviewJunk": 5, "reviewCue": 3}
    monkeypatch.setattr(review_counts, "compute_review_counts", AsyncMock(return_value=good))
    await review_counts.refresh_review_counts()
    assert review_counts.get_cached_review_counts()["reviewCountsKnown"] == 1
    monkeypatch.setattr(review_counts, "compute_review_counts", AsyncMock(side_effect=RuntimeError("read failed")))
    with pytest.raises(RuntimeError, match="read failed"):
        await review_counts.refresh_review_counts()
    assert review_counts.get_cached_review_counts() == {**good, "reviewCountsKnown": 0}
    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    assert "dag-seed-reviewCountsKnown" in response.text
    assert "$store.pipeline.reviewCountsKnown = 0" in response.text
    assert "$store.pipeline.reviewDedupe = 7" in response.text


@pytest.mark.asyncio
async def test_unmeasured_poll_emits_unknown(client: AsyncClient) -> None:
    assert review_counts.get_cached_review_counts()["reviewCountsKnown"] == 0
    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    assert "$store.pipeline.reviewCountsKnown = 0" in response.text


@pytest.mark.asyncio
async def test_cue_read_error_cannot_poison_cache(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session, "execute", AsyncMock(side_effect=RuntimeError("cue unavailable")))
    assert await count_cue_review_candidates(session) == 0
    with pytest.raises(RuntimeError, match="cue unavailable"):
        await count_cue_review_candidates(session, strict=True)


def test_cache_reader_returns_independent_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_counts, "_review_cache", {"reviewPending": 4, "reviewDedupe": 3, "reviewJunk": 2, "reviewCue": 1})
    copy = review_counts.get_cached_review_counts()
    copy["reviewPending"] = 900
    assert review_counts.get_cached_review_counts()["reviewPending"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_review_refresh_loop_survives_failures_and_cancels(monkeypatch: pytest.MonkeyPatch, failure: bool) -> None:
    import phaze.main as main

    refresh = AsyncMock(side_effect=RuntimeError("read failed") if failure else None)
    monkeypatch.setattr(main, "refresh_review_counts", refresh)
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await main._review_refresh_loop()
    refresh.assert_awaited_once()
    sleep.assert_awaited_once_with(review_counts._REVIEW_TTL_SECONDS)


@pytest.mark.asyncio
async def test_dag_attention_reuses_supplied_request_counts(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    import phaze.routers.pipeline.dashboard_stats as stats

    for reader in (
        "get_analysis_failed_count",
        "get_analysis_stalled_count",
        "get_inadmissible_count",
        "get_awaiting_cloud_count",
        "get_cloud_phase_counts",
    ):
        monkeypatch.setattr(stats, reader, AsyncMock(side_effect=AssertionError("duplicate attention read")))
    attention = {"analyzeFailed": 11, "analyzeStalled": 3, "inadmissible": 2, "awaitingCloud": 7, "queuedBehindQuota": 5}
    dag = (await _build_dag_context(SimpleNamespace(), session, {}, attention=attention))["dag"]
    assert {key: dag[key] for key in attention} == attention
    assert all(type(dag[key]) is int for key in attention)


@pytest.mark.asyncio
async def test_refresh_loop_propagates_cancellation_during_read(monkeypatch: pytest.MonkeyPatch) -> None:
    import phaze.main as main

    monkeypatch.setattr(main, "refresh_review_counts", AsyncMock(side_effect=asyncio.CancelledError))
    sleep = AsyncMock()
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await main._review_refresh_loop()
    sleep.assert_not_awaited()


def test_stalled_refresher_marks_last_good_counts_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_counts, "_review_cache", {"reviewPending": 11, "reviewDedupe": 7, "reviewJunk": 5, "reviewCue": 3})
    monkeypatch.setattr(review_counts, "_review_cache_measured_at", 100.0)
    monkeypatch.setattr(review_counts, "_review_cache_available", True)
    monkeypatch.setattr(review_counts.time, "monotonic", lambda: 100.0 + review_counts._REVIEW_MAX_AGE_SECONDS)
    assert review_counts.get_cached_review_counts()["reviewCountsKnown"] == 1
    monkeypatch.setattr(review_counts.time, "monotonic", lambda: 100.0 + review_counts._REVIEW_MAX_AGE_SECONDS + 0.001)
    snapshot = review_counts.get_cached_review_counts()
    assert snapshot["reviewCountsKnown"] == 0
    assert snapshot["reviewDedupe"] == 7


@pytest.mark.asyncio
async def test_default_cue_facade_keeps_session_only_reader_contract(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    import phaze.services.review as review

    async def count(session_arg: AsyncSession) -> int:
        assert session_arg is session
        return 17

    monkeypatch.setattr(review, "_cue_reader", lambda: SimpleNamespace(count_cue_review_candidates=count))
    assert await review.count_cue_review_candidates(session) == 17
