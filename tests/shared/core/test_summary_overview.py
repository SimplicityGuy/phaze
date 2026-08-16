"""State derivation and template contracts for the actionable Summary overview."""

from pathlib import Path

from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader

from phaze.routers.shell import _derive_summary_overview


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
        "queued_behind_quota_count": 0,
        "analysis_working": 0,
        "analyses_today": 0,
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
    summary = _derive(progress, analyses_today=2)

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
        queued_behind_quota_count=7,
    )

    assert summary["is_degraded"] is True
    titles = [item["title"] for item in summary["attention"]]  # type: ignore[index]
    assert titles == [
        "Metadata failures",
        "Analysis failures",
        "Orphaned work",
        "Cloud jobs blocked by configuration",
        "Files awaiting cloud capacity",
        "Cloud quota wait",
    ]
    assert summary["recommended"]["href"] == "/s/metadata"  # type: ignore[index]


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
    assert "md:grid-cols-2" in rendered
    assert "md:grid-cols-3" in rendered
    assert "xl:grid-cols-3" in rendered
    assert "w-[" not in rendered
    links = soup.select("a[hx-get]")
    assert links
    assert all(isinstance(link, Tag) and link.get("href") == link.get("hx-get") for link in links)
    assert all(link.get("hx-target") == "#stage-workspace" for link in links)
