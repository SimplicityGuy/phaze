"""One attention policy and known snapshot contract across Summary, header and lazy tray."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from bs4 import BeautifulSoup
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from phaze.enums.stage import Stage
from phaze.routers.shell.summary import SummaryOverviewInputs, _build_attention_items
from phaze.services.attention import analysis_attention_known, derive_attention_items
from phaze.services.pipeline import attention_snapshot as snapshot
from phaze.services.pipeline.buckets import _empty_buckets, _safe_bucket_counts, _safe_orphan_split
from phaze.services.pipeline.common import _safe_count, capture_read_degradations, mark_read_degraded


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


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


@pytest.mark.parametrize("source", ["bucket", "orphan_split"])
async def test_analyze_bucket_fallbacks_mark_the_parent_snapshot(source: str) -> None:
    session = MagicMock()
    session.begin_nested.return_value = AsyncMock()
    session.execute = AsyncMock(side_effect=RuntimeError("synthetic database unavailable"))
    buckets = _empty_buckets()
    buckets["in_flight"] = 1
    with capture_read_degradations() as failures:

        async def read() -> None:
            if source == "bucket":
                assert await _safe_bucket_counts(session, Stage.ANALYZE) == _empty_buckets()
            else:
                await _safe_orphan_split(session, Stage.ANALYZE, buckets)
                assert buckets["in_flight"] == 1

        # Summary reads sources concurrently; child-task fallbacks must reach this collector.
        await asyncio.gather(read())
    assert failures == ["stage_bucket" if source == "bucket" else "stage_orphan_split"]


async def test_summary_exposes_read_degradation_without_changing_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    from phaze.routers.shell import summary

    async def read(_state: object, _session: object) -> dict:
        mark_read_degraded("stage_controls")
        return {"summary": {"attention": [{"title": "Existing attention"}], "attention_known": True}}

    monkeypatch.setattr(summary, "_read_summary_context", read)
    context = await summary._build_summary_context(None, MagicMock())
    assert context["summary"]["attention"] == [{"title": "Existing attention"}]
    assert context["summary"]["attention_known"] is False
    assert context["summary"]["recommended"]["title"] == "Inspect unavailable status"
    assert "caught up" not in context["summary"]["recommended"]["title"]


def test_shared_attention_and_summary_wrapper_have_identical_items_and_hrefs() -> None:
    bucket = {"failed": 2, "not_started": 4, "in_flight": 1}
    inputs = _inputs()
    actual = derive_attention_items(bucket, bucket, (2, 2, 3), inputs)
    assert actual == _build_attention_items(bucket, bucket, (2, 2, 3), inputs)
    assert [item["href"] for item in actual] == [item["href"] for item in _build_attention_items(bucket, bucket, (2, 2, 3), inputs)]
    assert len(actual) == 7
    assert [item["priority"] for item in actual] == [10, 11, 20, 30, 45, 46, 50]
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


@pytest.mark.parametrize(
    "reason", ["held — all lanes at capacity (4/4 slots busy)", "queued — 2 free slots, dispatching on next drain tick (~5 min)"]
)
def test_full_capacity_and_quota_wait_are_neutral(reason: str) -> None:
    inputs = replace(
        _inputs(),
        active_fileservers=1,
        inadmissible_count=0,
        awaiting_hold_reason=reason,
        stage_paused={"metadata": False, "analyze": False},
        lanes=[{"id": "cloud-a", "kind": "kueue", "available": True, "in_flight": 4, "cap": 4}],
    )
    assert derive_attention_items({}, {}, (0, 0, 0), inputs) == []


def test_each_independent_incident_survives_simultaneous_failures() -> None:
    inputs = replace(
        _inputs(),
        active_fileservers=1,
        stage_paused={"metadata": False, "analyze": False},
        lanes=[
            {"id": "local", "kind": "local", "available": True, "stuck": 3, "in_flight": 100, "cap": 4},
            {"id": "cloud-a", "kind": "kueue", "available": False, "breaker_held": True, "in_flight": 5, "cap": 4},
        ],
    )
    items = derive_attention_items({}, {}, (2, 8, 4), inputs)
    by_id = {item["id"]: item for item in items}
    assert len(by_id) == len(items) == 8
    assert set(by_id) == {
        "metadata-failed",
        "analysis-failed",
        "pipeline-orphans",
        "lane:local:stuck",
        "lane:cloud-a:held",
        "lane:cloud-a:unavailable",
        "lane:cloud-a:over-capacity",
        "cloud-inadmissible",
    }
    assert by_id["analysis-failed"]["count"] == 8
    assert by_id["analysis-failed"]["href"] == "/s/files?stage=analyze&bucket=failed&scope=media"
    assert by_id["lane:local:stuck"]["count"] == 3
    assert by_id["lane:cloud-a:over-capacity"]["count"] == 1
    assert by_id["lane:cloud-a:unavailable"]["tone"] == "attention"
    assert by_id["lane:cloud-a:held"]["tone"] == "danger"
    assert all(item["href"] == "/s/analyze?lane=cloud-a" for key, item in by_id.items() if key.startswith("lane:cloud-a:"))


@pytest.mark.parametrize(
    "lanes, reason",
    [
        ([], None),
        ([{"id": "local", "kind": "local", "available": True, "stuck": None}], None),
        ([{"id": "local", "kind": "local", "available": True, "stuck": 0}], "held"),
    ],
)
def test_unmeasured_lane_or_routing_read_invalidates_snapshot(lanes: list[dict], reason: str | None) -> None:
    assert analysis_attention_known(lanes, 1 if reason else 0, reason) is False
    assert analysis_attention_known([{"id": "local", "kind": "local", "available": True, "stuck": 0}], 0, None) is True
    assert analysis_attention_known([{"id": "cloud-a", "kind": "kueue", "available": False, "in_flight": 0}], 0, None) is True
    missing_local_queue = [{"id": "local", "kind": "local", "available": True, "stuck": None}]
    assert analysis_attention_known(missing_local_queue, 0, None, active_fileservers=0) is True
    assert analysis_attention_known(missing_local_queue, 0, None, active_fileservers=1) is False


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


async def test_analyze_and_tray_render_the_same_incidents_and_unknown_state(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = replace(
        _inputs(),
        active_fileservers=1,
        stage_paused={"metadata": False, "analyze": False},
        lanes=[{"id": "local", "kind": "local", "available": True, "stuck": 3}],
    )
    items = derive_attention_items({}, {}, (0, 8, 4), inputs)
    clock = [100.0]
    monkeypatch.setattr(snapshot.time, "monotonic", lambda: clock[0])
    snapshot.set_attention_snapshot(items, known=True)
    tray = BeautifulSoup((await client.get("/shell/attention", headers={"HX-Request": "true"})).text, "html.parser")
    poll = BeautifulSoup((await client.get("/pipeline/stats")).text, "html.parser")
    card = poll.select_one("#analysis-health-card")
    assert card is not None
    expected = [(item["id"], item["tone"]) for item in items]
    assert [(row["data-incident-id"], row["data-incident-tone"]) for row in tray.select("[data-incident-id]")] == expected
    assert [(row["data-incident-id"], row["data-incident-tone"]) for row in card.select("[data-incident-id]")] == expected
    assert len(card.select('[data-incident-id="analysis-failed"]')) == 1
    assert card.select_one('[data-incident-id="analysis-failed"] a')["href"] == "/s/files?stage=analyze&bucket=failed&scope=media"
    clock[0] += 15.001
    stale = (await client.get("/pipeline/stats")).text
    assert "Incident status unavailable" in stale
    assert 'data-incident-id="analysis-failed"' not in stale
    assert "No actionable analysis exceptions" not in stale
    header = BeautifulSoup((await client.get("/s/files")).text, "html.parser").select_one("#attention-pill")
    assert header is not None and "unavailable" in str(header["aria-label"])
    assert "Healthy" not in header.get_text()
    stale_tray = (await client.get("/shell/attention", headers={"HX-Request": "true"})).text
    assert "Attention status unavailable" in stale_tray
    assert 'data-incident-id="analysis-failed"' not in stale_tray


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


@pytest.mark.parametrize("measurement", ["empty", "normal_waiting", "count_failure", "bucket_failure", "lane_failure", "admission_failure"])
async def test_source_measurement_faults_agree_across_real_summary_refresh_and_shell_surfaces(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, measurement: str
) -> None:
    """Real degrade primitives and refresh publication drive every rendered attention consumer."""
    import phaze.main as main
    from phaze.routers.shell import summary
    from phaze.services.backends.lane_snapshot import _admission_by_backend_id
    from tests.shared.routers.pipeline._shared import _reset_saq_jobs_minimal

    await _reset_saq_jobs_minimal(session)
    fault = MagicMock()
    fault.begin_nested.return_value = AsyncMock()
    fault.execute = AsyncMock(side_effect=RuntimeError("synthetic measurement unavailable"))
    fault.rollback = AsyncMock()
    lane = {"id": "cloud-a", "kind": "kueue", "available": True, "in_flight": 4, "cap": 4}

    async def lanes(_session: AsyncSession, _state: object) -> list[dict]:
        if measurement == "lane_failure":
            raise RuntimeError("synthetic lane measurement unavailable")
        if measurement == "admission_failure":
            assert await _admission_by_backend_id(fault) == {}  # Scheduler fallback remains unchanged.
        return [lane]

    async def count(_session: AsyncSession) -> int:
        return await _safe_count(fault, MagicMock(), node="inadmissible")

    read_progress = summary.get_stage_progress

    async def progress(own_session: AsyncSession) -> dict:
        result = await read_progress(own_session)
        # Preserve the real source's zero fallback; the collector must retain its unavailability.
        bucket = await _safe_bucket_counts(fault, Stage.ANALYZE)
        result["analyze"] = bucket | {"total": sum(bucket.values())}
        return result

    monkeypatch.setattr(summary, "get_backend_lane_snapshot", lanes)
    monkeypatch.setattr(summary, "_get_summary_aggregates", AsyncMock(return_value={"enriched": 0, "active_fileservers": 1}))
    monkeypatch.setattr(summary, "get_cached_stage_orphan_counts", lambda: {"metadata": 0, "analyze": 0})
    monkeypatch.setattr(summary, "stage_orphan_counts_known", lambda: True)
    if measurement == "count_failure":
        monkeypatch.setattr(summary, "get_inadmissible_count", count)
    if measurement == "bucket_failure":
        monkeypatch.setattr(summary, "get_stage_progress", progress)
    if measurement == "normal_waiting":
        monkeypatch.setattr(summary, "get_awaiting_cloud_count", AsyncMock(return_value=5))
        monkeypatch.setattr(summary, "derive_cloud_hold_reason", AsyncMock(return_value="held — all lanes at capacity (4/4 slots busy)"))
        monkeypatch.setattr(summary, "get_cloud_phase_counts", AsyncMock(return_value={"queued_behind_quota": 5}))

    known = measurement in {"empty", "normal_waiting"}
    if not known:
        # A failed refresh retains the prior measured exception internally, but every UI
        # consumer must hide it as unmeasured rather than display an unlabelled stale value.
        previous = derive_attention_items(
            {},
            {},
            (0, 3, 0),
            replace(
                _inputs(), active_fileservers=1, stage_paused={"metadata": False, "analyze": False}, inadmissible_count=0, awaiting_cloud_count=0
            ),
        )
        snapshot.set_attention_snapshot(previous, known=True)

    # Run the actual refresh loop for one iteration, including its real Summary collector.
    factory = async_sessionmaker(session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    with monkeypatch.context() as refresh:
        refresh.setattr(main, "async_session", factory)
        refresh.setattr(main, "asyncio", SimpleNamespace(CancelledError=asyncio.CancelledError, sleep=AsyncMock(side_effect=asyncio.CancelledError)))
        with pytest.raises(asyncio.CancelledError):
            await main._attention_refresh_loop(client._transport.app)

    assert snapshot.get_attention_counts() == {"attentionCount": int(not known), "attentionKnown": int(known)}
    if not known:
        assert snapshot.get_attention_snapshot() == (previous, False)
    summary_page = BeautifulSoup((await client.get("/s/summary")).text, "html.parser")
    pill = summary_page.select_one("#attention-pill")
    assert pill is not None
    assert ("Healthy" in pill.get_text()) is known
    assert ("unavailable" in str(pill["aria-label"])) is (not known)
    assert ("Attention status unavailable" in summary_page.get_text()) is (not known)
    assert ("Inspect unavailable status" in summary_page.get_text()) is (not known)
    tray = (await client.get("/shell/attention", headers={"HX-Request": "true"})).text
    card = BeautifulSoup((await client.get("/pipeline/stats")).text, "html.parser").select_one("#analysis-health-card")
    assert card is not None
    assert ("Attention status unavailable" in tray) is (not known)
    assert ("Incident status unavailable" in card.get_text()) is (not known)
    assert ("No actionable analysis exceptions" in card.get_text()) is known
    assert not card.select("[data-incident-id]")
