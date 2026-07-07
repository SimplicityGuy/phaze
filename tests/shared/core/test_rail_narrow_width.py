"""CUT-04 narrow-width rail collapse contract — pure-filesystem structural guard.

Phase 62 (CUT-04, D-07/D-08) collapses the 280px DAG rail to a 64px icon-only strip
below 1024px using the pure-CSS Tailwind ``max-lg:`` breakpoint (no JS, no persistence),
and adds a per-stage inline-SVG glyph set so the collapsed strip has something to show.

This guard proves the collapse contract **in the template source** — it asserts on the
Tailwind class strings and inline-SVG markup in ``rail.html``, NOT on compiled CSS or a
rendered browser. That keeps it in the fast lane (no DB, no HTTP client, no CSS build,
no headless browser) while still locking the contract the way a browser would honour it:

* the aside gains ``max-lg:w-16`` alongside the existing ``w-[280px]`` (collapse width);
* every rail-node **label** span carries ``max-lg:sr-only`` and NEVER ``max-lg:hidden`` —
  the hard CUT-04 ↔ CUT-01 join: the collapsed strip must stay screen-reader-navigable
  (glyphs are ``aria-hidden``, so the sr-only label is the node's accessible name, D-08);
* numeric **count** spans (``x-text=...``) carry ``max-lg:hidden`` (visual-only data);
* at least 14 inline ``<svg aria-hidden="true">`` glyphs exist (one per navigable node —
  12 stage buttons + the 2 below-line links);
* every navigable node carries a native ``title`` tooltip; and
* the active / focus affordances (``aria-current="page"`` idiom + a ``focus-visible:``
  class) survive the CUT-04 edit — a regression guard against the additive rewrite.

Mirrors the filesystem-only idiom of ``test_dead_template_guard.py`` /
``test_base_html_sri.py``: read the template with ``read_text()`` and assert on
substrings / regex matches. No fixtures, no network.
"""

from __future__ import annotations

from pathlib import Path
import re


_RAIL_HTML = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates" / "shell" / "partials" / "rail.html"

# An opening <button ...> or <a ...> tag (attribute values never contain '>', so a
# non-greedy [^>]* is safe even across the multi-line tags rail.html uses).
_OPEN_TAG = re.compile(r"<(?P<el>button|a)\b(?P<attrs>[^>]*)>", re.DOTALL)

# A <span ...> opening tag with its class attribute captured.
_SPAN_TAG = re.compile(r"<span\b(?P<attrs>[^>]*)>", re.DOTALL)


def _rail_source() -> str:
    return _RAIL_HTML.read_text()


def _navigable_node_tags() -> list[str]:
    """Opening tags of every navigable rail node: the /s/ stage buttons (which carry
    ``hx-get=``) and the two below-the-line links (``href=``)."""
    tags: list[str] = []
    for m in _OPEN_TAG.finditer(_rail_source()):
        attrs = m.group("attrs")
        if "hx-get=" in attrs or "href=" in attrs:
            tags.append(attrs)
    return tags


def _label_span_attrs() -> list[str]:
    """Class attributes of the node *label* spans.

    Two shapes exist: stage/top-level labels carry ``flex-1 text-sm``; the two below-line
    links use a bare ``<span class="max-lg:sr-only">`` label. Both are labels (they hold the
    node's visible text). Count spans (``x-text``) and eyebrows are excluded — they are
    handled by their own assertions.
    """
    spans: list[str] = []
    for m in _SPAN_TAG.finditer(_rail_source()):
        attrs = m.group("attrs")
        if "x-text=" in attrs:  # numeric count span — not a label
            continue
        if "flex-1 text-sm" in attrs or "max-lg:sr-only" in attrs:
            spans.append(attrs)
    return spans


def test_rail_html_exists() -> None:
    """Sanity: the guard is not vacuously satisfied by a missing file."""
    assert _RAIL_HTML.is_file(), f"rail template not found at {_RAIL_HTML}"


def test_collapse_width() -> None:
    """The aside keeps its expanded 280px width AND gains the 64px collapsed width."""
    html = _rail_source()
    # Require class= so we skip the literal "<aside>" mentioned in the header comment.
    aside = re.search(r"<aside\b[^>]*\bclass=[^>]*>", html, re.DOTALL)
    assert aside is not None, "no <aside> landmark in rail.html"
    aside_tag = aside.group(0)
    assert "w-[280px]" in aside_tag, "expanded rail width w-[280px] missing from <aside>"
    assert "max-lg:w-16" in aside_tag, "collapsed rail width max-lg:w-16 missing from <aside>"


def test_labels_sr_only_not_hidden() -> None:
    """Every node label span collapses via max-lg:sr-only, NEVER max-lg:hidden.

    Hard CUT-04 ↔ CUT-01 contract (D-08): the collapsed strip must remain
    screen-reader-navigable, so labels stay in the a11y tree (sr-only) rather than being
    removed from it (display:none via max-lg:hidden).
    """
    labels = _label_span_attrs()
    assert len(labels) >= 14, f"expected >=14 node label spans, found {len(labels)}"
    for attrs in labels:
        assert "max-lg:sr-only" in attrs, f"label span missing max-lg:sr-only: <span{attrs}>"
        assert "max-lg:hidden" not in attrs, f"label span uses max-lg:hidden (strips it from the a11y tree) — must be max-lg:sr-only: <span{attrs}>"


def test_counts_hidden() -> None:
    """Numeric count spans (x-text=...) drop out of the collapsed view via max-lg:hidden."""
    count_spans = [m.group("attrs") for m in _SPAN_TAG.finditer(_rail_source()) if "x-text=" in m.group("attrs")]
    assert count_spans, "no x-text count spans found in rail.html"
    for attrs in count_spans:
        assert "max-lg:hidden" in attrs, f"count span missing max-lg:hidden: <span{attrs}>"


def test_glyphs_present() -> None:
    """At least 14 inline-SVG glyphs, each aria-hidden (one per navigable node)."""
    html = _rail_source()
    glyphs = re.findall(r"<svg\b[^>]*aria-hidden=\"true\"[^>]*>", html, re.DOTALL)
    assert len(glyphs) >= 14, f"expected >=14 aria-hidden inline-SVG glyphs, found {len(glyphs)}"
    # Every glyph follows the wrapper contract (24x24 viewBox, currentColor, w-5 h-5).
    for glyph in glyphs:
        assert 'viewBox="0 0 24 24"' in glyph, f"glyph not using 24x24 viewBox: {glyph}"
        assert 'stroke="currentColor"' in glyph, f"glyph not using currentColor: {glyph}"
        assert "w-5 h-5" in glyph, f"glyph not sized w-5 h-5: {glyph}"


def test_titles_present() -> None:
    """Every navigable node carries a native title tooltip (collapsed-state name for
    sighted pointer/keyboard users)."""
    tags = _navigable_node_tags()
    assert len(tags) >= 14, f"expected >=14 navigable nodes, found {len(tags)}"
    for attrs in tags:
        assert "title=" in attrs, f"navigable node missing title tooltip: <...{attrs}>"


def test_focus_and_current_preserved() -> None:
    """Regression guard: the CUT-04 edit preserved the focus-visible ring on every
    navigable node and the aria-current='page' idiom on every /s/ stage button."""
    tags = _navigable_node_tags()
    for attrs in tags:
        assert "focus-visible:" in attrs, f"navigable node lost its focus-visible ring: <...{attrs}>"
    stage_buttons = [attrs for attrs in tags if "data-rail-stage=" in attrs]
    assert len(stage_buttons) >= 12, f"expected >=12 /s/ stage buttons, found {len(stage_buttons)}"
    for attrs in stage_buttons:
        assert 'aria-current="page"' in attrs, f"stage button lost the aria-current='page' idiom: <...{attrs}>"
