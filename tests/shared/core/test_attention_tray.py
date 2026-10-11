"""One attention policy and known snapshot contract across Summary, header and lazy tray."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from phaze.routers.shell.summary import SummaryOverviewInputs, _build_attention_items
from phaze.services.attention import derive_attention_items
from phaze.services.pipeline import attention_snapshot as snapshot
from phaze.services.pipeline.common import _safe_count, capture_read_degradations, mark_read_degraded


if TYPE_CHECKING:
    from httpx import AsyncClient


@pytest.fixture(autouse=True)
def clean_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "_items", [])
    monkeypatch.setattr(snapshot, "_available", False)
    monkeypatch.setattr(snapshot, "_measured_at", 0.0)


def _inputs() -> SummaryOverviewInputs:
    return SummaryOverviewInputs(
        proposal_pending=0,
        proposal_approved=0,
        active_fileservers=0,
        orphan_counts={"metadata": 1, "analyze": 2},
        stalled_analyses=2,
        inadmissible_count=3,
        awaiting_cloud_count=4,
        awaiting_hold_reason="held — all lanes at capacity",
        queued_behind_quota_count=5,
        stage_paused={"metadata": True, "analyze": True},
        enriched_count=0,
        analysis_live=0,
        analyses_today=0,
        analyses_lifetime=0,
    )


async def test_degraded_count_keeps_legacy_zero_but_marks_snapshot_unknown() -> None:
    session = MagicMock()
    session.begin_nested.side_effect = RuntimeError("synthetic database unavailable")
    with capture_read_degradations() as failures:
        assert await _safe_count(session, MagicMock(), node="inadmissible") == 0
    assert failures == ["inadmissible"]

    # Another request's child task must not poison this snapshot's availability.
    async def unrelated() -> None:
        mark_read_degraded("unrelated")

    task = asyncio.create_task(unrelated())
    with capture_read_degradations() as healthy:
        await task
    assert healthy == []


async def test_summary_exposes_read_degradation_without_changing_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    from phaze.routers.shell import summary

    async def read(_state: object, _session: object) -> dict:
        mark_read_degraded("stage_controls")
        return {"summary": {"attention": [{"title": "Existing attention"}], "attention_known": True}}

    monkeypatch.setattr(summary, "_read_summary_context", read)
    context = await summary._build_summary_context(None, MagicMock())
    assert context["summary"]["attention"] == [{"title": "Existing attention"}]
    assert context["summary"]["attention_known"] is False


def test_shared_attention_and_summary_wrapper_have_identical_items_and_hrefs() -> None:
    bucket = {"failed": 2, "not_started": 4, "in_flight": 1}
    inputs = _inputs()
    actual = derive_attention_items(bucket, bucket, (2, 2, 3), inputs)
    assert actual == _build_attention_items(bucket, bucket, (2, 2, 3), inputs)
    assert [item["href"] for item in actual] == [item["href"] for item in _build_attention_items(bucket, bucket, (2, 2, 3), inputs)]
    assert len(actual) == 9
    assert [item["priority"] for item in actual] == [10, 11, 20, 30, 40, 41, 45, 46, 50]
    assert all(item["tone"] != "danger" for item in actual if int(item["priority"]) >= 30)
    # Routine queued work is not a failure or incident. Empty stage controls yield no actionable item.
    neutral = replace(
        inputs,
        active_fileservers=1,
        inadmissible_count=0,
        awaiting_cloud_count=0,
        queued_behind_quota_count=0,
        stage_paused={"metadata": False, "analyze": False},
    )
    assert derive_attention_items(bucket, bucket, (0, 0, 0), neutral) == []


def test_snapshot_unknown_fresh_stale_and_failed_refresh_preserve_last_good(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(snapshot.time, "monotonic", lambda: clock[0])
    assert snapshot.get_attention_counts() == {"attentionCount": 0, "attentionKnown": 0}
    item = {"title": "Cloud quota wait", "href": "/s/agents"}
    snapshot.set_attention_snapshot([item], known=True)
    item["title"] = "mutated"
    items, known = snapshot.get_attention_snapshot()
    assert known and items[0]["title"] == "Cloud quota wait"
    items.clear()
    assert snapshot.get_attention_counts() == {"attentionCount": 1, "attentionKnown": 1}
    clock[0] += 15.001
    assert snapshot.get_attention_counts() == {"attentionCount": 1, "attentionKnown": 0}
    snapshot.set_attention_snapshot([], known=False)
    assert snapshot.get_attention_snapshot() == ([{"title": "Cloud quota wait", "href": "/s/agents"}], False)
    snapshot.set_attention_snapshot([], known=True)
    assert snapshot.get_attention_counts() == {"attentionCount": 0, "attentionKnown": 1}


@pytest.mark.parametrize("known", [False, True])
async def test_lazy_fragment_uses_cached_same_items_without_exposing_unknown(client: AsyncClient, known: bool) -> None:
    item = {
        "priority": 10,
        "title": "Metadata failures",
        "detail": "2 file(s) need inspection or retry.",
        "href": "/s/files?stage=metadata&bucket=failed&scope=media",
        "action": "Open Files",
        "tone": "danger",
    }
    snapshot.set_attention_snapshot([item], known=known)
    response = await client.get("/shell/attention", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text
    assert "HX-Request" in response.headers["vary"]
    assert "no-store" in response.headers["cache-control"]
    assert ("Metadata failures" in response.text) is known
    assert ('href="/s/files?stage=metadata&amp;bucket=failed&amp;scope=media"' in response.text) is known
    assert ("Attention status unavailable" in response.text) is (not known)


@pytest.mark.parametrize("headers", [{}, {"HX-Request": "true", "HX-History-Restore-Request": "true"}])
async def test_attention_direct_visit_and_restore_render_full_shell(client: AsyncClient, headers: dict[str, str]) -> None:
    response = await client.get("/shell/attention", headers=headers)
    assert response.status_code == 200
    assert "<html" in response.text
    assert 'id="attention-pill"' in response.text
    assert "HX-Request" in response.headers["vary"]


@pytest.mark.parametrize("failure", [False, True])
async def test_off_request_refresh_publishes_or_invalidates_and_cancels(monkeypatch: pytest.MonkeyPatch, failure: bool) -> None:
    import phaze.main as main

    reader = (
        AsyncMock(side_effect=RuntimeError("unavailable"))
        if failure
        else AsyncMock(return_value={"summary": {"attention": [], "attention_known": True}})
    )
    monkeypatch.setattr(main, "_build_summary_context", reader)
    cm = AsyncMock()
    monkeypatch.setattr(main, "async_session", MagicMock(return_value=cm))
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await main._attention_refresh_loop(MagicMock())
    assert snapshot.get_attention_counts() == {"attentionCount": 0, "attentionKnown": int(not failure)}


async def test_cancel_during_read_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    import phaze.main as main

    monkeypatch.setattr(main, "_build_summary_context", AsyncMock(side_effect=asyncio.CancelledError))
    monkeypatch.setattr(main, "async_session", MagicMock(return_value=AsyncMock()))
    with pytest.raises(asyncio.CancelledError):
        await main._attention_refresh_loop(MagicMock())
