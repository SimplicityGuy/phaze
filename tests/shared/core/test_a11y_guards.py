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


# --- phaze-jng72: the ⌘K command live region must outlive a search swap -------------

# The live region rendered INSIDE palette_results.html, i.e. inside #cmdk-results, which is the
# debounced search's hx-swap="innerHTML" target. That is the same trap as CONSOLE-03 below
# (a fixed element parked inside a swap target), plus an ARIA one: a listbox may own only
# option/group children, so a role="status" child made #cmdk-results structurally invalid and axe
# rated it critical. The behavioural half is the one that actually hurt — a swapped-away node
# cannot be announced, so ⌘K commands gave a screen-reader user no feedback at all.
#
# The browser suite proves the node's identity survives a keystroke; these guards are the blocking
# lane's copy, because the browser job is non-blocking and cannot stop the hoist being undone.
_PALETTE_RESULTS = _TEMPLATES / "search" / "partials" / "palette_results.html"
# The live region's own opening tag (no `>` inside any attribute value, so `[^>]*` bounds it).
_CMDK_LIVE_REGION = re.compile(r'<div\b[^>]*\bid="cmdk-command-result"[^>]*>', re.DOTALL)


def test_cmdk_live_region_is_a_sibling_of_the_results_listbox() -> None:
    """#cmdk-command-result must live OUTSIDE #cmdk-results — an innerHTML swap destroys anything inside it."""
    html = _strip_comments(_CMDK.read_text())
    start = html.find('id="cmdk-results"')
    assert start != -1, "expected the #cmdk-results swap target in cmdk_modal.html"
    # #cmdk-results is an EMPTY element in source (the fragment is swapped in), so the first
    # `</div>` after it is its own closing tag.
    close = html.find("</div>", start)
    inner = html[html.find(">", start) + 1 : close]
    assert "cmdk-command-result" not in inner, (
        "the ⌘K command live region must live OUTSIDE the #cmdk-results swap target — the debounced "
        "search swaps innerHTML on every keystroke, which destroys the node before the announcement "
        "is read, and a role=status child also makes the listbox invalid (axe: critical)"
    )
    region = _CMDK_LIVE_REGION.search(html)
    assert region, "cmdk_modal.html must render the #cmdk-command-result live region itself"
    assert html.find('id="cmdk-command-result"') > close, "the live region must be a SIBLING that follows #cmdk-results, not an ancestor-nested node"


def test_cmdk_live_region_keeps_its_status_semantics_and_stays_rendered() -> None:
    """The region announces only if it is a rendered live region BEFORE the content lands in it."""
    html = _strip_comments(_CMDK.read_text())
    region = _CMDK_LIVE_REGION.search(html)
    assert region, "cmdk_modal.html must render the #cmdk-command-result live region"
    tag = region.group(0)
    assert 'role="status"' in tag, "the ⌘K command live region is missing role=status"
    assert 'aria-live="polite"' in tag, "the ⌘K command live region is missing aria-live=polite"
    assert 'aria-atomic="true"' in tag, "the ⌘K command live region is missing aria-atomic=true"
    # Hiding it while empty reintroduces the defect in a different costume: a live region that is
    # created or revealed at announce time is not being observed when the mutation happens.
    for hidden in ("x-show=", "x-if=", "display:none", "display: none", "empty:hidden", " hidden"):
        assert hidden not in tag, f"the ⌘K command live region must stay rendered while empty — found {hidden!r} on it"


def test_the_palette_results_fragment_references_the_live_region_without_owning_it() -> None:
    """The swapped fragment keeps targeting the region by id; htmx and ARIA resolve it document-wide."""
    results = _strip_comments(_PALETTE_RESULTS.read_text())
    assert 'id="cmdk-command-result"' not in results, (
        "palette_results.html must not RENDER #cmdk-command-result — this fragment is the innerHTML "
        "of #cmdk-results, so a live region declared here is both an invalid listbox child and "
        "destroyed by the next keystroke (phaze-jng72)"
    )
    targeting = results.count('hx-target="#cmdk-command-result"')
    assert targeting >= 1, "the ⌘K command rows must still post their outcome into #cmdk-command-result"
    assert results.count('aria-describedby="cmdk-command-result"') == targeting, (
        "every command row that targets the live region must also name it via aria-describedby"
    )


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


def _record_body_after_swap() -> str:
    """The ``hx-on::after-swap`` expression on ``#record-body`` (the host's only one)."""
    m = _AFTER_SWAP.search(_strip_comments(_RECORD.read_text()))
    assert m, "expected an hx-on::after-swap handler on #record-body in record_host.html"
    return m.group(1)


def test_record_after_swap_waits_for_the_reveal_before_focusing_the_heading() -> None:
    """phaze-f65nu: the heading focus must be gated on #record-body being VISIBLE.

    The dialog is ``aria-modal`` and inert-s the shell behind it, so an open that does not move
    focus leaves the keyboard operator stranded in the inert half (WCAG 2.4.3 / 4.1.2). The
    original handler read correctly and did nothing: it flipped ``loaded = true`` and called
    ``h.focus()`` on the very next statement, but ``loaded`` is what reveals ``#record-body``
    through ``x-show`` and Alpine defers that reveal onto a later task — so ``focus()`` ran against
    a ``display:none`` element, where it is a silent no-op.

    Guarded here rather than only in ``tests/browser`` because the browser suite is a separate,
    non-blocking job: this is the lane that keeps a "simplification" back to a straight-line
    ``focus()`` from shipping. Asserted structurally (the call is not a top-level statement, and
    the handler consults a visibility primitive) so the guard survives the wait being reshaped.
    """
    expr = _record_body_after_swap()
    # The heading may suppress the browser's implicit scroll when the opener requested a stable
    # in-record section; that lets the handler focus first and then scroll the requested anchor.
    focus = re.search(r"\.focus\(", expr)
    assert focus, "the #record-body after-swap handler no longer focuses the record heading at all"

    depth = expr[: focus.start()].count("{") - expr[: focus.start()].count("}")
    assert depth > 0, (
        "the heading focus is a TOP-LEVEL statement in the after-swap handler, so it runs on the "
        "same task as the `loaded` flip — while x-show still has #record-body at display:none, "
        "where focus() silently no-ops and the dialog opens with focus outside it (phaze-f65nu)"
    )
    assert "getClientRects" in expr or "checkVisibility" in expr, (
        "the after-swap handler no longer checks that #record-body is actually visible before "
        "focusing its heading — deferring by a tick is NOT enough, because Alpine's x-show reveal "
        "lands on a later task than anything the handler can queue (phaze-f65nu)"
    )


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
    """The rail subtree must be x-data-rooted — Alpine only walks x-data-rooted subtrees.

    Without it every x-text numeral, x-show orphan badge, and pause/priority binding in the
    rail is silently inert: the badges forever render their server-side "0" defaults no matter
    what $store.pipeline holds (CONSOLE-02: the Analyze badge read 0 while 2,183 analyze jobs
    were in flight). Invisible to markup/httpx tests — only a live browser surfaced it.

    phaze-tzy6s.13 moved the Alpine root one level out. The <aside> is now wrapped by the
    drawer-state element (`x-data="{ navOpen: false }"`, `display:contents` so layout is
    unaffected), and the aside itself carries x-trap rather than x-data. The invariant is
    unchanged and is what this asserts: the OUTERMOST element of the rail partial is the Alpine
    root, so everything below it -- aside, nav, every store-bound numeral -- is inside a walked
    subtree. Asserting on the outermost element rather than specifically on <aside> keeps the
    guard about the property that matters instead of about which tag happens to hold it.
    """
    # Macro DEFINITIONS are not rendered where they sit, so they are not the root; drop them (and
    # the Jinja comments) to find the element the partial actually emits first.
    html = re.sub(r"\{%-?\s*macro\b.*?\{%-?\s*endmacro\s*-?%\}", "", _strip_comments(_RAIL.read_text()), flags=re.DOTALL)
    html = re.sub(r"\{%.*?%\}", "", html, flags=re.DOTALL).lstrip()
    root = re.match(r"<(?P<tag>[a-z]+)\b(?P<attrs>[^>]*)>", html)
    assert root, f"expected the rail partial to emit an element first, got: {html[:80]!r}"
    assert re.search(r"\bx-data\b", root.group("attrs")), (
        f"the rail's outermost element (<{root.group('tag')}>) must carry x-data so Alpine binds the "
        "rail subtree — without it every store-bound numeral/badge in the rail is inert and renders 0 forever"
    )
    # The nav must actually be INSIDE that root, not a sibling after it.
    assert "<aside" in html[root.end() :], "the rail <aside> escaped the Alpine root — its bindings would be inert"


# --- phaze-am7c: detail-pane own-tick must not steal focus every 5s ------------------

# The wave-2 body swapped into #detail-pane (_lane_detail.html) carries a bounded self-refresh
# own-tick (`hx-trigger="every 5s" hx-target="#detail-pane" hx-swap="innerHTML"`). htmx fires
# htmx:afterSwap on the swap TARGET for every swap into it, including the poll, so the shell's
# `hx-on::after-swap -> Alpine.$data(this).onLoaded()` runs every 5 seconds for as long as
# the pane is open. onLoaded() parks focus on the pane <h2 tabindex="-1"> — correct exactly
# ONCE, on the closed->open transition, and an a11y defect on every tick after that: the
# operator cannot tab to ✕ Close, read the recent-completions list, or type in the ⌘K
# palette / status filter without focus being yanked back every 5s.
#
# phaze-2u8v.6: _agent_activity.html carries the SAME own-tick shape but no longer targets
# #detail-pane — the agents-table detail is an expanded row now (admin/partials/
# _agent_detail_row.html), with the equivalent focus-once guard living on that row's own
# x-init (Alpine only initializes a component once, and hx-preserve keeps that same node —
# and its already-initialized component — alive across the table's unrelated 5s poll).
#
# The invariant below is behavioural, not textual: the focus call must still EXIST (initial
# open must keep moving focus), but it must be reachable only through a conditional whose
# predicate reads the pane's open state as it was BEFORE this swap flipped it true. Any fix
# shape satisfying that passes — `if (!this.open) { focus } ... this.open = true`, or a
# `const wasOpen = this.open` capture consulted after the flip.


def _detail_pane_component() -> str:
    """Return the shell <section x-data> expression from _detail_pane.html."""
    html = _strip_comments(_DETAIL_PANE.read_text())
    m = re.search(r'x-data="([^"]*)"', html, re.DOTALL)
    assert m, "expected the shell <section x-data> component"
    return m.group(1)


def _method_body(component: str, name: str) -> str:
    """Return the brace-delimited body of the Alpine method ``name``."""
    start = component.find(f"{name}()")
    assert start != -1, f"expected an Alpine method {name}() on the detail-pane shell"
    open_brace = component.find("{", start)
    assert open_brace != -1, f"{name}() has no body"
    depth = 0
    for i in range(open_brace, len(component)):
        if component[i] == "{":
            depth += 1
        elif component[i] == "}":
            depth -= 1
            if depth == 0:
                return component[open_brace + 1 : i]
    raise AssertionError(f"unbalanced braces in {name}()")


def _enclosing_block_predicate(body: str, index: int) -> str | None:
    """Return the ``if (...)`` predicate of the innermost block enclosing ``body[index]``.

    ``None`` means the statement sits at the method's top level (unconditional), or its
    innermost enclosing block is not an ``if``.
    """
    stack: list[int] = []
    for i in range(index):
        if body[i] == "{":
            stack.append(i)
        elif body[i] == "}" and stack:
            stack.pop()
    if not stack:
        return None
    head = body[: stack[-1]].rstrip()
    if not head.endswith(")"):
        return None
    depth = 0
    for i in range(len(head) - 1, -1, -1):
        if head[i] == ")":
            depth += 1
        elif head[i] == "(":
            depth -= 1
            if depth == 0:
                return head[i + 1 : len(head) - 1] if re.search(r"\bif\s*$", head[:i]) else None
    return None


def test_detail_pane_own_tick_does_not_resteal_focus() -> None:
    """onLoaded() must move focus to the pane heading ONLY on the closed->open transition.

    The 5s own-tick re-swaps #detail-pane and re-fires after-swap, so an unconditional
    focus() in onLoaded() rips focus off whatever the operator is using, every 5 seconds,
    on both the lane pane and the /admin/agents pane (same shell).
    """
    body = _method_body(_detail_pane_component(), "onLoaded")

    focus = re.search(r"[^;{}]*getElementById\('detail-pane-heading'\)\s*\??\.focus\(\)", body)
    assert focus, "onLoaded() must still focus #detail-pane-heading — parking focus on the pane heading on OPEN is the intended a11y behaviour"

    # The focus call is wrapped in a $nextTick callback; anchor on the statement that
    # SCHEDULES it, so the arrow function's own braces are not mistaken for a guard.
    stmt = body.rfind("this.$nextTick", 0, focus.start())
    assert stmt != -1, "expected the heading focus to be scheduled via $nextTick"

    predicate = _enclosing_block_predicate(body, stmt)
    assert predicate is not None, (
        "the heading focus in onLoaded() is UNCONDITIONAL — htmx fires afterSwap on "
        "#detail-pane for the 5s own-tick too, so focus is yanked back to the heading every "
        "tick. Guard it on the closed->open transition."
    )

    flip = body.find("open = true")
    assert flip != -1, "expected onLoaded() to flip the pane open"

    if re.search(r"\bopen\b", predicate):
        # Direct form: the guard reads `open` itself, so it must run BEFORE the flip.
        assert stmt < flip, (
            f"the focus guard {predicate!r} reads `open` AFTER onLoaded() already set it true, so it "
            "is true on the initial open too and the guard can never distinguish a poll tick"
        )
        return

    # Capture form: a local bound to the pre-flip `open` value, consulted by the predicate.
    for m in re.finditer(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:this\.)?open\b", body):
        if m.start() < flip and re.search(rf"\b{re.escape(m.group(1))}\b", predicate):
            return
    raise AssertionError(
        f"the focus guard {predicate!r} does not read the pane's PRE-swap open state, so it cannot "
        "tell the initial card-click open from a 5s own-tick refresh"
    )


# phaze-coypu: direct-surface text colours are intent-named. The repainted Tailwind
# rungs remain available for borders, focus rings, tinted backgrounds, and the few
# deep-rung text colours intentionally paired with tinted alert surfaces. They are
# no longer the vocabulary for muted, link/info, or status text on the four app
# surfaces: those meanings resolve once per theme in assets/src/app.css.
_RAW_SEMANTIC_TEXT_COLOUR = re.compile(
    r"text-(?:"
    r"gray-(?:400|500)|"
    r"blue-(?:500|600|700)|"
    r"green-(?:600|700)|"
    r"emerald-(?:600|700)|"
    r"amber-(?:500|600|700)|"
    r"red-(?:600|700)|"
    r"rose-600"
    r")\b|"
    r"dark:text-(?:"
    r"gray-400|"
    r"blue-(?:300|400)|"
    r"green-400|"
    r"emerald-(?:300|400)|"
    r"amber-(?:300|400)|"
    r"red-(?:300|400)|"
    r"rose-(?:300|400)"
    r")\b"
)
_SEMANTIC_TEXT_OPACITY = re.compile(r"text-(?:muted|link|info|ok|warn|danger)/\d+\b")
_ROUTERS = _TEMPLATES.parent / "routers"


def test_semantic_colour_guard_recognizes_raw_variants_and_permits_tokens() -> None:
    raw = ("text-gray-500", "dark:text-gray-400", "hover:text-green-700", "aria-[current=page]:text-blue-700", "text-warn/80")
    semantic = ("text-muted", "text-link", "text-info", "text-ok", "text-warn", "text-danger")

    assert all(_RAW_SEMANTIC_TEXT_COLOUR.search(utility) or _SEMANTIC_TEXT_OPACITY.search(utility) for utility in raw)
    assert not any(_RAW_SEMANTIC_TEXT_COLOUR.search(utility) or _SEMANTIC_TEXT_OPACITY.search(utility) for utility in semantic)


def test_direct_surface_text_colours_use_semantic_tokens() -> None:
    """Raw hue/rung text utilities cannot bypass the theme-resolved vocabulary."""
    offenders: list[str] = []
    sources = [*_TEMPLATES.rglob("*.html"), *_ROUTERS.rglob("*.py")]
    for source_path in sorted(sources):
        source = source_path.read_text()
        scannable = _COMMENTS.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), source)
        for pattern in (_RAW_SEMANTIC_TEXT_COLOUR, _SEMANTIC_TEXT_OPACITY):
            for match in pattern.finditer(scannable):
                line_no = scannable.count("\n", 0, match.start()) + 1
                offenders.append(f"{source_path.relative_to(_TEMPLATES.parent)}:{line_no} -> {match.group(0)}")

    assert not offenders, (
        "direct-surface text must use opaque intent-named utilities (text-muted, text-link, text-info, "
        "text-ok, text-warn, text-danger) instead of a raw hue/rung or opacity modifier:\n  " + "\n  ".join(offenders)
    )


# phaze-4yrle: two competing `dark:` utilities of the SAME property on one element is always a
# defect, and a silent one. Which utility wins is decided by the order Tailwind emits them into
# the stylesheet, NOT by their order in the class attribute, so the rendered dark-mode colour is
# not necessarily the one the author wrote last -- and one of the two is dead in every case.
#
# Found by the phaze-tzy6s per-slice audit in three templates (stats_bar.html, trigger_response.html,
# trigger_tracklist_response.html), all blamed OUTSIDE that epic (PR #38 and PR #131), i.e. this
# survived every prior review because nothing was looking for it. Hence a guard rather than three
# one-line fixes.
#
# Scoped to `dark:text-<colour>` deliberately. A general "no duplicate utility" sweep would have to
# model Tailwind's whole conflict lattice (p-2 vs px-2, text-sm vs text-gray-500 -- `text-` is both
# size and colour) and would be a false-positive engine. This checks one property, in one variant,
# where a duplicate is unambiguously wrong.
_DARK_TEXT_COLOUR = re.compile(r"dark:text-(?:[a-z-]+)-\d{2,3}\b")

# phaze-4yrle REDRIVE: the first version of this guard read the markup LINE BY LINE and skipped
# any class attribute containing `{{` or `{%`. Both shortcuts made it blind exactly where
# duplicates accumulate, and it passed on main with two live offenders present:
#
#   1. `class="([^"]*)"` per line never matches a class attribute wrapped across lines, and a long
#      wrapped attribute is precisely where a stray extra utility hides (cue/partials/cue_row.html).
#   2. Skipping the WHOLE attribute when it contains Jinja is right about the alternatives
#      ({{ 'dark:text-gray-100' if x else 'dark:text-gray-400' }} emits one colour, not two) but
#      wrong about everything else in the attribute -- including two colours sitting together
#      INSIDE one branch (execution/partials/filter_tabs.html).
#
# So the guard now reads each class attribute as authored (whole-file scan, `[^"]` spans newlines)
# and models what the attribute can actually EMIT: static text is always emitted, each
# `{% if %}/{% elif %}/{% else %}` branch is an alternative, and a `{{ ... }}` expression (or an
# Alpine `:class` ternary) contributes at most one of its quoted literals. A duplicate is a
# duplicate iff some single emitted class string carries two `dark:text-<colour>` utilities.
#
# Which of a duplicate pair actually renders was MEASURED off the built stylesheet, not assumed --
# `src/phaze/static/css/app.css` emits the gray text colours in ascending scale order
# (`.dark\:text-gray-300` < `-400` < `-500`), all as `:where(.dark, .dark *)`, which contributes
# zero specificity. Equal specificity means the LAST rule emitted wins, so the higher scale number
# always renders regardless of class-attribute order. Both offenders this redrive fixed were
# therefore rendering `dark:text-gray-500`, and both KEPT `dark:text-gray-400` -- because on this
# theme's dark surfaces gray-500 fails WCAG 2.1 AA for normal text and gray-400 passes:
#   gray-500 on `--color-phaze-panel` #10141c = 3.81:1   gray-400 = 7.09:1   (cue_row.html pill)
#   gray-500 on `--color-phaze-bg`    #0a0c12 = 4.04:1   gray-400 = 7.51:1   (filter_tabs.html tab)
# Dropping the duplicate is thus not cosmetic here: it changes the rendered colour, and it is the
# 4.5:1 threshold that decides which one goes. `proposals/partials/filter_tabs.html` -- the same
# component without the strays -- independently agrees on `text-gray-500 dark:text-gray-400`.
#
# phaze-o8voj RESOLVED the `{% set %}` half of this limit: a same-file `{% set NAME = '...' %}`
# (single or double quoted) bare-string-literal assignment now resolves into every attribute that
# references `{{ NAME }}`, so a class string assembled across a `{% set %}` and the attribute that
# uses it is no longer opaque -- see `_extract_set_vars` below.
#
# KNOWN LIMIT (still open): a class string passed INTO a macro -- i.e. bound to a macro parameter
# by the call site, not assigned via `{% set %}` -- stays opaque, because that requires following
# the call site into the macro body across (possibly) file boundaries, which is a materially larger
# analysis than resolving a same-file literal. Also opaque, deliberately: a `{% set %}` built from
# something other than a bare string literal -- concatenation (`~`), a filter, or another variable
# -- because guessing its value would risk a false positive, which is worse than staying blind (see
# module docstring's guiding principle at the top of this file). Swept manually at redrive time and
# clean on both counts: no macro-parameter class string and no non-literal `{% set %}` carries two
# `dark:text-*` colours in the tree today. Also swept at redrive time, also clean: `dark:bg-*` and
# `dark:border-*`, which have the same duplicate-is-always-a-defect property and would be the
# natural widening if either ever grows an offender.

# `{% ... %}` / `{{ ... }}`; DOTALL because a wrapped attribute puts newlines inside both.
_JINJA_BRANCH_TAG = re.compile(r"\{%-?\s*(if|elif|else|endif)\b.*?-?%\}", re.DOTALL)
_JINJA_ANY_TAG = re.compile(r"\{%-?.*?-?%\}", re.DOTALL)
_JINJA_EXPR = re.compile(r"(\{\{.*?\}\})", re.DOTALL)
_SINGLE_QUOTED = re.compile(r"'([^']*)'")
# Comments are prose, not markup: shell.html documents a superseded `:class="..."` binding in one,
# and a guard that reads comments would flag the documentation instead of the code.
_COMMENTS = re.compile(r"<!--.*?-->|\{#.*?#\}", re.DOTALL)
# Both the plain attribute and Alpine's `:class` binding, which are parsed differently: a `class`
# value is Jinja-templated markup, a `:class` value is one JS expression. A value can never contain
# a `"` (that is what closes it), so `[^"]*` bounds the attribute exactly even across newlines.
_CLASS_ATTR = re.compile(r'(?P<alpine>:)?class="(?P<value>[^"]*)"')

# A same-file `{% set NAME = '...' %}` / `{% set NAME = "..." %}` bare-string-literal assignment.
# Deliberately narrow: `NAME = other_var`, `NAME = a ~ b`, `NAME = a|filter`, and multi-target
# `{% set a, b = ... %}` all fail to match and stay opaque (see the KNOWN LIMIT comment above) --
# guessing a value here would risk manufacturing a false positive, which this guard must not do.
_SET_STRING = re.compile(r"\{%-?\s*set\s+(\w+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\")\s*-?%\}")
# A `{{ ... }}` expression that is nothing but a bare variable reference -- no filter, no ternary,
# no attribute/index access -- is the only shape resolved against `{% set %}` literals below.
_BARE_VAR = re.compile(r"^\{\{\s*(\w+)\s*\}\}$")


def _extract_set_vars(source: str) -> dict[str, list[str]]:
    """Same-file `{% set NAME = '...' %}` string literals this template defines, by name.

    A name `{% set %}` more than once (e.g. once per branch of an `{% if %}`/`{% elif %}` chain,
    like ``lane_color`` in ``_lane_card.html``) collects every distinct literal it was ever
    assigned, mirroring how this guard already treats `{% if %}` branches as alternatives: each
    literal is one possible render, and a duplicate is flagged if ANY of them collides.
    """
    resolved: dict[str, list[str]] = {}
    for match in _SET_STRING.finditer(source):
        name = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        values = resolved.setdefault(name, [])
        if value not in values:
            values.append(value)
    return resolved


def _expression_alternatives(expr: str, set_vars: dict[str, list[str]] | None = None) -> list[str]:
    """Class strings a `{{ ... }}` (or Alpine ternary) can contribute -- at most ONE quoted literal.

    A bare `{{ NAME }}` reference to a same-file `{% set %}` string literal resolves to that
    literal's alternatives (phaze-o8voj); anything else stays opaque, the "" no-contribution
    alternative, exactly as before.
    """
    literals = _SINGLE_QUOTED.findall(expr)
    if literals:
        # "" covers the falsy/no-literal branch, so an expression never fabricates a duplicate on
        # its own.
        return [*literals, ""]
    bare = _BARE_VAR.match(expr)
    if bare and set_vars and bare.group(1) in set_vars:
        return list(set_vars[bare.group(1)])
    return [""]


def _alpine_class_alternatives(expr: str) -> list[str]:
    """Class strings an Alpine `:class` value can apply.

    A `cond ? 'a' : 'b'` ternary applies exactly ONE side, so its literals are alternatives -- reading
    them as one concatenated string is a false positive, and the shape is common enough in this tree
    (rail.html, header.html, _dupe_group.html) to matter. The object/array forms
    (`{'a': x, 'b': y}`) can apply several literals at once, so there they stay concatenated.
    """
    return _expression_alternatives(expr) if "?" in expr else [expr]


def _expand_expressions(text: str, set_vars: dict[str, list[str]] | None = None) -> list[str]:
    """Expand a branch-free fragment into every class string it can emit."""
    emitted = [""]
    for part in _JINJA_EXPR.split(text):
        alternatives = _expression_alternatives(part, set_vars) if part.startswith("{{") else [_JINJA_ANY_TAG.sub(" ", part)]
        emitted = [f"{done} {alternative}" for done in emitted for alternative in alternatives]
    return list(dict.fromkeys(emitted))


def _split_top_level_if(text: str) -> tuple[str, list[str], str] | None:
    """Split on the FIRST top-level `{% if %}`: (always-emitted prefix, branches, remaining suffix)."""
    depth = 0
    opened: re.Match[str] | None = None
    separators: list[re.Match[str]] = []
    for tag in _JINJA_BRANCH_TAG.finditer(text):
        keyword = tag.group(1)
        if keyword == "if":
            depth += 1
            if depth == 1:
                opened, separators = tag, []
        elif keyword == "endif":
            depth -= 1
            if depth == 0 and opened is not None:
                branches, cut = [], opened.end()
                for separator in separators:
                    branches.append(text[cut : separator.start()])
                    cut = separator.end()
                branches.append(text[cut : tag.start()])
                if not any(separator.group(1) == "else" for separator in separators):
                    branches.append("")  # an `{% if %}` with no `{% else %}` can also emit nothing
                return text[: opened.start()], branches, text[tag.end() :]
        elif depth == 1:
            separators.append(tag)
    return None


def _emitted_class_strings(attr: str, set_vars: dict[str, list[str]] | None = None) -> list[str]:
    """Every class string this attribute can render as -- one per combination of branches taken."""
    split = _split_top_level_if(attr)
    if split is None:
        return _expand_expressions(attr, set_vars)
    prefix, branches, suffix = split
    emitted = [
        f"{before} {inside} {after}"
        for before in _expand_expressions(prefix, set_vars)
        for branch in branches
        for inside in _emitted_class_strings(branch, set_vars)
        for after in _emitted_class_strings(suffix, set_vars)
    ]
    return list(dict.fromkeys(emitted))


def test_no_element_carries_two_dark_text_colours() -> None:
    offenders: list[str] = []
    for template in sorted(_TEMPLATES.rglob("*.html")):
        source = template.read_text()
        # Blank comments out rather than deleting them, so offsets (and line numbers) stay true.
        scannable = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), source)
        # Same-file `{% set %}` string literals this template defines (phaze-o8voj) -- resolved
        # into any `{{ NAME }}` reference below, so a class string assembled across a `{% set %}`
        # and the attribute that uses it is no longer invisible to this scan.
        set_vars = _extract_set_vars(scannable)
        for match in _CLASS_ATTR.finditer(scannable):
            line_no = scannable.count("\n", 0, match.start()) + 1
            value = match.group("value")
            renders = _alpine_class_alternatives(value) if match.group("alpine") else _emitted_class_strings(value, set_vars)
            for emitted in renders:
                hits = _DARK_TEXT_COLOUR.findall(emitted)
                if len(hits) > 1:
                    rel = template.relative_to(_TEMPLATES)
                    offenders.append(f"{rel}:{line_no} emits {len(hits)} dark: text colours together -> {' '.join(hits)}")
                    break  # one report per attribute; the first offending branch is enough to locate it

    assert not offenders, (
        "an element carries two competing `dark:` text colours; the winner is decided by Tailwind's "
        "emit order, not by class-attribute order, so one is dead and the rendered colour may not be "
        "the intended one:\n  " + "\n  ".join(offenders)
    )


# The guard above is only worth its line count if it can SEE the shapes duplicates actually take.
# Its first version could not, and shipped green over two live offenders for exactly that reason, so
# these fix the two blind spots as executable cases rather than as a comment nobody re-checks. Each
# `_carries` case is a real shape lifted from this tree; each `_permits` case is the false positive
# the narrowing must not produce.
def _worst_case(attr: str, *, alpine: bool = False, set_vars: dict[str, list[str]] | None = None) -> int:
    """Most `dark:` text colours any single render of this attribute puts on one element."""
    renders = _alpine_class_alternatives(attr) if alpine else _emitted_class_strings(attr, set_vars)
    return max(len(_DARK_TEXT_COLOUR.findall(emitted)) for emitted in renders)


def test_the_duplicate_dark_utility_guard_sees_a_wrapped_class_attribute() -> None:
    # Blind spot 1: a per-LINE `class="([^"]*)"` never matches this at all, and long wrapped
    # attributes are precisely where a stray extra utility survives review (cue_row.html).
    assert _worst_case("text-xs rounded-full\n            bg-gray-100 text-gray-500 dark:text-gray-400 dark:text-gray-500") == 2


def test_the_duplicate_dark_utility_guard_sees_a_duplicate_inside_one_jinja_branch() -> None:
    # Blind spot 2: skipping the whole attribute because it contains `{%` also skips both colours
    # when they sit together in ONE branch and are therefore emitted together (filter_tabs.html).
    attr = "px-3 {% if active %} dark:text-blue-400 {% else %} dark:text-gray-400 dark:text-gray-500 {% endif %}"
    assert _worst_case(attr) == 2


def test_the_duplicate_dark_utility_guard_sees_a_branch_colliding_with_the_static_part() -> None:
    # An always-emitted colour plus a conditional one is a real collision whenever the branch is taken.
    assert _worst_case("dark:text-gray-400 {{ 'dark:text-gray-500' if muted }}") == 2


def test_the_duplicate_dark_utility_guard_sees_two_independent_ifs_that_can_both_fire() -> None:
    # Sequential `{% if %}`s are NOT alternatives -- nothing stops both conditions holding, so both
    # colours land on the element together. The branch model has to keep them independent; collapsing
    # sequential branches into one choice would silently exempt this.
    assert _worst_case("{% if a %}dark:text-gray-400{% endif %} {% if b %}dark:text-gray-500{% endif %}") == 2


def test_the_duplicate_dark_utility_guard_sees_through_a_nested_if() -> None:
    # A duplicate does not become invisible by sitting one level deeper; the split recurses.
    assert _worst_case("{% if a %}{% if b %}dark:text-gray-100 dark:text-gray-200{% endif %}{% endif %}") == 2


def test_the_duplicate_dark_utility_guard_permits_genuine_jinja_alternatives() -> None:
    # The exemption the original guard was reaching for, kept exactly: a ternary and an if/else emit
    # ONE colour each, so they are alternatives, not duplicates. Narrowing must not break this.
    assert _worst_case("{{ 'dark:text-gray-100' if selected else 'dark:text-gray-400' }}") == 1
    assert _worst_case("{% if selected %} dark:text-gray-100 {% else %} dark:text-gray-400 {% endif %}") == 1
    # An `{% elif %}` chain is the same thing with more arms -- the shape of _cue_preview.html and
    # rail.html, which are the tree's real three-way alternatives and must stay exempt.
    assert _worst_case("{% if a %}dark:text-red-400{% elif b %}dark:text-gray-300{% else %}dark:text-amber-300{% endif %}") == 1


def test_the_duplicate_dark_utility_guard_permits_an_alpine_class_ternary() -> None:
    # `:class="cond ? 'a' : 'b'"` is Alpine's alternatives form; only one side is ever applied.
    assert _worst_case("$store.theme.dim ? 'dark:text-gray-500' : 'dark:text-gray-300'", alpine=True) == 1


def test_the_duplicate_dark_utility_guard_sees_a_duplicate_in_one_alpine_ternary_arm() -> None:
    # ...but the narrowing must stay narrow: two colours in ONE arm are still applied together.
    assert _worst_case("$store.theme.dim ? 'dark:text-gray-400 dark:text-gray-500' : ''", alpine=True) == 2


def test_the_duplicate_dark_utility_guard_permits_a_hover_variant_alongside_a_base_colour() -> None:
    # `dark:hover:text-*` is a different variant, not a competing declaration for the same state.
    assert _worst_case("text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300") == 1


# --- phaze-o8voj: same-file `{% set %}` class strings are no longer opaque to the guard -----------
#
# The bead's own example: `{% set _btn = 'text-gray-700 dark:text-gray-300' %}` followed by
# `class="{{ _btn }} dark:text-gray-400"` puts two competing `dark:` text colours on one element,
# but only through a variable, not a literal in the attribute itself.

_SET_BTN_SOURCE = "{% set _btn = 'text-gray-700 dark:text-gray-300' %}"


def test_the_pre_o8voj_guard_could_not_see_a_set_composed_duplicate_at_all() -> None:
    """Proves the gap: with NO `{% set %}` resolution (the guard as it stood before this bead),
    `{{ _btn }}` is fully opaque -- its "" no-contribution alternative is all `_expression_alternatives`
    could ever return for a bare variable reference -- so the composed duplicate below was invisible.
    """
    assert _worst_case("{{ _btn }} dark:text-gray-400") == 1


def test_the_duplicate_dark_utility_guard_sees_a_set_variable_composed_with_a_colliding_literal() -> None:
    # Same fixture, now WITH `{% set %}` resolution: `_btn` resolves to its literal, so the guard
    # sees both `dark:text-gray-300` (from the variable) and `dark:text-gray-400` (from the
    # attribute) landing on the same element.
    set_vars = _extract_set_vars(_SET_BTN_SOURCE)
    assert _worst_case("{{ _btn }} dark:text-gray-400", set_vars=set_vars) == 2


def test_the_duplicate_dark_utility_guard_permits_a_set_variable_that_does_not_collide() -> None:
    # The narrowing must not manufacture a false positive: `_btn` still contributes its ONE colour,
    # and nothing else in the attribute collides with it.
    set_vars = _extract_set_vars(_SET_BTN_SOURCE)
    assert _worst_case("{{ _btn }} font-semibold", set_vars=set_vars) == 1


def test_the_duplicate_dark_utility_guard_sees_a_set_variable_defined_per_if_branch() -> None:
    # A name `{% set %}` once per branch (the real shape of `lane_color` in `_lane_card.html`) --
    # each literal is an alternative, and a duplicate is flagged if the attribute pairs ANY of them
    # with a colliding literal.
    source = "{% if a %}{% set c = 'dark:text-emerald-300' %}{% else %}{% set c = 'dark:text-blue-300' %}{% endif %}"
    set_vars = _extract_set_vars(source)
    assert set_vars["c"] == ["dark:text-emerald-300", "dark:text-blue-300"]
    assert _worst_case("{{ c }} dark:text-blue-300", set_vars=set_vars) == 2


def test_set_variable_extraction_ignores_non_literal_assignments() -> None:
    # `{% set %}` built from another variable, a filter, or concatenation is not a bare string
    # literal -- stays opaque rather than guessed, so it cannot manufacture a false positive.
    assert _extract_set_vars("{% set _cls = other_var %}") == {}
    assert _extract_set_vars("{% set _cls = 'a' ~ suffix %}") == {}
    assert _extract_set_vars("{% set _cls = raw_cls|trim %}") == {}


def test_set_variable_extraction_reads_double_quoted_literals_too() -> None:
    # `{% set %}` in this tree uses both quote styles (e.g. `heading_class` in rail.html uses
    # double quotes) -- both must resolve.
    set_vars = _extract_set_vars('{% set c = "dark:text-gray-300" %}')
    assert set_vars == {"c": ["dark:text-gray-300"]}
