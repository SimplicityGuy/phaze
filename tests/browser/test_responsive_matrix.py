"""phaze-fk1ww: the viewport x theme x state matrix ADR-0009 asks for and nothing ran.

What was missing
================

Two lanes claimed to enforce ADR-0009's responsive/accessibility baseline and neither covered the
middle of it:

* ``tests/shared/core/test_cross_workspace_responsive_a11y.py`` sweeps every template's **markup**.
  It is exhaustive about classes and attributes and cannot compute a rendered layout at all.
* ``tests/browser/test_shell_contract.py`` computes real layout, but only at 1440x900 and 390x844,
  only in the default (light) theme, and only against an **empty** database.

So three properties were unverified at every workspace: the ``md`` band between those two widths
(768-1023px takes the drawer branch with none of a phone's other constraints), the dark theme, and
any state with rows in it. An empty workspace is the one state where "the page does not scroll
sideways" is trivially true -- there is no table to overflow. This module closes all three, and it
is written as a matrix rather than as a handful of spot checks so the record can say *which* cells
were exercised instead of "responsive was checked".

What each cell asserts
======================

Per workspace, per cell, the four properties that are computed-layout facts and therefore invisible
to the markup sweep:

1. the document never scrolls sideways (ADR-0009 "Tables");
2. every visible table sits in a container that actually computes ``overflow-x: auto|scroll``;
3. the navigation branch matches the width -- expanded rail at ``lg``+, off-canvas drawer below it,
   and NO icon-only state at any width (ADR-0009 "Breakpoints");
4. every visible icon-only control has a non-empty accessible name at runtime, not merely an
   ``aria-label`` attribute in a template (ADR-0009 "Controls").

Plus, per cell, that the theme under test is the one that actually painted: the ``.dark`` class is
present or absent as pinned AND the resolved background colour differs between the two themes. The
second half is what stops a "dark theme validated" claim from being satisfied by a page that stayed
light because the pre-flash script never ran.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from tests.browser import _seed
from tests.browser.conftest import THEMES, VIEWPORTS


if TYPE_CHECKING:
    from collections.abc import Iterator


pytestmark = pytest.mark.browser


# Every rail destination: STAGE_PARTIALS' twelve DAG stages plus UTILITY_PANES' three, minus the two
# Changes Review aliases (`tagwrite`, `move`) that render the same partial as `rename` and are
# deliberately absent from the rail. Kept in rail order so a failure names where in the rail it sat.
WORKSPACES: tuple[str, ...] = (
    "summary",
    "files",
    "discover",
    "metadata",
    "analyze",
    "tracklist",
    "propose",
    "rename",
    "dedupe",
    "cue",
    "apply",
    "operations",
    "audit",
    "agents",
)

# ``lg`` in the ADR's breakpoint table. At or above it the rail is static and expanded; below it the
# same <aside> is an off-canvas drawer. One constant, because two tests disagreeing about where the
# fork sits is how a band ends up untested -- which is the defect this module exists to fix.
_LG_BREAKPOINT_PX = 1024


def _below_lg(viewport: str) -> bool:
    return VIEWPORTS[viewport]["width"] < _LG_BREAKPOINT_PX


# (workspace, viewport) pairs that DO scroll the document sideways once they have rows in them.
# Found by this sweep on 2026-08-17 and filed as phaze-mrg1c rather than fixed here, per
# phaze-fk1ww's brief.
#
# This is not a suppression list and must not become one. Two guards keep it honest:
#   * an overflow at any pair NOT listed here still fails the sweep, so a new regression is caught;
#   * a listed pair that no longer reproduces ALSO fails, so the entry has to be deleted when the
#     defect is fixed instead of surviving as stale text that quietly excuses a future one.
# Widening it to a whole viewport or a bare workspace name would defeat both. Add pairs only with a
# bead id, and delete them with the fix.
KNOWN_OVERFLOW: dict[tuple[str, str], str] = {
    ("files", "desktop"): "phaze-mrg1c",
    ("files", "tablet"): "phaze-mrg1c",
    ("audit", "tablet"): "phaze-mrg1c",
    ("audit", "phone"): "phaze-mrg1c",
}


async def _open(page: Any, path: str) -> None:
    """Navigate and wait until the shell is interactive.

    Deliberately NOT ``wait_until="networkidle"``: the shell holds a 5s stats poll and opens SSE
    streams, so the network is never idle and that wait blocks until the timeout.
    """
    await page.goto(path, wait_until="domcontentloaded")
    await page.wait_for_selector("#stage-workspace", state="attached")
    await page.wait_for_function("() => window.Alpine !== undefined && window.htmx !== undefined")


# The whole per-visit probe in one round trip. Split into four evaluates it would be four sequential
# CDP calls per workspace per cell -- 252 visits x 4 -- for measurements that must in any case all
# describe the same frame.
_PROBE_JS = """
() => {
    const doc = document.documentElement;
    const scrollsX = (el) => {
        const overflow = getComputedStyle(el).overflowX;
        return overflow === 'auto' || overflow === 'scroll';
    };
    // Element.checkVisibility, not a hand-rolled display/visibility pair. A control inside a CLOSED
    // <details> is laid out (getBoundingClientRect returns a real box) but not rendered: Chromium
    // hides the disclosure content with content-visibility, which zeroes innerText while leaving the
    // box measurable. A display/visibility probe therefore reports "/s/discover has a visible
    // button with no accessible name" about a button that reads "Run recovery check" and is not on
    // screen at all. checkVisibility is the API that knows about content-visibility.
    const visible = (el) => {
        if (!el.checkVisibility({ contentVisibilityAuto: true, opacityProperty: true, visibilityProperty: true })) return false;
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
    };

    // Tables: a visible table must sit inside SOME ancestor that actually computes a horizontal
    // scroller. Asserting the class would re-assert what the markup sweep already covers; asserting
    // the computed value is what catches a wrapper whose overflow is undone by a parent rule.
    const unwrapped = [];
    let tables = 0;
    for (const table of document.querySelectorAll('table')) {
        if (!visible(table)) continue;   // <table hidden> carriers are exempt by ADR-0009.
        tables += 1;
        let node = table.parentElement, wrapped = false;
        while (node && node !== doc) {
            if (scrollsX(node)) { wrapped = true; break; }
            node = node.parentElement;
        }
        if (!wrapped) unwrapped.push(table.id || table.className || '<table>');
    }

    // Icon-only controls: a control whose rendered text is empty announces as "button" unless it
    // carries an accessible name. innerText (not textContent) so sr-only text still counts as a
    // name source only where it is actually exposed.
    const unnamed = [];
    let iconOnly = 0;
    for (const el of document.querySelectorAll('button, a[href], [role="button"]')) {
        if (!visible(el)) continue;
        if ((el.innerText || '').trim()) continue;
        iconOnly += 1;
        const name = (el.getAttribute('aria-label') || '').trim()
            || (el.getAttribute('aria-labelledby') ? 'labelledby' : '')
            || (el.querySelector('.sr-only') ? (el.querySelector('.sr-only').textContent || '').trim() : '');
        if (!name) unnamed.push(el.id || el.className || el.tagName);
    }

    const rail = document.querySelector('aside[aria-label="Pipeline navigation"]');
    const trigger = document.querySelector('#rail-drawer-trigger');
    const railLinks = rail ? [...rail.querySelectorAll('a[data-rail-stage]')] : [];

    // Which elements are actually past the right edge. "The page scrolls sideways by 429px" names a
    // symptom; the offender is usually a flex/grid CHILD that never got `min-width: 0`, several
    // levels above the table that appears to be at fault. Reported deepest-first and capped, so the
    // failure message points at a node instead of at the document.
    const culprits = [];
    if (doc.scrollWidth - doc.clientWidth > 1) {
        for (const el of document.querySelectorAll('body *')) {
            const box = el.getBoundingClientRect();
            if (box.right <= doc.clientWidth + 1 || box.width === 0) continue;
            if (!visible(el)) continue;
            culprits.push({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                cls: (el.className && el.className.toString ? el.className.toString() : '').slice(0, 80),
                right: Math.round(box.right),
                width: Math.round(box.width),
            });
        }
    }

    // Two numbers, not one, because they answer different questions and only the second is proof of
    // harm. `scrollWidth - clientWidth` is the scrolling AREA; the shell sets `overflow-hidden` on
    // <body>, and an overflow-hidden box is still scrollable programmatically and by focus -- so the
    // area on its own can be a phantom nobody can reach. `scrollX` after a real scroll attempt is
    // whether the document ACTUALLY moves, which is the moment the header and rail leave the screen.
    const scrolledTo = (() => {
        const before = window.scrollX;
        window.scrollTo(9999, 0);
        const reached = window.scrollX;
        window.scrollTo(before, 0);
        return Math.round(reached);
    })();

    return {
        overflow: doc.scrollWidth - doc.clientWidth,
        scrolledTo,
        // Outermost first: document order, and the OUTERMOST overflowing node is the one whose
        // sizing is actually wrong. The deep cells below it are just being carried.
        culprits: culprits.slice(0, 5),
        tables, unwrapped, iconOnly, unnamed,
        dark: doc.classList.contains('dark'),
        background: getComputedStyle(document.body).backgroundColor,
        railVisible: rail ? visible(rail) : false,
        railWidth: rail ? Math.round(rail.getBoundingClientRect().width) : 0,
        railDestinations: railLinks.length,
        // A rail whose destinations render no visible label text is the icon-only state the ADR
        // forbids at every width. Measured on the OPEN surface only; a closed drawer legitimately
        // shows nothing at all.
        labelledDestinations: railLinks.filter(a => (a.innerText || '').trim().length > 1).length,
        triggerVisible: trigger ? visible(trigger) : false,
    };
}
"""


async def _probe(page: Any) -> dict[str, Any]:
    return dict(await page.evaluate(_PROBE_JS))


def _collect_console(page: Any, sink: list[str]) -> None:
    page.on("console", lambda message: sink.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda exc: sink.append(str(exc)))


async def _apply_state(dsn: str, state: str) -> None:
    """Put the app's database into one of the matrix's named states."""
    await _seed.reset(dsn)
    if state == "populated":
        await _seed.seed_populated(dsn)
    elif state == "degraded":
        # The product's real warning surface: terminal metadata + analysis failures, which
        # routers/shell.py turns into `danger`-toned Summary attention cards.
        await _seed.seed_populated(dsn, with_failures=True)
    elif state != "empty":  # pragma: no cover - guards a typo'd parametrization
        raise ValueError(f"unknown state {state!r}")


def _check_cell(
    *, stage: str, state: str, viewport: str, theme: str, probe: dict[str, Any], errors: list[str], known: set[tuple[str, str]]
) -> list[str]:
    """Return every violation in this cell, rather than raising on the first.

    A verification sweep that stops at the first failure reports one defect per RUN, and each run
    costs a full browser walk -- so a real inventory would take as many runs as there are defects,
    with the later ones only discoverable after the earlier ones are fixed. Collecting means one run
    yields the whole list, which is what a bead whose deliverable is evidence needs.
    """
    where = f"/s/{stage} @ {viewport} {VIEWPORTS[viewport]['width']}px / {theme} / {state}"
    found: list[str] = []

    if errors:
        found.append(f"{where}: console errors {errors}")
    if probe["overflow"] > 1:
        detail = (
            f"{where}: the document scrolls sideways by {probe['overflow']}px "
            f"(reachable: scrollX reaches {probe['scrolledTo']}px); widest offenders {probe['culprits']}"
        )
        if (stage, viewport) in KNOWN_OVERFLOW:
            known.add((stage, viewport))
            print(f"[known {KNOWN_OVERFLOW[stage, viewport]}] {detail}")
        else:
            found.append(detail)
    if probe["unwrapped"]:
        found.append(f"{where}: visible table(s) with no horizontal scroll container: {probe['unwrapped']}")
    if probe["unnamed"]:
        found.append(f"{where}: icon-only control(s) with no accessible name: {probe['unnamed']}")
    if probe["dark"] is not (theme == "dark"):
        found.append(f"{where}: pinned theme {theme!r} did not paint (html.dark={probe['dark']})")

    if _below_lg(viewport):
        if probe["railVisible"]:
            found.append(f"{where}: the rail is still mounted below lg -- this is the icon-rail defect")
        if not probe["triggerVisible"]:
            found.append(f"{where}: no drawer trigger below lg")
    else:
        if not probe["railVisible"]:
            found.append(f"{where}: the expanded rail is missing at lg+")
        elif probe["railWidth"] < 240:
            found.append(f"{where}: the rail computes {probe['railWidth']}px wide -- an icon strip, not the 280px expanded rail")
        if probe["labelledDestinations"] != probe["railDestinations"]:
            missing = probe["railDestinations"] - probe["labelledDestinations"]
            found.append(f"{where}: {missing} of {probe['railDestinations']} destinations render no visible label")
    return found


def _cells() -> Iterator[Any]:
    return (pytest.param(viewport, theme, id=f"{viewport}-{theme}") for viewport in VIEWPORTS for theme in THEMES)


@pytest.mark.parametrize(("viewport", "theme"), list(_cells()))
async def test_every_workspace_holds_the_layout_contract(viewport: str, theme: str, page_at: Any, browser_dsn: str) -> None:
    """Every rail destination, at this width and theme, in every populated/empty/degraded state.

    One browser per cell walking fourteen workspaces, not one browser per workspace: a
    browser-per-cell-per-workspace shape would launch 252 Chromium instances to make the same
    measurements. The page is reused across states because a state change is a database write the
    next navigation picks up, not a browser-level fact.
    """
    results: list[dict[str, Any]] = []
    violations: list[str] = []
    reproduced: set[tuple[str, str]] = set()
    try:
        async with page_at(viewport=viewport, theme=theme) as page:
            # Registered ONCE and cleared per visit. Re-registering inside the loop would stack a
            # fresh listener per navigation, so a single console error would be reported as many
            # times as there had been visits before it -- noise that reads like a worse defect.
            errors: list[str] = []
            _collect_console(page, errors)
            for state in ("empty", "populated", "degraded"):
                await _apply_state(browser_dsn, state)
                for stage in WORKSPACES:
                    errors.clear()
                    await _open(page, f"/s/{stage}")
                    probe = await _probe(page)
                    violations += _check_cell(
                        stage=stage, state=state, viewport=viewport, theme=theme, probe=probe, errors=list(errors), known=reproduced
                    )
                    results.append({"stage": stage, "state": state, **probe})
    finally:
        await _seed.reset(browser_dsn)

    # Printed BEFORE the assertion, so a failing cell still reports what it managed to exercise.
    # `-s` surfaces it; a normal run swallows it.
    tables = sum(r["tables"] for r in results)
    controls = sum(r["iconOnly"] for r in results)
    backgrounds = {r["background"] for r in results}
    print(
        f"[matrix] {viewport}/{theme}: {len(results)} workspace visits, {tables} visible tables, "
        f"{controls} icon-only controls, body background(s) {sorted(backgrounds)}"
    )

    # A KNOWN_OVERFLOW entry for THIS viewport that did not reproduce is a stale suppression: the
    # defect it excuses is gone, and leaving the entry would silently absolve the next regression at
    # the same pair. Fixing phaze-mrg1c therefore requires deleting its entries, which is the point.
    stale = {pair for pair in KNOWN_OVERFLOW if pair[1] == viewport} - reproduced
    if stale:
        violations.append(f"KNOWN_OVERFLOW entries no longer reproduce and must be deleted: {sorted(stale)}")

    assert not violations, f"{len(violations)} violation(s) at {viewport}/{theme}:\n  " + "\n  ".join(violations)


async def _painted_surface(page_at: Any, viewport: str, theme: str) -> dict[str, Any]:
    async with page_at(viewport=viewport, theme=theme) as page:
        await _open(page, "/s/summary")
        return dict(
            await page.evaluate(
                """() => {
                    const read = (el) => getComputedStyle(el);
                    const header = document.querySelector('header');
                    return {
                        dark: document.documentElement.classList.contains('dark'),
                        body: read(document.body).backgroundColor,
                        text: read(document.body).color,
                        header: header ? read(header).backgroundColor : null,
                    };
                }"""
            )
        )


@pytest.mark.parametrize("viewport", list(VIEWPORTS))
async def test_the_two_themes_are_actually_two_different_renderings(viewport: str, page_at: Any) -> None:
    """The dark theme paints -- proved by comparing the two themes, not by inspecting one.

    Without this, every dark-theme assertion in this module is satisfiable by a page that never left
    the light theme: the layout probes would pass identically and the matrix would report "dark
    validated" over a light render. The comparison is made across two real page loads rather than by
    toggling ``.dark`` in place, because an in-place toggle proves only that a class name has a rule
    attached -- it says nothing about whether the pinning path (localStorage -> the pre-flash IIFE in
    shell.html -> ``documentElement.classList``) actually gets there on a cold load, which is the
    half that can break.
    """
    light = await _painted_surface(page_at, viewport, "light")
    dark = await _painted_surface(page_at, viewport, "dark")

    assert light["dark"] is False and dark["dark"] is True, f"{viewport}: the pinned theme did not reach documentElement ({light=}, {dark=})"
    assert light["body"] != dark["body"], f"{viewport}: both themes paint the same body background {light['body']} -- the dark theme is not rendering"
    assert light["text"] != dark["text"], f"{viewport}: both themes paint the same body text colour {light['text']}"
    assert light["header"] != dark["header"], f"{viewport}: both themes paint the same header background {light['header']}"
    print(f"[theme] {viewport}: light body={light['body']} text={light['text']} | dark body={dark['body']} text={dark['text']}")


@pytest.mark.parametrize("viewport", list(VIEWPORTS))
async def test_the_shell_survives_a_slow_workspace_fetch(viewport: str, page_at: Any, browser_dsn: str) -> None:
    """The LOADING state: the layout contract holds while a rail swap is still in flight.

    Every earlier check measured a settled page. A workspace mid-swap is a different DOM -- the old
    content is gone, htmx indicators are showing, and the stage container is empty -- and it is the
    state an operator on a slow link spends the most time looking at. Delaying the fragment response
    rather than mocking it keeps the real request/response path intact.
    """
    try:
        await _apply_state(browser_dsn, "populated")
        async with page_at(viewport=viewport, theme="dark") as page:
            errors: list[str] = []
            _collect_console(page, errors)
            await _open(page, "/s/summary")

            async def _slow(route: Any) -> None:
                # asyncio.sleep, not page.wait_for_timeout: the latter evaluates in the page, and the
                # page is exactly what is blocked on this route -- it deadlocks until the timeout.
                await asyncio.sleep(0.9)
                await route.continue_()

            await page.route("**/s/files*", _slow)
            if VIEWPORTS[viewport]["width"] < _LG_BREAKPOINT_PX:
                await page.click("#rail-drawer-trigger")
                await page.locator("#rail-drawer").wait_for(state="visible")
            await page.locator('a[data-rail-stage="files"]').first.click(no_wait_after=True)

            # Measured WHILE the fetch is outstanding: the request is held for 900ms, so a probe
            # taken now describes the in-flight frame, not the settled one.
            await page.wait_for_timeout(250)
            probe = await _probe(page)
            assert probe["overflow"] <= 1, f"{viewport}: the document scrolls sideways by {probe['overflow']}px during a workspace fetch"
            assert not probe["unnamed"], f"{viewport}: icon-only control(s) lose their accessible name mid-swap: {probe['unnamed']}"
            assert probe["dark"] is True, f"{viewport}: the theme was dropped during a swap"
            assert not errors, f"{viewport}: console errors during a slow fetch {errors}"
    finally:
        await _seed.reset(browser_dsn)


@pytest.mark.parametrize("viewport", list(VIEWPORTS))
async def test_a_failed_refresh_renders_its_alert_without_breaking_the_layout(viewport: str, page_at: Any, browser_dsn: str) -> None:
    """The ERROR state: the Compute pane's ``role=alert`` refresh-failure banner.

    This is the product's own error surface -- shell.html records a failed
    ``#agents-table-section`` swap into ``localStorage['phaze:agents:lastError']`` and
    admin/partials/agents_table.html renders a red ``role=alert`` from it. Driving the real
    storage key exercises the real banner instead of asserting a template string, and the banner is
    the widest single element the pane can grow, so it is also the most likely to overflow a tablet.
    """
    try:
        await _apply_state(browser_dsn, "populated")
        async with page_at(viewport=viewport, theme="dark") as page:
            await page.add_init_script("try { localStorage.setItem('phaze:agents:lastError', new Date().toISOString()); } catch (e) {}")
            errors: list[str] = []
            _collect_console(page, errors)
            await _open(page, "/s/agents")

            alert = page.locator('[role="alert"]').first
            await alert.wait_for(state="visible", timeout=10_000)
            probe = await _probe(page)
            assert probe["overflow"] <= 1, f"{viewport}: the error banner pushes the document sideways by {probe['overflow']}px"
            assert not probe["unnamed"], f"{viewport}: icon-only control(s) with no accessible name beside the error banner: {probe['unnamed']}"
            assert not errors, f"{viewport}: console errors while the error banner is up {errors}"
    finally:
        await _seed.reset(browser_dsn)
