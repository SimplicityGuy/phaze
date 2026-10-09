"""One display name per shell page (phaze-yyfax).

The rail label, the workspace ``<h1>``, the document title and the command palette all read
``phaze.web.template_globals.PAGE_NAMES``. These tests hold that: they render every shell stage through the
real app and compare the surfaces, instead of trusting that each template was edited to match.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup
import pytest

from phaze.routers.shell import STAGE_PARTIALS, UTILITY_PANES
from phaze.routers.shell.summary import SummaryOverviewInputs, _derive_summary_overview
from phaze.web.template_globals import PAGE_NAMES, PALETTE_PAGE_KEYS, open_label, palette_pages


if TYPE_CHECKING:
    from httpx import AsyncClient

_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
# Aliases render the Changes Review workspace but have no rail node of their own.
_ALIASES = {"tagwrite": "rename", "move": "rename"}
_ALL_STAGES = [*STAGE_PARTIALS, *UTILITY_PANES]


def test_every_shell_stage_has_a_page_name() -> None:
    assert set(PAGE_NAMES) == set(_ALL_STAGES)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", _ALL_STAGES)
async def test_sidebar_h1_and_document_title_agree_for_every_stage(client: AsyncClient, make_file, stage: str) -> None:  # type: ignore[no-untyped-def]
    """The sidebar label, the page H1 and the browser-tab title are the same string for every stage."""
    await make_file()  # a zero-file Analyze renders the first-run guide (its own H1), not the workspace
    response = await client.get(f"/s/{stage}")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    expected = PAGE_NAMES[stage]

    assert soup.title is not None
    assert soup.title.get_text(strip=True) == f"{expected} | Phaze"
    h1s = soup.select("#stage-workspace h1")
    assert [h.get_text(" ", strip=True) for h in h1s][:1] == [expected]

    rail_stage = _ALIASES.get(stage, stage)
    link = soup.select_one(f'a[data-rail-stage="{rail_stage}"]')
    assert link is not None
    # Config opens from the header gear (an icon with an aria-label); every other page from a rail row.
    label = link.get("aria-label") if stage == "runtime-config" else link.select_one("span.flex-1").get_text(strip=True)  # type: ignore[union-attr]
    assert label == PAGE_NAMES[rail_stage] == expected


@pytest.mark.asyncio
async def test_palette_lists_every_navigable_page_under_its_sidebar_name(client: AsyncClient) -> None:
    """The ⌘K Navigate group is exactly the set of navigable pages (aliases excluded, Config included)."""
    response = await client.get("/search/", headers={"HX-Request": "true"})
    soup = BeautifulSoup(response.text, "html.parser")
    rows = {a["href"]: a.get_text(" ", strip=True).lstrip("→").strip() for a in soup.select('a[id^="cmdk-nav-"]')}

    navigable = {stage for stage in _ALL_STAGES if stage not in _ALIASES}
    assert rows == {f"/s/{stage}": PAGE_NAMES[stage] for stage in navigable}
    assert set(PALETTE_PAGE_KEYS) == navigable
    assert [stage for stage, _, _ in palette_pages()] == list(PALETTE_PAGE_KEYS)

    # ...and each name is what the rail shows for that destination.
    rail = BeautifulSoup((await client.get("/")).text, "html.parser")
    for stage in navigable - {"runtime-config"}:
        rail_link = rail.select_one(f'nav a[data-rail-stage="{stage}"] span.flex-1')
        assert rail_link is not None
        assert rail_link.get_text(strip=True) == rows[f"/s/{stage}"]


def _all_summary_actions() -> list[dict[str, object]]:
    """Every attention item and recommended action the Summary can emit, across every branch."""
    progress: dict[str, dict[str, int | None]] = {
        "discovery": {"done": 4, "total": 4},
        "metadata": {"not_started": 4, "in_flight": 0, "done": 0, "skipped": 0, "failed": 2, "total": 4},
        "analyze": {"not_started": 4, "in_flight": 0, "done": 0, "skipped": 0, "failed": 2, "total": 4},
        "tracklist": {"done": 0, "total": None},
        "match": {"done": 0, "total": 0},
        "proposals": {"done": 0, "total": 0},
        "execute": {"done": 0, "total": 0},
    }
    done = {"not_started": 0, "in_flight": 0, "done": 4, "skipped": 0, "failed": 0, "total": 4}
    base: dict[str, object] = {
        "proposal_pending": 0,
        "proposal_approved": 0,
        "active_fileservers": 0,
        "orphan_counts": {"metadata": 1, "analyze": 1},
        "stalled_analyses": 1,
        "inadmissible_count": 1,
        "awaiting_cloud_count": 1,
        "awaiting_hold_reason": "held — no cloud backend reachable",
        "queued_behind_quota_count": 1,
        "stage_paused": {"metadata": True, "analyze": True},
        "enriched_count": 0,
        "analysis_live": 0,
        "analyses_today": 0,
        "analyses_lifetime": 0,
    }
    calm = base | {
        "active_fileservers": 1,
        "orphan_counts": {"metadata": 0, "analyze": 0},
        "inadmissible_count": 0,
        "awaiting_cloud_count": 0,
        "queued_behind_quota_count": 0,
        "stage_paused": {"metadata": False, "analyze": False},
    }
    scenarios: list[tuple[dict[str, dict[str, int | None]], dict[str, object]]] = [(progress, base)]
    for reason in ("cloud routing disabled", "held", "held — no cloud backend reachable"):
        scenarios.append((progress, base | {"awaiting_hold_reason": reason}))
    ready = progress | {"metadata": done, "analyze": done, "proposals": {"done": 4, "total": 4}}
    scenarios += [
        (ready, calm | {"proposal_pending": 2}),
        (ready, calm | {"proposal_approved": 2}),
        (ready, calm),
        (progress | {"metadata": done | {"in_flight": 1, "done": 3}}, calm),
        (progress | {"metadata": done, "analyze": done | {"in_flight": 1, "done": 3}}, calm),
        (progress | {"metadata": done, "analyze": done, "proposals": {"done": 0, "total": 4}}, calm),
        (progress | {"metadata": done | {"not_started": 4, "done": 0}, "analyze": done}, calm),
        (progress | {"metadata": done, "analyze": done | {"not_started": 4, "done": 0}}, calm),
        (
            progress | {"metadata": done | {"not_started": 4, "done": 0}, "analyze": done},
            calm | {"stage_paused": {"metadata": True, "analyze": False}},
        ),
        ({**progress, "discovery": {"done": 0, "total": 0}}, calm),
    ]
    actions: list[dict[str, object]] = []
    for scenario_progress, overrides in scenarios:
        summary = _derive_summary_overview(scenario_progress, SummaryOverviewInputs(**overrides))  # type: ignore[arg-type]
        actions += list(summary["attention"])  # type: ignore[call-overload]
        if summary["recommended"]:
            actions.append(summary["recommended"])  # type: ignore[arg-type]
    return actions


def test_summary_link_labels_name_the_page_they_open() -> None:
    """Every Summary alert / recommendation link reads ``Open <page name>`` for the page its href opens."""
    actions = _all_summary_actions()
    assert actions, "the scenarios produced no summary actions at all"
    seen_hrefs = set()
    for item in actions:
        stage = str(item["href"]).removeprefix("/s/")
        assert stage in PAGE_NAMES, f"{item['href']!r} is not a shell page"
        assert item["action"] == open_label(stage), item
        seen_hrefs.add(stage)
    # The two labels the review named as lying must have been exercised, not merely absent.
    assert {"discover", "agents", "runtime-config", "apply", "rename"} <= seen_hrefs


def test_summary_flow_names_the_last_stage_like_the_rest_of_the_app() -> None:
    """The Summary card for the last stage carries the page name (Execute), not a private 'Apply'."""
    from phaze.routers.shell.summary import _build_summary_flow

    bucket = {"not_started": 0, "in_flight": 0, "done": 0, "skipped": 0, "failed": 0, "total": 0}
    progress = {"discovery": {"done": 1, "total": 1}, "metadata": bucket, "analyze": bucket, "proposals": bucket, "execute": {"done": 0, "total": 0}}
    flow = _build_summary_flow(progress, 1, 0, 0)  # type: ignore[arg-type]
    names = {node["href"]: node["name"] for node in flow}
    assert names["/s/apply"] == PAGE_NAMES["apply"] == "Execute"
    assert "Apply" not in names.values()


def test_last_stage_has_one_name_in_the_record_and_files_matrix() -> None:
    """The Files matrix column and the record's stage list spell the last stage as the page name."""
    name = PAGE_NAMES["apply"]
    matrix = (_TEMPLATES / "pipeline/partials/files_table_view.html").read_text()
    record = (_TEMPLATES / "record/_record_content.html").read_text()
    assert f"('{name}', 'apply')" in matrix
    assert f"('apply', '{name}', false)" in record
    assert re.search(r"Execute approved|'Apply'", matrix + record) is None
