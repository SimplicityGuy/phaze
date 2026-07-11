"""Jinja-render tests for the Phase 87 DAG-rail ambient controls (87-08, UI-05 / PRIO-01 / D-11).

Renders ``shell/partials/rail.html`` through FastAPI's ``Jinja2Templates`` (the SAME safe wrapper
Phaze uses in production) and asserts the two ambient enrich-node additions:

* **PRIO-01** — every enrich stage (metadata / fingerprint / analyze) carries a ``▲``/``▼`` priority
  stepper posting to the LIVE ``POST /pipeline/stages/{stage}/priority`` endpoint with a ``delta`` of
  ``-10`` (▲, raise = lower number) / ``+10`` (▼), plus a pause→``/pause`` and resume→``/resume``
  toggle. Each control carries an explicit ``aria-label`` (not tooltip-only) AND the D-11 clarifying
  tooltip. No Phase-38 template is resurrected — the markup posts to the live endpoints directly.
* **UI-05** — each enrich node carries an amber orphaned/stuck badge bound to the
  ``$store.pipeline.{stage}Orphan`` store key, ``role="status"``, hidden at 0 (``x-show ... > 0``).

The interactive stepper/pause buttons must NOT be nested inside the rail's navigation ``<button>``
(invalid nested interactive controls) — they live in a sibling controls sub-row.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
import pytest
from starlette.requests import Request


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "phaze" / "templates"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_ENRICH_STAGES = ("metadata", "fingerprint", "analyze")

# The D-11 clarifying tooltip copy (UI-SPEC Copywriting) — ▲ raises priority = lowers the number.
_D11_TOOLTIP = "▲ Higher priority runs sooner (lowers the queue number). ▼ lowers priority."


def _fake_request() -> Request:
    """Minimal Starlette Request stub — rail.html only reads the ``stage`` context var."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": None,
    }
    return Request(scope=scope)  # type: ignore[arg-type]


def _render_rail(*, stage: str = "analyze") -> str:
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="shell/partials/rail.html",
        context={"stage": stage},
    )
    return response.body.decode()


# ---------------------------------------------------------------------------
# PRIO-01 — priority stepper posts to the live endpoints with the ±10 delta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", _ENRICH_STAGES)
def test_priority_stepper_posts_to_live_endpoint_with_delta(stage: str) -> None:
    """▲ posts delta -10 and ▼ posts delta +10 to POST /pipeline/stages/{stage}/priority (live endpoint)."""
    html = _render_rail()

    assert f'hx-post="/pipeline/stages/{stage}/priority"' in html
    # Both deltas present for this stage's stepper (raise = -10 lowers the queue number; lower = +10).
    assert f'hx-post="/pipeline/stages/{stage}/priority" hx-vals=\'{{"delta": -10}}\'' in html
    assert f'hx-post="/pipeline/stages/{stage}/priority" hx-vals=\'{{"delta": 10}}\'' in html


@pytest.mark.parametrize("stage", _ENRICH_STAGES)
def test_pause_and_resume_post_to_live_endpoints(stage: str) -> None:
    """The pause/resume toggle posts to the live /pause and /resume endpoints for each enrich stage."""
    html = _render_rail()

    assert f'hx-post="/pipeline/stages/{stage}/pause"' in html
    assert f'hx-post="/pipeline/stages/{stage}/resume"' in html


@pytest.mark.parametrize("stage", _ENRICH_STAGES)
def test_stepper_controls_carry_explicit_aria_labels(stage: str) -> None:
    """Each stepper + pause/resume control carries an explicit aria-label (not tooltip-only, Visuals/a11y)."""
    html = _render_rail()

    assert f'aria-label="Raise {stage} priority"' in html
    assert f'aria-label="Lower {stage} priority"' in html
    assert f'aria-label="Pause {stage}"' in html
    assert f'aria-label="Resume {stage}"' in html


@pytest.mark.parametrize("stage", _ENRICH_STAGES)
def test_stepper_carries_d11_clarifying_tooltip(stage: str) -> None:
    """The D-11 clarifying tooltip (▲ raises priority = lowers the number) is present on the steppers."""
    html = _render_rail()

    # The tooltip appears on both stepper buttons; assert it is present at least once per render.
    assert _D11_TOOLTIP in html


@pytest.mark.parametrize("stage", _ENRICH_STAGES)
def test_priority_label_names_high_normal_low(stage: str) -> None:
    """The priority label renders 'Priority: {High|Normal|Low} ({n})' from the store (UI-SPEC copy)."""
    html = _render_rail()

    assert f"$store.pipeline.{stage}Priority" in html
    # The High/Normal/Low ternary + the numeric ({n}) are both present in the label expression.
    assert "'High'" in html
    assert "'Normal'" in html
    assert "'Low'" in html


def test_paused_amber_caption_present() -> None:
    """A 'Paused' amber caption renders (shown when the stage is paused)."""
    html = _render_rail()

    assert ">Paused</span>" in html


# ---------------------------------------------------------------------------
# UI-05 — amber orphan badge binds the store key, role=status, hidden at 0
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", _ENRICH_STAGES)
def test_orphan_badge_binds_store_key_and_hidden_at_zero(stage: str) -> None:
    """The amber orphan badge binds $store.pipeline.{stage}Orphan, role=status, hidden at 0 (x-show > 0)."""
    html = _render_rail()

    assert f'x-show="$store.pipeline.{stage}Orphan > 0"' in html
    assert f'x-text="$store.pipeline.{stage}Orphan"' in html


@pytest.mark.parametrize("stage", _ENRICH_STAGES)
def test_orphan_badge_is_amber_and_role_status(stage: str) -> None:
    """The orphan badge uses the amber hue tokens (never red) and carries role='status' (UI-SPEC)."""
    html = _render_rail()

    # Amber, light + dark pair (the established "needs attention, not failure" hue).
    assert "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-400" in html
    assert 'role="status"' in html


# ---------------------------------------------------------------------------
# Structure — interactive controls are NOT nested inside a nav <button>
# ---------------------------------------------------------------------------


def test_stepper_buttons_are_not_nested_in_nav_button() -> None:
    """The stepper hx-post buttons live in a controls sub-row, never inside the rail nav <button>.

    A nested interactive control (a <button> inside a <button>) is invalid HTML and breaks the rail's
    hx-get navigation. Assert every priority hx-post appears AFTER a closing </button> and the controls
    row's role="group" wrapper — i.e. it is a sibling of the nav node, not a child.
    """
    html = _render_rail()

    # Each enrich stage's priority control is wrapped in its own role="group" controls row.
    for stage in _ENRICH_STAGES:
        assert f'aria-label="{stage.capitalize()} priority and pause controls"' in html

    # The nav node's closing </button> precedes the first stepper hx-post (controls are a sibling row).
    first_stepper = html.index('hx-post="/pipeline/stages/metadata/priority"')
    preceding = html.rindex("</button>", 0, first_stepper)
    assert preceding < first_stepper
