"""CUT-01 accessibility structural guard (Phase 62 — audit-and-close-gaps, NOT a rebuild).

This is the pure-filesystem proof that the shell's WCAG-2.1-AA-relevant ARIA is
present and cannot silently regress. It mirrors the repo's established guard-test
idiom (``test_dead_template_guard.py`` for path constants, ``test_base_html_sri.py``
for regex-over-``read_text()`` assertions): it reads template SOURCE and asserts
plain substrings / small regexes, so it needs **no browser, no axe/pa11y, no Node,
and no new dependency** (decision D-01). It touches no ``client``/session/DB fixture,
so ``conftest.py`` does not auto-mark it ``integration`` — it runs in the fast lane
(``pytest -m 'not integration'``).

CUT-01 is **audit-and-close-gaps, not an ARIA rebuild** (decision D-01a). Phase 57/61
already built the hard ARIA — skip link + ``#stage-workspace`` target, DAG-rail
``nav``/``aside`` landmarks with ``aria-current="page"`` and focus-visible rings, the
⌘K palette as ``role=combobox``/``listbox``/``option`` with ``aria-expanded`` +
``:aria-activedescendant``, and the record slide-in as ``role=dialog aria-modal`` with
an ``x-trap`` focus-trap. This guard asserts that whole baseline is still in place.

The ONE real gap this phase closes is that the ⌘K combobox ``<input>`` had no
accessible name (a placeholder is NOT an accessible name per the WAI-ARIA APG) — the
fix is a single ``aria-label="Search files and commands"`` attribute. It also removes
the dead empty right detail-pane ``<aside aria-label="Detail pane">`` (superseded by
the Phase 61 record slide-in, its removal deferred to Phase 62). The two assertions
covering those gaps (``test_cmdk_combobox_has_accessible_name`` and
``test_shell_has_no_dead_detail_pane_aside``) are the RED half of the TDD cycle — they
go green once the source fixes land.

Assertions cover class STRINGS in the HTML source, never compiled CSS, so the guard
passes without a Tailwind build step.
"""

from __future__ import annotations

from pathlib import Path
import re


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_SHELL = _TEMPLATES / "shell" / "shell.html"
_RAIL = _TEMPLATES / "shell" / "partials" / "rail.html"
_CMDK = _TEMPLATES / "shell" / "partials" / "cmdk_modal.html"
_RECORD = _TEMPLATES / "shell" / "partials" / "record_host.html"

# The ⌘K combobox input tag: from `<input x-ref="input"` to its closing `>`. The tag
# has no `>` inside any attribute value, so a non-greedy `[^>]*` cleanly bounds it.
_CMDK_INPUT = re.compile(r"<input\b[^>]*\bx-ref=\"input\"[^>]*>", re.DOTALL)

# Every rail node carries a `data-rail-stage="<id>"` hook (a literal id or the
# `{{ item.id }}` loop var). Splitting the (comment-stripped) source on the attribute
# name yields one chunk per navigable node (chunk[0] is the pre-first-node preamble,
# discarded). Jinja comments are stripped first — one documents the hook by name and
# would otherwise create a spurious node chunk.
_RAIL_NODE_SPLIT = "data-rail-stage"
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


# --- Skip link (shell.html) -------------------------------------------------------


def test_skip_link_is_first_focusable_in_body() -> None:
    """The sr-only skip link targets #stage-workspace and precedes any other focusable."""
    html = _SHELL.read_text()
    body_at = html.find("<body")
    assert body_at != -1, "shell.html has no <body>"
    body = html[body_at:]

    skip_at = body.find('href="#stage-workspace"')
    assert skip_at != -1, "shell.html is missing the skip link to #stage-workspace"

    # The skip link must be the FIRST focusable element in <body>: no other href=,
    # <button, or <input appears before it.
    for token in ('href="', "<button", "<input"):
        other = body.find(token)
        if token == 'href="':
            # The first href= in the body IS the skip link — assert that.
            assert other == body.find('href="#stage-workspace"'), "the skip link must be the first href= in <body>"
        else:
            assert other == -1 or other > skip_at, f"a {token!r} element appears before the skip link in <body>"


def test_skip_link_target_id_exists() -> None:
    """#stage-workspace (the skip-link + swap target) exists in the shell."""
    html = _SHELL.read_text()
    assert 'id="stage-workspace"' in html, "shell.html is missing the id=stage-workspace swap/skip target"


# --- DAG rail landmarks + per-node state (rail.html) -------------------------------


def test_rail_has_landmark_labels() -> None:
    """The rail exposes an <aside> and a <nav>, each with a non-empty aria-label."""
    html = _RAIL.read_text()
    aside = re.search(r"<aside\b[^>]*\baria-label=\"([^\"]+)\"", html, re.DOTALL)
    nav = re.search(r"<nav\b[^>]*\baria-label=\"([^\"]+)\"", html, re.DOTALL)
    assert aside and aside.group(1).strip(), "rail.html <aside> needs a non-empty aria-label"
    assert nav and nav.group(1).strip(), "rail.html <nav> needs a non-empty aria-label"


def test_rail_nodes_carry_aria_current_and_focus_visible() -> None:
    """Every navigable rail node uses the aria-current="page" idiom and a focus-visible class."""
    source = _JINJA_COMMENT.sub("", _RAIL.read_text())
    chunks = source.split(_RAIL_NODE_SPLIT)
    node_chunks = chunks[1:]  # chunk[0] is the pre-first-node preamble
    assert node_chunks, "rail.html has no data-rail-stage nodes"
    for i, chunk in enumerate(node_chunks):
        assert 'aria-current="page"' in chunk, f"rail node #{i} is missing the aria-current=page idiom"
        assert "focus-visible:" in chunk, f"rail node #{i} is missing a focus-visible ring class"


# --- ⌘K command palette (cmdk_modal.html) -----------------------------------------


def test_cmdk_combobox_semantics_present() -> None:
    """The ⌘K input carries combobox + controls + expanded + activedescendant semantics."""
    html = _CMDK.read_text()
    match = _CMDK_INPUT.search(html)
    assert match, 'cmdk_modal.html has no <input x-ref="input"> combobox'
    tag = match.group(0)
    assert 'role="combobox"' in tag, "⌘K input is missing role=combobox"
    assert 'aria-controls="cmdk-results"' in tag, "⌘K input is missing aria-controls"
    assert 'aria-expanded="true"' in tag, "⌘K input is missing aria-expanded"
    assert ":aria-activedescendant=" in tag, "⌘K input is missing :aria-activedescendant"


def test_cmdk_combobox_has_accessible_name() -> None:
    """RED-until-fixed: the ⌘K combobox input needs an aria-label (placeholder is not a name)."""
    html = _CMDK.read_text()
    match = _CMDK_INPUT.search(html)
    assert match, 'cmdk_modal.html has no <input x-ref="input"> combobox'
    tag = match.group(0)
    assert 'aria-label="Search files and commands"' in tag, (
        'the ⌘K combobox input needs aria-label="Search files and commands" — a placeholder is not an accessible name (WAI-ARIA APG)'
    )


def test_cmdk_listbox_and_dialog_present() -> None:
    """The ⌘K results are a labelled listbox inside a labelled modal dialog."""
    html = _CMDK.read_text()
    assert 'role="listbox"' in html, "cmdk_modal.html is missing role=listbox"
    assert 'aria-label="Search and command results"' in html, "cmdk_modal.html listbox is missing its aria-label"
    assert 'role="dialog"' in html, "cmdk_modal.html is missing role=dialog"
    assert 'aria-modal="true"' in html, "cmdk_modal.html dialog is missing aria-modal=true"
    assert 'aria-label="Command palette"' in html, "cmdk_modal.html dialog is missing its aria-label"


# --- Record slide-in (record_host.html) -------------------------------------------


def test_record_slide_in_is_a_trapped_modal_dialog() -> None:
    """The record slide-in panel is a labelled modal dialog with an x-trap focus-trap."""
    html = _RECORD.read_text()
    panel = re.search(r"<div\b[^>]*\bx-ref=\"panel\"[^>]*>", html, re.DOTALL)
    assert panel, 'record_host.html has no <div x-ref="panel"> dialog'
    tag = panel.group(0)
    assert 'role="dialog"' in tag, "record panel is missing role=dialog"
    assert 'aria-modal="true"' in tag, "record panel is missing aria-modal=true"
    assert re.search(r"\baria-label=\"[^\"]+\"", tag), "record panel is missing an aria-label"
    assert "x-trap" in tag, "record panel is missing the x-trap focus-trap directive"


# --- Dead detail-pane removal (shell.html) ----------------------------------------


def test_shell_has_no_dead_detail_pane_aside() -> None:
    """RED-until-fixed: the dead empty right detail-pane <aside> must be gone from the shell."""
    html = _SHELL.read_text()
    assert 'aria-label="Detail pane"' not in html, (
        'the dead empty right detail-pane <aside aria-label="Detail pane"> was superseded by the Phase 61 record slide-in — remove it (deferred from Phase 61)'
    )


# --- Phase 88 detail-pane after-swap scope (browser-caught regression) -------------

# `onLoaded` / `hide` are Alpine METHODS on the `<section x-data>` in _detail_pane.html.
# hx-on::after-swap evaluates in the GLOBAL scope, so a bare `onLoaded()` there is a
# ReferenceError — `open` never flips true, the ✕/Esc dismiss and the body's self-removing
# own-tick all silently break. This was invisible to markup/httpx tests (the string
# `onLoaded()` was present either way) and to the source-reading verifier; only a live
# browser (Phase 88 UAT) surfaced it. The fix reaches the component scope via
# `Alpine.$data(this).onLoaded()`. Guard: the after-swap MUST go through Alpine.$data, and
# MUST NOT call a bare `onLoaded()` in the global hx-on scope.
_DETAIL_PANE = _TEMPLATES / "pipeline" / "partials" / "_detail_pane.html"
# The `hx-on::after-swap="..."` attribute value (no `"` inside the expression, so `[^"]*` bounds it).
_AFTER_SWAP = re.compile(r'hx-on::after-swap="([^"]*)"')


def test_detail_pane_after_swap_reaches_alpine_scope() -> None:
    """The #detail-pane after-swap must call onLoaded() through Alpine.$data, never bare (global-scope ReferenceError)."""
    html = _strip_comments(_DETAIL_PANE.read_text())
    m = _AFTER_SWAP.search(html)
    assert m, "expected an hx-on::after-swap handler on the #detail-pane swap target"
    expr = m.group(1)
    # Must reach the Alpine component scope explicitly.
    assert "Alpine.$data(this).onLoaded()" in expr, (
        "hx-on::after-swap must invoke Alpine.$data(this).onLoaded() — hx-on evaluates in the GLOBAL "
        f"scope where the Alpine method onLoaded is undefined. Got: {expr!r}"
    )
    # Must NOT call a bare onLoaded() (the broken global-scope form). Remove the reachable
    # `.onLoaded()` occurrences, then assert no stray `onLoaded(` identifier remains.
    residual = expr.replace("Alpine.$data(this).onLoaded()", "")
    assert "onLoaded(" not in residual, f"bare global-scope onLoaded() call is a ReferenceError: {expr!r}"


def _strip_comments(text: str) -> str:
    """Blank out ``{# ... #}`` Jinja comment regions before scanning (prose may mention onLoaded)."""
    return _JINJA_COMMENT.sub("", text)


# --- Phase 94 detail-pane full dismiss (browser-caught regression) ------------------

# CONSOLE-03: clicking ✕ only removed the ✕ icon. Root cause chain: the trigger's
# hx-swap="innerHTML" DESTROYS the resting empty-state div (it lived INSIDE the
# #detail-pane swap target), and hide() only flipped `open=false` — the swapped wave-2
# body is not gated on `open`, so it stayed fully visible with no empty state to fall
# back to. A late in-flight own-tick response could also re-fire onLoaded() after a
# dismiss and resurrect the pane. Three source guards keep the trap from returning;
# browser UAT is the authoritative catch.


def test_detail_pane_empty_state_lives_outside_swap_target() -> None:
    """The resting empty state must be a SIBLING of #detail-pane, never inside it (innerHTML swaps destroy it)."""
    html = _strip_comments(_DETAIL_PANE.read_text())
    start = html.find('id="detail-pane"')
    assert start != -1, "expected the #detail-pane swap target"
    # The fixed swap target is an EMPTY element: nothing but whitespace before its closing tag.
    inner = html[html.find(">", start) + 1 : html.find("</div>", start)]
    assert "_empty_head" not in inner and 'x-show="!open"' not in inner, (
        "the resting empty state must live OUTSIDE the #detail-pane swap target — an "
        "hx-swap='innerHTML' load destroys everything inside the target, so an inside "
        "empty state can never come back after dismiss"
    )
    # And it must still exist somewhere in the shell, gated on !open.
    assert "_empty_head" in html, "the resting empty state must still be rendered by the shell"


def test_detail_pane_dismiss_clears_swapped_body() -> None:
    """hide() must clear the swapped body out of #detail-pane (this also removes the body's own-tick poller)."""
    html = _strip_comments(_DETAIL_PANE.read_text())
    m = re.search(r'x-data="([^"]*)"', html, re.DOTALL)
    assert m, "expected the shell <section x-data> component"
    component = m.group(1)
    assert re.search(r"getElementById\('detail-pane'\)", component), (
        "the dismiss path must reach the #detail-pane swap target to clear the swapped body"
    )
    assert re.search(r"innerHTML\s*=\s*''", component), (
        "dismiss must wipe #detail-pane's innerHTML — flipping `open` alone leaves the "
        "swapped wave-2 body (not gated on `open`) fully visible, the ✕-only-disappears trap"
    )


def test_detail_pane_late_swap_cannot_resurrect_dismissed_pane() -> None:
    """onLoaded() must guard on the ?param still being present — a late own-tick swap after dismiss must not re-open."""
    html = _strip_comments(_DETAIL_PANE.read_text())
    m = re.search(r'x-data="([^"]*)"', html, re.DOTALL)
    assert m, "expected the shell <section x-data> component"
    component = m.group(1)
    on_loaded = component[component.find("onLoaded()") :]
    guard = re.search(r"if\s*\(!id\)", on_loaded)
    assert guard, (
        "onLoaded() must early-return (and wipe) when the ?param is gone — hide() clears the "
        "param, so a late in-flight tick response landing after dismiss must not resurrect the pane"
    )
    assert guard.start() < on_loaded.find("open = true"), "the missing-param guard must run BEFORE open flips true"


# --- Phase 93 rail Alpine root (browser-caught regression) ---------------------------


def test_rail_root_carries_alpine_x_data() -> None:
    """The rail <aside> must carry x-data — Alpine only walks x-data-rooted subtrees.

    Without it every x-text numeral, x-show orphan badge, and pause/priority binding in the
    rail is silently inert: the badges forever render their server-side "0" defaults no matter
    what $store.pipeline holds (CONSOLE-02: the Analyze badge read 0 while 2,183 analyze jobs
    were in flight). Invisible to markup/httpx tests — only a live browser surfaced it.
    """
    html = _strip_comments(_RAIL.read_text())
    m = re.search(r"<aside\b[^>]*>", html)
    assert m, "expected the rail <aside> root"
    assert re.search(r"\bx-data\b", m.group(0)), (
        "the rail <aside> must carry a bare x-data so Alpine binds the rail subtree — without "
        "it every store-bound numeral/badge in the rail is inert and renders 0 forever"
    )
