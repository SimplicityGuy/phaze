"""State derivation and template contracts for the actionable Summary overview."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader
import pytest
from sqlalchemy import text

from phaze.models.analysis import AnalysisResult
from phaze.models.metadata import FileMetadata
from phaze.routers import shell as shell_mod
from phaze.routers.shell import _derive_summary_overview, _get_summary_aggregates
from phaze.services.backends import get_analysis_activity_counts
from phaze.services.proposal_queries import ProposalStats


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"


def _progress(*, total: int = 4, metadata: dict[str, int] | None = None, analyze: dict[str, int] | None = None) -> dict[str, dict[str, int | None]]:
    empty = {"not_started": total, "in_flight": 0, "done": 0, "skipped": 0, "failed": 0, "total": total}
    return {
        "discovery": {"done": total, "total": total},
        "metadata": empty | (metadata or {}),
        "analyze": empty | (analyze or {}),
        "tracklist": {"done": 0, "total": None},
        "match": {"done": 0, "total": 0},
        "proposals": {"done": 0, "total": 0},
        "execute": {"done": 0, "total": 0},
    }


def _derive(progress: dict[str, dict[str, int | None]], **overrides: object) -> dict[str, object]:
    args: dict[str, object] = {
        "proposal_pending": 0,
        "proposal_approved": 0,
        "active_fileservers": 1,
        "orphan_counts": {"metadata": 0, "analyze": 0},
        "stalled_analyses": 0,
        "inadmissible_count": 0,
        "awaiting_cloud_count": 0,
        "awaiting_hold_reason": None,
        "queued_behind_quota_count": 0,
        "stage_paused": {"metadata": False, "analyze": False},
        "enriched_count": 0,
        "analysis_live": 0,
        "analyses_today": 0,
        "analyses_lifetime": 0,
    }
    args.update(overrides)
    return _derive_summary_overview(progress, **args)  # type: ignore[arg-type]


def test_empty_and_partially_configured_states_recommend_real_destinations() -> None:
    empty = _derive(_progress(total=0), active_fileservers=1)
    assert empty["is_empty"] is True
    assert empty["recommended"]["href"] == "/s/discover"  # type: ignore[index]

    partial = _derive(_progress(total=0), active_fileservers=0)
    assert partial["is_partially_configured"] is True
    assert partial["attention"][0]["href"] == "/s/agents"  # type: ignore[index]
    assert partial["recommended"]["title"] == "No file-server agent available"  # type: ignore[index]


def test_complete_state_has_no_attention_and_reports_parallel_enrichment() -> None:
    complete_bucket = {"not_started": 0, "done": 4}
    progress = _progress(metadata=complete_bucket, analyze=complete_bucket)
    progress["proposals"] = {"done": 4, "total": 4}
    progress["execute"] = {"done": 4, "total": 0}
    summary = _derive(progress, enriched_count=4, analyses_today=2, analyses_lifetime=4)

    assert summary["attention"] == []
    assert summary["recommended"]["title"] == "Pipeline caught up"  # type: ignore[index]
    assert summary["resolved_enrichment"] == 4
    assert summary["recent"] == {"live": 0, "today": 2, "lifetime": 4}


def test_degraded_state_prioritizes_failures_then_orphans_and_capacity() -> None:
    progress = _progress(
        metadata={"not_started": 2, "failed": 2},
        analyze={"not_started": 2, "in_flight": 1, "failed": 1},
    )
    summary = _derive(
        progress,
        orphan_counts={"metadata": 2, "analyze": 1},
        stalled_analyses=1,
        inadmissible_count=3,
        awaiting_cloud_count=5,
        awaiting_hold_reason="held — all lanes at capacity (4/4 slots busy)",
        queued_behind_quota_count=7,
    )

    assert summary["is_degraded"] is True
    titles = [item["title"] for item in summary["attention"]]  # type: ignore[index]
    assert titles == [
        "Metadata failures",
        "Analysis failures",
        "Orphaned work",
        "Cloud jobs blocked by configuration",
        "Files awaiting cloud routing",
        "Cloud quota wait",
    ]
    assert summary["recommended"]["href"] == "/s/metadata"  # type: ignore[index]


def test_in_flight_enrichment_recommends_runnable_parallel_work_then_monitoring() -> None:
    metadata_running = _progress(metadata={"not_started": 2, "in_flight": 2})
    start_analyze = _derive(metadata_running)
    assert start_analyze["recommended"]["title"] == "Start audio analysis"  # type: ignore[index]

    both_running = _progress(
        metadata={"not_started": 0, "in_flight": 2},
        analyze={"not_started": 0, "in_flight": 2},
    )
    monitor = _derive(both_running)
    assert monitor["recommended"]["title"] == "Enrichment is in progress"  # type: ignore[index]
    assert monitor["recommended"]["href"] == "/s/metadata"  # type: ignore[index]


def test_paused_stage_is_not_recommended_as_new_work() -> None:
    metadata_paused = _derive(
        _progress(metadata={"not_started": 4}, analyze={"not_started": 0, "done": 4}),
        stage_paused={"metadata": True, "analyze": False},
    )
    assert metadata_paused["recommended"]["title"] == "Metadata enrichment is paused"  # type: ignore[index]
    paused_attention = next(item for item in metadata_paused["attention"] if item["title"] == "Metadata enrichment is paused")  # type: ignore[union-attr]
    assert paused_attention["href"] == "/s/metadata"
    assert "4 not started" in paused_attention["detail"]

    paused_attention_stays_prioritized = _derive(
        _progress(),
        stage_paused={"metadata": True, "analyze": False},
    )
    assert paused_attention_stays_prioritized["recommended"]["title"] == "Metadata enrichment is paused"  # type: ignore[index]


@pytest.mark.parametrize(
    ("reason", "href", "action"),
    [
        ("cloud routing disabled", "/s/operations", "Open Routing"),
        ("held — cloud routing paused (force-local)", "/s/operations", "Open Routing"),
        ("held — no cloud backend reachable", "/s/agents", "Inspect Compute"),
        ("held — all lanes at capacity (2/2 slots busy)", "/s/agents", "Inspect Compute"),
        ("held — no fileserver agent online", "/s/agents", "Inspect Compute"),
        ("queued — 2 free slots, dispatching on next drain tick (~5 min)", "/s/analyze", "Open Analyze"),
        ("held", "/s/analyze", "Open Analyze"),
    ],
)
def test_awaiting_cloud_reuses_exact_reason_and_routes_to_the_relevant_workspace(reason: str, href: str, action: str) -> None:
    summary = _derive(_progress(), awaiting_cloud_count=3, awaiting_hold_reason=reason)
    item = next(item for item in summary["attention"] if item["title"] == "Files awaiting cloud routing")  # type: ignore[union-attr]
    assert reason in item["detail"]
    assert item["href"] == href
    assert item["action"] == action


def test_unknown_metrics_remain_unknown_and_do_not_claim_partial_configuration() -> None:
    summary = _derive(
        _progress(),
        active_fileservers=None,
        enriched_count=None,
        analysis_live=None,
        analyses_today=None,
        analyses_lifetime=None,
    )
    assert summary["is_partially_configured"] is False
    assert summary["resolved_enrichment"] is None
    assert summary["recent"] == {"live": None, "today": None, "lifetime": None}


@pytest.mark.asyncio
async def test_enriched_count_is_the_same_file_intersection(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    metadata_only = await make_file(original_filename="metadata-only.mp3")
    analysis_only = await make_file(original_filename="analysis-only.mp3")
    session.add(FileMetadata(file_id=metadata_only.id))
    session.add(AnalysisResult(file_id=analysis_only.id, analysis_completed_at=datetime.now(UTC)))
    await session.commit()

    aggregates = await _get_summary_aggregates(session)

    assert aggregates["enriched"] == 0
    assert (await get_analysis_activity_counts(session))["lifetime"] == 1


@pytest.mark.asyncio
async def test_today_is_bounded_by_utc_midnight_regardless_of_session_timezone(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    before_utc_midnight = await make_file(original_filename="before-midnight.mp3")
    after_utc_midnight = await make_file(original_filename="after-midnight.mp3")
    session.add_all(
        [
            AnalysisResult(file_id=before_utc_midnight.id, analysis_completed_at=datetime(2026, 8, 15, 23, 59, tzinfo=UTC)),
            AnalysisResult(file_id=after_utc_midnight.id, analysis_completed_at=datetime(2026, 8, 16, 0, 1, tzinfo=UTC)),
        ]
    )
    await session.commit()
    await session.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))

    activity = await get_analysis_activity_counts(session, now=datetime(2026, 8, 16, 0, 30, tzinfo=UTC))

    assert activity["today"] == 1
    assert activity["lifetime"] == 2


@pytest.mark.asyncio
async def test_summary_aggregate_clock_rejects_naive_time(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await get_analysis_activity_counts(session, now=datetime(2026, 8, 16, 0, 30))


@pytest.mark.asyncio
async def test_summary_independent_reads_run_in_parallel_under_the_shared_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    peak = 0
    fanout = asyncio.Semaphore(2)

    async def bounded_read(semaphore: asyncio.Semaphore, fn, default):  # type: ignore[no-untyped-def]
        nonlocal active, peak
        assert semaphore is fanout
        async with semaphore:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            try:
                return await fn(object())
            finally:
                active -= 1

    async def stage_progress(_session: object) -> dict[str, dict[str, int | None]]:
        return _progress(total=0)

    async def proposal_stats(_session: object) -> ProposalStats:
        return ProposalStats(total=0, pending=0, approved=0, executed=0, rejected=0, avg_confidence=None)

    async def zero(_session: object) -> int:
        return 0

    async def phases(_session: object) -> dict[str, int]:
        return {"queued_behind_quota": 0, "admitted": 0, "running": 0, "finished": 0}

    async def controls(_session: object) -> dict[str, dict[str, int | bool]]:
        return {"metadata": {"paused": False, "priority": 50}, "analyze": {"paused": False, "priority": 50}}

    async def aggregates(_session: object) -> dict[str, int | None]:
        return {"enriched": 0, "active_fileservers": 1}

    async def live(_session: object, _app_state: object) -> int:
        return 0

    async def activity(_session: object) -> dict[str, int | None]:
        return {"today": 0, "lifetime": 0}

    monkeypatch.setattr(shell_mod, "_stats_fanout", lambda: fanout)
    monkeypatch.setattr(shell_mod, "_read_in_own_session", bounded_read)
    monkeypatch.setattr(shell_mod, "get_stage_progress", stage_progress)
    monkeypatch.setattr(shell_mod, "get_proposal_stats", proposal_stats)
    monkeypatch.setattr(shell_mod, "get_analysis_stalled_count", zero)
    monkeypatch.setattr(shell_mod, "get_inadmissible_count", zero)
    monkeypatch.setattr(shell_mod, "get_awaiting_cloud_count", zero)
    monkeypatch.setattr(shell_mod, "get_cloud_phase_counts", phases)
    monkeypatch.setattr(shell_mod, "get_stage_controls", controls)
    monkeypatch.setattr(shell_mod, "_get_summary_aggregates", aggregates)
    monkeypatch.setattr(shell_mod, "get_analysis_live_count", live)
    monkeypatch.setattr(shell_mod, "get_analysis_activity_counts", activity)

    context = await shell_mod._build_summary_context(SimpleNamespace(), object())  # type: ignore[arg-type]

    assert context["summary"]["total_files"] == 0
    assert peak == 2, "independent reads should overlap but never exceed the shared cap"


def test_review_and_apply_recommendations_follow_current_proposal_state() -> None:
    complete_bucket = {"not_started": 0, "done": 4}
    progress = _progress(metadata=complete_bucket, analyze=complete_bucket)
    progress["proposals"] = {"done": 4, "total": 4}

    review = _derive(progress, proposal_pending=2)
    assert review["recommended"]["href"] == "/s/rename"  # type: ignore[index]

    apply = _derive(progress, proposal_approved=2)
    assert apply["recommended"]["href"] == "/s/apply"  # type: ignore[index]


def test_template_uses_shared_primitives_native_htmx_links_and_responsive_grids() -> None:
    complete_bucket = {"not_started": 0, "done": 4}
    summary = _derive(_progress(metadata=complete_bucket, analyze=complete_bucket))
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    rendered = env.get_template("shell/partials/summary_overview.html").render(summary=summary)
    soup = BeautifulSoup(rendered, "html.parser")

    assert soup.select_one("[data-summary-overview]") is not None
    assert "Parallel enrichment" in soup.get_text(" ", strip=True)
    assert "Live now" in soup.get_text(" ", strip=True)
    assert "Today (UTC)" in soup.get_text(" ", strip=True)
    assert "Lifetime" in soup.get_text(" ", strip=True)
    assert "$store.pipeline.summaryRecentLive" in rendered
    assert "$store.pipeline.summaryRecentToday" in rendered
    assert "$store.pipeline.summaryRecentLifetime" in rendered
    assert "md:grid-cols-2" in rendered
    assert "md:grid-cols-3" in rendered
    assert "xl:grid-cols-3" in rendered
    assert "w-[" not in rendered
    links = soup.select("a[hx-get]")
    assert links
    assert all(isinstance(link, Tag) and link.get("href") == link.get("hx-get") for link in links)
    assert all(link.get("hx-target") == "#stage-workspace" for link in links)


def test_template_renders_unknown_activity_and_intersection_as_em_dashes() -> None:
    summary = _derive(
        _progress(),
        enriched_count=None,
        analysis_live=None,
        analyses_today=None,
        analyses_lifetime=None,
    )
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    soup = BeautifulSoup(env.get_template("shell/partials/summary_overview.html").render(summary=summary), "html.parser")
    text_content = soup.get_text(" ", strip=True)
    assert "Fully enriched —" in text_content
    assert text_content.count("—") >= 4
