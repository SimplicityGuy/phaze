"""Narrow-width navigation contract — pure-filesystem structural guard.

**This contract was inverted by phaze-tzy6s.13.** Phase 62 (CUT-04) collapsed the 280px rail to a
64px icon-only strip below 1024px, keeping labels only as ``max-lg:sr-only`` text with ``title=`` as
the sighted fallback. .13 removed that collapse outright, because it fails on exactly the devices it
targets: ``title`` never appears on touch (there is no hover), it is unreliable for screen readers,
and it is unreachable by keyboard — so on phones the fourteen destinations were labelled only for
assistive tech, and fourteen destinations across four groups is more than an icon strip carries
legibly anyway. (Fourteen, not the sixteen .13's own ADR recorded: phaze-tzy6s.11 had already
consolidated the three Rename / Path, Tag write and Move files nodes into one Changes Review
destination before .13 landed — corrected here and in ADR-0009 by phaze-tzy6s.17.)

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


# ---------------------------------------------------------------------------
# phaze-tzy6s.17 (CR-13-1): the drawer's ARIA contract.
#
# .13 added a disclosure control and a focus-trapped overlay, and neither announced what it
# was. The trigger carried an accessible NAME (aria-label) but no STATE, and the <aside>
# carried x-trap + a backdrop without role="dialog"/aria-modal -- so below `lg` a screen
# reader described a trap the user could not be told they were in. The guard above only
# checked the `lg:hidden` breakpoint, which is why both slipped through.
# ---------------------------------------------------------------------------


def test_the_drawer_trigger_announces_its_state_and_what_it_controls() -> None:
    """WCAG 4.1.2: a disclosure control owes open/closed state, not just a name."""
    header = _render("shell/partials/header.html")
    trigger = re.search(r'<button\b[^>]*id="rail-drawer-trigger"[^>]*>', header, re.DOTALL)
    assert trigger is not None, "header is missing the drawer trigger"
    attrs = trigger.group(0)

    assert 'aria-expanded="false"' in attrs, "the server HTML must state the closed state before Alpine boots"
    assert ':aria-expanded="open"' in attrs, "aria-expanded must track the drawer, not stay frozen at false"
    assert 'aria-controls="rail-drawer"' in attrs, "the trigger must name the element it discloses"

    rail = _rail_source()
    assert 'id="rail-drawer"' in rail, "aria-controls points at an id the rail does not render"


def test_the_trigger_takes_its_state_from_the_rail_not_from_its_own_clicks() -> None:
    """Escape, the backdrop, the close button and an HTMX navigation all close the drawer.

    None of them involve the trigger, so a trigger that toggled its own boolean would sit at
    aria-expanded="true" over a closed drawer -- worse than the missing attribute, because it is
    confidently wrong rather than absent.
    """
    attrs = re.search(r'<button\b[^>]*id="rail-drawer-trigger"[^>]*>', _render("shell/partials/header.html"), re.DOTALL)
    assert attrs is not None
    assert "@rail:state.window" in attrs.group(0), "the trigger does not listen for the drawer's published state"

    rail = _rail_source()
    assert "x-effect=" in rail and "rail:state" in rail, "the rail does not publish navOpen outward"


def test_the_drawer_is_a_dialog_below_lg_and_a_landmark_above_it() -> None:
    """x-trap + backdrop must be paired with role="dialog"/aria-modal -- and only where it traps.

    The pairing is already an invariant elsewhere in the tree (record_host.html,
    _force_skip_dialog.html), and _detail_pane.html documents the converse for a non-modal pane.
    Both attributes are BOUND, not static, so they disappear at `lg`+ where the rail is an ordinary
    navigation landmark and traps nothing -- a permanent role="dialog" would be the mirror-image bug.
    """
    rail = _rail_source()
    aside = re.search(r"<aside\b[^>]*>", rail, re.DOTALL)
    assert aside is not None, "the rail no longer renders an <aside>"
    attrs = aside.group(0)

    assert 'x-trap.noscroll="navOpen"' in attrs, "the drawer is no longer focus-trapped"
    assert ":role=" in attrs and "'dialog'" in attrs, "a focus-trapped overlay must announce itself as a dialog"
    assert ":aria-modal=" in attrs, "a modal dialog must set aria-modal"
    assert 'role="dialog"' not in attrs, "role must be BOUND so it vanishes at lg+, where the rail is not modal"
    assert 'aria-modal="true"' not in attrs, "aria-modal must be bound for the same reason"


def test_the_drawer_role_breakpoint_matches_tailwinds_lg() -> None:
    """The JS media query is a hand-copy of Tailwind's `lg` and must not drift from it.

    A role cannot be switched by a CSS class, so the breakpoint has to exist twice: once as
    Tailwind's `lg:`/`max-lg:` utilities and once as a literal pixel value in the matchMedia call.
    If they disagree there is a band of widths where the rail LOOKS like a drawer and is announced
    as a landmark, or vice versa -- a defect no screenshot and no markup test would show. Tailwind's
    default `lg` is 1024px and this project defines no custom screens, so the complement of
    `min-width: 1024px` is `max-width: 1023.98px`.
    """
    rail = _rail_source()
    assert "(max-width: 1023.98px)" in rail, "the drawer's media query no longer complements Tailwind's lg (1024px)"
    assert "max-lg:fixed" in rail, "the drawer's own layout is no longer keyed to max-lg"
