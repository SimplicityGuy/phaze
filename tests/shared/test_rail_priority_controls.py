"""Stage-control placement and rail-status coverage."""

from __future__ import annotations

from pathlib import Path
import re

import pytest


_TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "phaze" / "templates"
_RAIL = _TEMPLATES / "shell" / "partials" / "rail.html"
_ENRICH_STAGES = ("metadata", "analyze")


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


@pytest.mark.parametrize("stage", _ENRICH_STAGES)
def test_orphan_badge_remains_read_only_rail_status(stage: str) -> None:
    html = _RAIL.read_text()
    assert f'x-show="$store.pipeline.{stage}Orphan > 0"' in html
    assert f'x-text="$store.pipeline.{stage}Orphan"' in html
    assert "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-400" in html
    assert 'role="status"' in html


@pytest.mark.parametrize("stage", _ENRICH_STAGES)
def test_enrich_numeral_renders_done_over_total_with_matching_labels(stage: str) -> None:
    html = _RAIL.read_text()
    assert f"$store.pipeline.{stage}Done" in html
    assert f"$store.pipeline.{stage}Total" in html
    title = re.search(rf':title="([^\"]*{stage}Done[^\"]*)"', html)
    aria = re.search(rf':aria-label="([^\"]*{stage}Done[^\"]*)"', html)
    assert title and aria
    assert title.group(1) == aria.group(1)


def test_other_rail_numerals_remain_labeled() -> None:
    html = _RAIL.read_text()
    for key in ("discovered", "tracklistDone", "proposalsDone"):
        assert re.search(rf':title="\$store\.pipeline\.{key}[^\"]*"', html)
        assert re.search(rf':aria-label="\$store\.pipeline\.{key}[^\"]*"', html)
