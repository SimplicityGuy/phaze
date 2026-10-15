"""Stage-control placement and rail-status coverage."""

from __future__ import annotations

from pathlib import Path
import re

from bs4 import BeautifulSoup
from fastapi.templating import Jinja2Templates
import pytest

from phaze.web.template_globals import register_page_name_globals


_TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "phaze" / "templates"
_RAIL = _TEMPLATES / "shell" / "partials" / "rail.html"
_ENRICH_STAGES = ("metadata", "analyze")
_REVIEW_COUNTS = {"rename": "reviewPending", "dedupe": "reviewDedupe", "junk": "reviewJunk", "cue": "reviewCue"}


def _render_rail(seed: dict[str, int] | None) -> BeautifulSoup:
    templates = Jinja2Templates(directory=str(_TEMPLATES))
    register_page_name_globals(templates.env)
    return BeautifulSoup(templates.env.get_template("shell/partials/rail.html").render(stage="summary", pipeline_seed=seed), "html.parser")


def _seed(**values: int) -> dict[str, int]:
    return dict.fromkeys(
        ("discovered", "metadataStatusKnown", "analyzeDone", "analyzeTotal", "tracklistDone", "proposalsDone", "proposalsTotal"), 0
    ) | {
        "seedKnown": 1,
        "reviewCountsKnown": 1,
        **dict.fromkeys(_REVIEW_COUNTS.values(), 12345),
        "executedDone": 1234,
        "executedTotal": 12000,
        "agentOnline": 99,
        "visibleAgentCount": 1234,
        "visibleAgentCountKnown": 1,
        "clusterCount": 5678,
        "clusterCountKnown": 1,
        **values,
    }


@pytest.mark.parametrize("route", [*_REVIEW_COUNTS, "apply"])
def test_review_and_operations_counts_have_identical_formatted_accessible_labels(route: str) -> None:
    count = _render_rail(_seed()).select_one(f'a[data-rail-stage="{route}"] span[x-text]')
    assert count is not None
    assert "formatCount(" in count["x-text"]
    assert "seedKnown" in count["x-text"]
    assert count[":title"] == count[":aria-label"]
    assert "formatCount(" in count[":title"]
    assert count.get_text(strip=True) == ("1,234 / 12,000" if route == "apply" else "12,345")


@pytest.mark.parametrize("seed", [None, _seed(seedKnown=0), _seed(reviewCountsKnown=0)])
def test_review_unknown_never_renders_retained_counts_or_amber(seed: dict[str, int] | None) -> None:
    soup = _render_rail(seed)
    for route in _REVIEW_COUNTS:
        count = soup.select_one(f'a[data-rail-stage="{route}"] span[x-text]')
        assert count is not None
        assert count.get_text(strip=True) == "—"
        assert "text-warn" not in count["class"]
        assert "reviewCountsKnown" in count["x-text"]


@pytest.mark.parametrize("amount", [0, 12345])
def test_amber_is_only_a_known_nonempty_review_queue(amount: int) -> None:
    soup = _render_rail(_seed(**dict.fromkeys(_REVIEW_COUNTS.values(), amount)))
    section = soup.select_one('section[aria-labelledby="nav-review"]')
    assert section is not None
    assert not any("amber" in css for css in section["class"])
    for route, key in _REVIEW_COUNTS.items():
        count = soup.select_one(f'a[data-rail-stage="{route}"] span[x-text]')
        assert count is not None
        assert ("text-warn" in count["class"]) is (amount > 0)
        assert f"$store.pipeline.{key} > 0" in count[":class"]
        assert "$store.pipeline.seedKnown && $store.pipeline.reviewCountsKnown" in count[":class"]
        assert count.get_text(strip=True) == ("12,345" if amount else "0")


@pytest.mark.parametrize("seed", [None, _seed(seedKnown=0)])
def test_execution_unknown_has_no_fabricated_zero(seed: dict[str, int] | None) -> None:
    soup = _render_rail(seed)
    for route, expected in (("apply", "— / —"),):
        count = soup.select_one(f'a[data-rail-stage="{route}"] span[x-text]')
        assert count is not None
        assert count.get_text(strip=True) == expected


@pytest.mark.parametrize(
    ("agents", "clusters", "expected"),
    [
        (0, 0, "0 agents · 0 clusters"),
        (1, 1, "1 agent · 1 cluster"),
        (1, 2, "1 agent · 2 clusters"),
        (1234, 5678, "1,234 agents · 5,678 clusters"),
    ],
)
def test_agents_inventory_summary_is_formatted_visible_link_text(agents: int, clusters: int, expected: str) -> None:
    soup = _render_rail(_seed(visibleAgentCount=agents, clusterCount=clusters))
    link = soup.select_one('a[data-rail-stage="agents"]')
    assert link is not None
    label = link.select_one("[data-rail-label]")
    summary = link.select_one("[data-worker-inventory]")
    assert label is not None and summary is not None
    assert label.get_text(strip=True) == "Agents & clusters"
    assert summary.get_text(strip=True) == expected
    assert link.get_text(" ", strip=True) == f"Agents & clusters {expected}"
    assert label.find_next_sibling() == summary  # The count is beneath its label.
    assert "block" in summary["class"]
    assert "formatCount(" in summary["x-text"]
    assert "visibleAgentCount" in summary["x-text"]
    assert "clusterCount" in summary["x-text"]
    assert "agentOnline" not in summary["x-text"]  # Activity must not stand in for inventory.


@pytest.mark.parametrize(
    ("seed", "expected"),
    [
        (None, "Agents unavailable · Clusters unavailable"),
        (_seed(seedKnown=0), "Agents unavailable · Clusters unavailable"),
        (_seed(visibleAgentCountKnown=0), "Agents unavailable · 5,678 clusters"),
        (_seed(clusterCountKnown=0), "1,234 agents · Clusters unavailable"),
        (_seed(visibleAgentCountKnown=0, clusterCountKnown=0), "Agents unavailable · Clusters unavailable"),
    ],
)
def test_agents_inventory_unknown_components_never_show_retained_counts(seed: dict[str, int] | None, expected: str) -> None:
    soup = _render_rail(seed)
    link = soup.select_one('a[data-rail-stage="agents"]')
    assert link is not None
    summary = link.select_one("[data-worker-inventory]")
    assert summary is not None
    assert summary.get_text(strip=True) == expected
    assert link.get_text(" ", strip=True) == f"Agents & clusters {expected}"
    assert "visibleAgentCountKnown" in summary["x-text"]
    assert "clusterCountKnown" in summary["x-text"]


def _workspace(stage: str) -> str:
    return (_TEMPLATES / "pipeline" / "partials" / f"{stage}_workspace.html").read_text()


@pytest.mark.parametrize("stage", _ENRICH_STAGES)
def test_priority_controls_live_in_relevant_workspace(stage: str) -> None:
    html = _workspace(stage)
    assert f'hx-post="/pipeline/stages/{stage}/priority" hx-vals=\'{{"delta": -10}}\'' in html
    assert f'hx-post="/pipeline/stages/{stage}/priority" hx-vals=\'{{"delta": 10}}\'' in html
    assert f'aria-label="Raise {stage} priority"' in html
    assert f'aria-label="Lower {stage} priority"' in html
    assert f"$store.pipeline.{stage}Priority" in html


@pytest.mark.parametrize("stage", _ENRICH_STAGES)
def test_pause_and_resume_live_in_relevant_workspace(stage: str) -> None:
    html = _workspace(stage)
    assert f'hx-post="/pipeline/stages/{stage}/pause"' in html
    assert f'hx-post="/pipeline/stages/{stage}/resume"' in html
    assert f'aria-label="Pause {stage}"' in html
    assert f'aria-label="Resume {stage}"' in html


def test_rail_contains_no_stage_mutation_controls() -> None:
    html = _RAIL.read_text()
    assert 'hx-post="/pipeline/stages/' not in html
    assert "priority and pause controls" not in html


def test_rail_surfaces_no_orphan_bubble_for_any_stage() -> None:
    """phaze-640na: orphaned work lives on the Summary Needs Attention card, never as a rail bubble."""
    html = _RAIL.read_text()
    assert "Orphan" not in html
    assert "orphan" not in html.lower()
    assert "bg-amber-100 text-warn dark:bg-amber-950" not in html


@pytest.mark.parametrize(
    ("stage", "done_key", "total_key"),
    [("metadata", "metadataStatusDone", "metadataStatusTotal"), ("analyze", "analyzeDone", "analyzeTotal")],
)
def test_enrich_numeral_renders_done_over_total_with_matching_labels(stage: str, done_key: str, total_key: str) -> None:
    html = _RAIL.read_text()
    assert f"$store.pipeline.{done_key}" in html
    assert f"$store.pipeline.{total_key}" in html
    title = re.search(rf':title="([^\"]*{done_key}[^\"]*)"', html)
    aria = re.search(rf':aria-label="([^\"]*{done_key}[^\"]*)"', html)
    assert title and aria
    assert title.group(1) == aria.group(1)


def test_other_rail_numerals_remain_labeled() -> None:
    html = _RAIL.read_text()
    for key in ("discovered", "tracklistDone", "proposalsDone"):
        assert re.search(rf':title="[^\"]*\$store\.pipeline\.{key}[^\"]*"', html)
        assert re.search(rf':aria-label="[^\"]*\$store\.pipeline\.{key}[^\"]*"', html)
