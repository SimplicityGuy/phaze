"""phaze-2u8v.6: opening a compute-lane detail must not reflow the rest of the page.

Before this bead, the shared #detail-pane sat in a right-side GRID COLUMN
(analyze_workspace.html's `lg:grid lg:grid-cols-3` row: lane grid col-span-2, pane col-span-1). The
column's height fed into that row's total height, so opening the pane (populating its swap target)
grew the row and pushed every section below it (the cloud-state cards, the per-file table) down the
page -- and on < lg the pane stacked BELOW the lane grid entirely, so opening/closing it visibly
moved everything beneath it regardless of viewport width.

The fix takes the pane out of document flow entirely: `position: fixed`, sliding in from the right
edge via a `translate-x-full` <-> `translate-x-0` transform rather than occupying a column. A fixed
element can never contribute to a sibling's layout, so this holds at every viewport by construction --
there is no "narrow" vs "wide" branch left to diverge.
"""

from __future__ import annotations

from pathlib import Path
import re


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_ANALYZE_WORKSPACE = _TEMPLATES / "pipeline" / "partials" / "analyze_workspace.html"
_DETAIL_PANE = _TEMPLATES / "pipeline" / "partials" / "_detail_pane.html"

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _strip_comments(text: str) -> str:
    return _JINJA_COMMENT.sub("", text)


def test_analyze_workspace_no_longer_reserves_a_grid_column_for_the_pane() -> None:
    """The lane grid + pane must not share an `lg:grid-cols-3` row any more.

    RED before phaze-2u8v.6: `<div class="lg:grid lg:grid-cols-3 lg:items-start lg:gap-4">` wrapped
    both `_analyze_lanes.html` (col-span-2) and `_detail_pane.html` (col-span-1) -- the pane's height
    fed into that row and pushed the content below it down whenever the pane opened.
    """
    html = _strip_comments(_ANALYZE_WORKSPACE.read_text())
    assert "lg:grid-cols-3" not in html
    assert "lg:col-span-1" not in html
    assert "lg:col-span-2" not in html
    # Both partials must still be included -- just no longer column-mates.
    assert '{% include "pipeline/partials/_analyze_lanes.html" %}' in html
    assert '{% include "pipeline/partials/_detail_pane.html" %}' in html


def test_detail_pane_is_a_fixed_non_reflowing_overlay() -> None:
    """The pane's outer <section> is `position: fixed`, out of document flow, sliding in via transform.

    The section's opening tag cannot be isolated with a bare `[^>]*` scan: its `x-data` carries JS
    arrow functions (`() => { ... }`), and `=>` contains a literal `>` that would truncate the match
    long before the real end of the tag. The section's OWN `class="..."` attribute is instead the
    FIRST such attribute in the file (nothing precedes it), so it is found directly.
    """
    html = _strip_comments(_DETAIL_PANE.read_text())
    # `(?<!:)` excludes Alpine's `:class="..."` dynamic binding (also matched by a bare `\bclass="`),
    # which is checked separately below.
    class_match = re.search(r'(?<!:)\bclass="([^"]*)"', html)
    assert class_match, "expected the pane's class attribute"
    classes = class_match.group(1)
    assert "fixed" in classes.split(), "the pane must be position: fixed so it cannot contribute to sibling layout"
    assert "inset-y-0" in classes and "right-0" in classes, "expected a right-edge slide-in overlay"

    dynamic_match = re.search(r':class="([^"]*)"', html)
    assert dynamic_match, "expected the pane's :class binding"
    binding = dynamic_match.group(1)
    assert "translate-x-full" in binding and "translate-x-0" in binding, "expected an open/closed translate-x toggle driving the slide-in"


def test_detail_pane_degraded_flag_is_gone_not_a_dead_contract() -> None:
    """phaze-d80hf: `degraded` must not be declared/rendered again unless something can set it true.

    Before this bead, `_detail_pane.html` declared `degraded: false` and rendered a caption gated
    on it, with a comment claiming "the wave-2 body flips `degraded` on a per-section fallback" --
    but no code anywhere ever set it true (the per-section reads it would gate on treat a real
    degrade and a genuinely-empty result as the SAME value by deliberate, documented design; see
    ``get_lane_recent_completions`` / ``get_lane_queue_depths``). A flag nothing can ever truthfully
    set is the bug, not a UI contract -- it was removed rather than wired to a lie. `refreshError`
    (the sibling flag) stays -- see ``test_lane_detail.py``'s own-tick-failure assertions for where
    it is now genuinely set.
    """
    html = _strip_comments(_DETAIL_PANE.read_text())
    assert "degraded" not in html, "the dead flag/caption must be gone from the RENDERED template (comments are stripped above)"
    assert "refreshError" in html, "refreshError must still be declared/rendered -- only degraded was dead"


def test_detail_pane_never_uses_grid_or_col_span_classes() -> None:
    """No stray grid-column class should reappear on the pane shell itself."""
    html = _strip_comments(_DETAIL_PANE.read_text())
    assert "col-span" not in html
