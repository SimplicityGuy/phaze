"""Narrow-width navigation contract — pure-filesystem structural guard.

**This contract was inverted by phaze-tzy6s.13.** Phase 62 (CUT-04) collapsed the 280px rail to a
64px icon-only strip below 1024px, keeping labels only as ``max-lg:sr-only`` text with ``title=`` as
the sighted fallback. .13 removed that collapse outright, because it fails on exactly the devices it
targets: ``title`` never appears on touch (there is no hover), it is unreliable for screen readers,
and it is unreachable by keyboard — so on phones the sixteen destinations were labelled only for
assistive tech, and sixteen destinations across four groups is more than an icon strip carries
legibly anyway.

Below ``lg`` the rail is now an **off-canvas drawer** opened from the header, with every label as
visible text. At ``lg`` and up it is the static expanded rail the epic's desktop constraint requires.

The assertions below therefore prove the OPPOSITE of what this module originally proved, and several
are written as negatives on purpose: they exist so a future change cannot quietly reintroduce an
icon-only rail and still pass. The glyph, title, focus and aria-current guards are unchanged — those
survived .13 intact.

Same filesystem-only idiom as before (``test_dead_template_guard.py`` / ``test_base_html_sri.py``):
render the template source and assert on class strings. No DB, no HTTP client, no browser.
"""

from __future__ import annotations

from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_RAIL_HTML = _TEMPLATES / "shell" / "partials" / "rail.html"
_HEADER_HTML = _TEMPLATES / "shell" / "partials" / "header.html"

# An opening <button ...> or <a ...> tag (attribute values never contain '>', so a
# non-greedy [^>]* is safe even across the multi-line tags rail.html uses).
_OPEN_TAG = re.compile(r"<(?P<el>button|a)\b(?P<attrs>[^>]*)>", re.DOTALL)

# A <span ...> opening tag with its class attribute captured.
_SPAN_TAG = re.compile(r"<span\b(?P<attrs>[^>]*)>", re.DOTALL)


def _render(name: str) -> str:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    return env.get_template(name).render(stage="summary")


def _rail_source() -> str:
    return _render("shell/partials/rail.html")


def _navigable_node_tags() -> list[str]:
    """Opening tags of every navigable rail node (the /s/ stage links carrying ``hx-get=``)."""
    return [m.group("attrs") for m in _OPEN_TAG.finditer(_rail_source()) if "hx-get=" in m.group("attrs")]


def _label_span_attrs() -> list[str]:
    """Class attributes of the node *label* spans (excluding numeric count spans)."""
    spans: list[str] = []
    for m in _SPAN_TAG.finditer(_rail_source()):
        attrs = m.group("attrs")
        if "x-text=" in attrs:  # numeric count span — not a label
            continue
        if "flex-1 text-sm" in attrs:
            spans.append(attrs)
    return spans


def test_rail_html_exists() -> None:
    assert _RAIL_HTML.is_file(), f"rail template not found at {_RAIL_HTML}"


def test_the_icon_only_collapse_is_gone() -> None:
    """No width below ``lg`` may render the rail as a 64px icon strip.

    A negative assertion by design. The failure mode this prevents is a well-meaning revert: the
    collapse is one Tailwind class, so it is cheap to reintroduce and invisible in review.
    """
    source = _rail_source()
    assert "max-lg:w-16" not in source, "the 64px icon-only rail is back — .13 replaced it with a drawer"
    assert "max-lg:justify-center" not in source, "icon-centering means the labels are gone again"


def test_labels_are_visible_text_at_every_width() -> None:
    """Every destination label is real visible text, never sr-only and never display:none."""
    labels = _label_span_attrs()
    assert len(labels) >= 14, f"expected >=14 node label spans, found {len(labels)}"
    for attrs in labels:
        assert "max-lg:sr-only" not in attrs, f"label hidden from sighted users below lg: <span{attrs}>"
        assert "max-lg:hidden" not in attrs, f"label removed from the a11y tree below lg: <span{attrs}>"
        assert "sr-only" not in attrs, f"label is screen-reader-only at all widths: <span{attrs}>"


def test_counts_are_not_hidden_below_lg() -> None:
    """The live per-stage counts survive into the drawer.

    They were ``max-lg:hidden`` because a 64px strip had nowhere to put them. The drawer is 20rem, so
    the reason is gone — and the counts are the rail's only at-a-glance progress signal.
    """
    source = _rail_source()
    count_spans = [m.group("attrs") for m in _SPAN_TAG.finditer(source) if "x-text=" in m.group("attrs")]
    assert count_spans, "no x-text count spans found in rail.html"
    for attrs in count_spans:
        assert "max-lg:hidden" not in attrs, f"count span still hidden below lg: <span{attrs}>"


def test_narrow_width_navigation_is_a_drawer() -> None:
    """Below ``lg`` the aside is an off-canvas drawer with a backdrop and a keyboard escape."""
    source = _rail_source()
    assert "max-lg:fixed" in source and "max-lg:inset-y-0" in source, "the narrow-width aside is not off-canvas"
    assert "@rail:open.window" in source, "no open handler for the header's drawer trigger"
    assert "@keydown.escape.window" in source, "the drawer cannot be dismissed with Escape"
    assert "x-trap.noscroll" in source, "the open drawer does not trap focus"
    assert 'aria-label="Close navigation"' in source, "the drawer has no close control"


def test_closed_drawer_is_removed_from_the_tab_order() -> None:
    """Closed means ``invisible``, not merely translated off-screen.

    A transformed-but-visible element keeps its tab stops, so a keyboard user on a phone would tab
    through sixteen off-screen destinations before reaching the workspace. This is the assertion that
    catches a translate-only implementation, which looks correct in a screenshot and is not.
    """
    source = _rail_source()
    assert "max-lg:invisible" in source, "the closed drawer is still focusable — needs visibility:hidden"
    assert "lg:visible" in source, "the desktop rail must stay visible regardless of drawer state"


def test_navigation_is_mounted_exactly_once() -> None:
    """One <aside> and one <nav>, not a desktop copy plus a drawer copy.

    Two copies duplicate every heading id, give the document two identically-named navigation
    landmarks, and let a destination exist on one surface but not the other.
    """
    source = _rail_source()
    assert source.count("<aside") == 1, "navigation is mounted more than once"
    assert source.count("<nav ") == 1, "more than one nav landmark in the rail"
    assert source.count('id="nav-overview"') == 1, "duplicate heading id — the nav is rendered twice"


def test_header_carries_the_drawer_trigger_only_below_lg() -> None:
    """Exactly one navigation affordance is reachable at any width."""
    header = _render("shell/partials/header.html")
    trigger = re.search(r'<button\b[^>]*id="rail-drawer-trigger"[^>]*>', header, re.DOTALL)
    assert trigger is not None, "header is missing the drawer trigger"
    attrs = trigger.group(0)
    assert "lg:hidden" in attrs, "the drawer trigger must not appear alongside the desktop rail"
    assert "rail:open" in attrs, "the trigger does not open the drawer"
    assert 'aria-label="Open navigation"' in attrs, "the icon-only trigger has no accessible name"
    assert "h-11 w-11" in attrs, "the trigger is below the 44px minimum touch target"


def test_glyphs_present() -> None:
    glyphs = re.findall(r'<svg\b[^>]*aria-hidden="true"[^>]*>', _rail_source(), re.DOTALL)
    assert len(glyphs) >= 14, f"expected >=14 aria-hidden inline-SVG glyphs, found {len(glyphs)}"
    for glyph in glyphs:
        assert 'viewBox="0 0 24 24"' in glyph, f"glyph not using 24x24 viewBox: {glyph}"
        assert 'stroke="currentColor"' in glyph, f"glyph not using currentColor: {glyph}"
        assert "w-5 h-5" in glyph, f"glyph not sized w-5 h-5: {glyph}"


def test_titles_present_but_never_the_only_label() -> None:
    """``title`` is retained as a supplement for truncation — it is not the label."""
    tags = _navigable_node_tags()
    assert len(tags) >= 14, f"expected >=14 navigable nodes, found {len(tags)}"
    for attrs in tags:
        assert "title=" in attrs, f"navigable node missing title tooltip: <...{attrs}>"
    # The visible labels asserted in test_labels_are_visible_text_at_every_width are what make the
    # titles a supplement rather than the sole carrier; both tests must hold together.
    assert len(_label_span_attrs()) >= 14


def test_focus_and_current_preserved() -> None:
    for attrs in _navigable_node_tags():
        assert "focus-visible:" in attrs, f"navigable node lost its focus-visible ring: <...{attrs}>"
    stage_links = re.findall(r'href="/s/[a-z]+"', _rail_source())
    assert len(stage_links) >= 14, f"expected >=14 /s/ stage links, found {len(stage_links)}"
    assert 'aria-current="page"' in _rail_source(), "the active-node idiom was lost"


def test_animation_respects_reduced_motion() -> None:
    """The drawer slide is gated behind ``motion-safe:`` — vestibular-safe by default."""
    source = _rail_source()
    assert "motion-safe:max-lg:transition-transform" in source, "the drawer transition ignores prefers-reduced-motion"
