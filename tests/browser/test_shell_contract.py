"""phaze-tzy6s.14: the unified interaction contract, asserted in a real browser.

Every test here asserts something the server-side suite **structurally cannot**. The rule applied
when choosing them: if an httpx test could prove it from the markup, it does not belong here. What
is left is behaviour that only exists once a browser has run the JavaScript -- htmx swapping,
history restoring, focus moving, storage persisting, and layout actually overflowing.
"""

from __future__ import annotations

from typing import Any

import pytest


pytestmark = pytest.mark.browser


async def _open(page: Any, path: str = "/s/summary") -> None:
    """Navigate and wait until the shell is actually interactive.

    Deliberately NOT ``wait_until="networkidle"``. The shell holds a 5s stats poll and opens SSE
    streams, so the network is never idle and that wait blocks until the timeout -- the first run of
    this suite hung on exactly that. Readiness here means the two libraries the contract depends on
    have booted and the swap target exists; anything finer is waited for per test.
    """
    await page.goto(path, wait_until="domcontentloaded")
    await page.wait_for_selector("#stage-workspace", state="attached")
    await page.wait_for_function("() => window.Alpine !== undefined && window.htmx !== undefined")


async def _goto_shell(page: Any) -> None:
    await _open(page)


async def _wait_for_stage(page: Any, document_title: str) -> None:
    """Wait for an htmx rail swap to land.

    NOT ``#stage-workspace``'s own ``data-stage``: the rail swaps with ``hx-swap="innerHTML"``, so
    the container element -- and every attribute on it -- survives untouched and that value is
    frozen at whatever the initial full-page render set. Polling it waits forever on a swap that
    already succeeded. The fragment (``shell/_stage_fragment.html``) carries a
    ``data-document-title`` marker precisely because it is content-only; that marker is what
    actually changes, so it is what a swap assertion must read.
    """
    await page.wait_for_function(
        """expected => {
            const marker = document.querySelector('#stage-workspace [data-document-title]');
            return marker !== null && marker.dataset.documentTitle === expected;
        }""",
        arg=document_title,
    )


# --- Shell navigation, htmx swaps and history ---------------------------------------------


async def test_rail_navigation_swaps_the_workspace_without_a_full_page_load(page: Any) -> None:
    """A rail click is an htmx swap, not a navigation: the document survives, the workspace changes.

    The markup says ``hx-target="#stage-workspace"``. Only a browser can show that htmx honoured it
    -- and that the shell around it was not re-created, which is the property the whole
    swap-target design exists to provide.
    """
    await _goto_shell(page)
    await page.evaluate("window.__shellAlive = true")

    await page.click('a[data-rail-stage="files"]')
    await _wait_for_stage(page, "Files")

    assert await page.evaluate("window.__shellAlive === true"), "the document was replaced — this was a full load, not an htmx swap"
    assert "/s/files" in page.url, "hx-push-url did not update the address bar"


async def test_back_and_forward_restore_the_matching_workspace(page: Any) -> None:
    """Browser history restores the right stage, not a stale or wrongly-shaped response.

    The retrospective's ``Vary: HX-Request`` family lives precisely here: a cached fragment restored
    into a full-page navigation (or the reverse) yields a document that looks subtly wrong and
    passes every string assertion on the server side.
    """
    await _goto_shell(page)
    await page.click('a[data-rail-stage="files"]')
    await _wait_for_stage(page, "Files")
    await page.click('a[data-rail-stage="dedupe"]')
    await _wait_for_stage(page, "Duplicates")

    await page.go_back()
    await _wait_for_stage(page, "Files")

    await page.go_forward()
    await _wait_for_stage(page, "Duplicates")

    # A restored document must still be a working shell, not an orphaned fragment.
    assert await page.locator('aside[aria-label="Pipeline navigation"]').count() == 1, "history restore produced a fragment, not the shell"


async def test_focus_is_not_dropped_to_the_body_after_a_swap(page: Any) -> None:
    """Focus continuity across an htmx swap — invisible to markup assertions.

    If focus falls back to ``<body>`` a keyboard user restarts from the top of the document on every
    navigation, which makes the rail unusable without a mouse.
    """
    await _goto_shell(page)
    await page.focus('a[data-rail-stage="files"]')
    await page.keyboard.press("Enter")
    await _wait_for_stage(page, "Files")

    focused = await page.evaluate("document.activeElement === document.body ? 'body' : document.activeElement.tagName")
    assert focused != "body", "focus was dropped to <body> after the swap"


# --- Responsive navigation (the phaze-tzy6s.13 contract, verified for real) ------------------


async def test_phone_navigation_is_a_drawer_not_a_permanent_icon_rail(phone_page: Any) -> None:
    """At phone width the rail is off-canvas and the header trigger opens it.

    This is the .13 acceptance criterion. The structural guard proves the classes are present; only
    a browser proves the computed layout actually hides the rail and that the trigger reveals it.
    """
    await _open(phone_page, "/s/summary")

    rail = phone_page.locator('aside[aria-label="Pipeline navigation"]')
    assert not await rail.is_visible(), "the rail is still mounted on a phone — this is the icon-rail defect"

    trigger = phone_page.locator("#rail-drawer-trigger")
    assert await trigger.is_visible(), "no drawer trigger at phone width"
    await trigger.click()
    await rail.wait_for(state="visible")

    # The labels are real visible text, not sr-only or a title tooltip.
    label = rail.locator('a[data-rail-stage="rename"] span', has_text="Changes Review").first
    assert await label.is_visible(), "destination labels are not visible in the drawer"


async def test_the_drawer_announces_its_open_state_and_modality_at_phone_width(phone_page: Any) -> None:
    """phaze-tzy6s.17 (CR-13-1): aria-expanded tracks the drawer, and the open drawer is a dialog.

    The structural guard can only prove the attributes are WRITTEN. Whether aria-expanded actually
    flips, and whether the conditional role resolves to "dialog" at this width, are runtime facts
    about Alpine's bindings -- exactly the class of claim .14 exists to check in a real browser
    rather than against a template string.

    The close path is asserted via Escape specifically because it is the one that does not involve
    the trigger at all: a trigger tracking its own clicks would be left announcing "expanded" over a
    drawer that is no longer there.
    """
    await _open(phone_page, "/s/summary")

    trigger = phone_page.locator("#rail-drawer-trigger")
    rail = phone_page.locator("#rail-drawer")

    assert await trigger.get_attribute("aria-expanded") == "false", "closed drawer must not announce itself as expanded"

    await trigger.click()
    await rail.wait_for(state="visible")
    assert await trigger.get_attribute("aria-expanded") == "true", "aria-expanded did not follow the drawer open"
    assert await rail.get_attribute("role") == "dialog", "a focus-trapped overlay must resolve to role=dialog at phone width"
    assert await rail.get_attribute("aria-modal") == "true", "the open drawer traps focus, so it must set aria-modal"

    await phone_page.keyboard.press("Escape")
    await rail.wait_for(state="hidden")
    assert await trigger.get_attribute("aria-expanded") == "false", "Escape closed the drawer but the trigger still says expanded"


async def test_closed_drawer_contributes_no_tab_stops(phone_page: Any) -> None:
    """The closed drawer must be untabbable — the assertion that catches a translate-only drawer.

    A drawer moved off-screen with ``transform`` alone still holds its tab stops, so a keyboard user
    tabs through sixteen invisible destinations before reaching the workspace. That is impossible to
    see in a screenshot and impossible to assert from markup; it needs a real focus walk.
    """
    await _open(phone_page, "/s/summary")

    # `offsetParent !== null` is NOT the probe: it is non-null for a visibility:hidden element
    # (only display:none nulls it), so it reports every destination as reachable and says nothing
    # about tabbability. Computed visibility is the property that actually governs tab order.
    state = await phone_page.evaluate(
        """() => {
            const rail = document.querySelector('aside[aria-label="Pipeline navigation"]');
            const links = [...rail.querySelectorAll('a[data-rail-stage]')];
            return {
                railVisibility: getComputedStyle(rail).visibility,
                visibleLinks: links.filter(el => getComputedStyle(el).visibility !== 'hidden').length,
                total: links.length,
            };
        }"""
    )
    assert state["total"] >= 14, f"expected the full destination set, found {state['total']}"
    assert state["railVisibility"] == "hidden", (
        f"the closed drawer computes visibility:{state['railVisibility']} — a transform-only drawer keeps "
        "its tab stops, so a keyboard user tabs through every off-screen destination first"
    )
    assert state["visibleLinks"] == 0, f"{state['visibleLinks']} rail destinations remain focusable while the drawer is closed"

    # And it genuinely becomes reachable once opened — otherwise "hidden" would be trivially satisfiable.
    await phone_page.click("#rail-drawer-trigger")
    await phone_page.wait_for_function(
        """() => getComputedStyle(document.querySelector('aside[aria-label="Pipeline navigation"]')).visibility === 'visible'"""
    )


async def test_the_page_never_scrolls_sideways_on_a_phone(phone_page: Any) -> None:
    """Wide tables scroll inside themselves; the document does not.

    A horizontally scrolling page drags the header and rail out of view and is the single most
    common way a dense admin UI becomes unusable on touch. It is a computed-layout property — no
    amount of class assertion establishes it.
    """
    for stage in ("files", "rename", "apply", "audit"):
        await _open(phone_page, f"/s/{stage}")
        overflow = await phone_page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"/s/{stage} overflows the viewport horizontally by {overflow}px"


# --- Command palette, focus return -----------------------------------------------------------


async def test_command_palette_opens_traps_and_returns_focus(page: Any) -> None:
    """⌘K opens the palette, Escape closes it, and focus returns to the trigger."""
    await _goto_shell(page)

    await page.click("#cmdk-trigger")
    dialog = page.locator("#cmdk-dialog")
    await dialog.wait_for(state="visible")

    await page.keyboard.press("Escape")
    await dialog.wait_for(state="hidden")

    returned = await page.evaluate("document.activeElement && document.activeElement.id")
    assert returned == "cmdk-trigger", f"focus went to {returned!r} instead of back to the palette trigger"


# --- Theme persistence -------------------------------------------------------------------------


async def test_theme_choice_survives_a_reload(page: Any) -> None:
    """The theme toggle persists — a storage round-trip no server-side test can observe."""
    await _goto_shell(page)
    before = await page.evaluate("Alpine.store('theme').mode")

    await page.click('button[aria-label="Toggle theme"]')
    chosen = await page.evaluate("Alpine.store('theme').mode")
    assert chosen != before, "the theme toggle did not change the mode"

    await page.reload(wait_until="domcontentloaded")
    await page.wait_for_function("() => window.Alpine !== undefined")
    after = await page.evaluate("Alpine.store('theme').mode")
    assert after == chosen, f"theme did not persist: chose {chosen!r}, got {after!r} after reload"


# --- Execute: the disabled state and the confirmation boundary ---------------------------------


async def test_execute_explains_its_disabled_state_in_visible_text(page: Any) -> None:
    """With nothing approved, Execute is inert and says why AND what to do next, on screen.

    An empty-state test in the browser rather than the template, because the criterion is that the
    operator can READ the reason: a ``title=`` tooltip satisfies a markup assertion and satisfies
    nobody on a touch device.
    """
    await _open(page, "/s/apply")

    button = page.locator('button[aria-label^="Review and execute"]')
    assert await button.is_disabled(), "Execute is enabled with nothing approved"

    reason = page.locator("#apply-blocked-reason")
    assert await reason.is_visible(), "the disabled reason is not visible on screen"
    text = await reason.inner_text()
    assert "No approved proposals are ready to execute." in text
    assert "Approve filename and destination changes in Changes Review first." in text, "no next action offered"


async def test_execute_always_states_the_scope_it_will_not_dispatch(page: Any) -> None:
    """The scope statement renders whatever the queue holds -- including empty.

    phaze-tzy6s.17 (CR-14-4) rewrote this test. It was
    ``test_execute_never_dispatches_from_a_native_browser_prompt``, and it could not fail:

    * It never clicked anything, so no dialog of any kind -- native or in-product -- could fire.
    * It could not have clicked anyway. ``apply_workspace.html`` gates the whole confirmation
      dialog behind ``{% if preflight.can_execute %}``, and ``can_execute = total > 0 and not
      conflicts``; the neighbouring test asserts the Execute button ``is_disabled()``, which proves
      ``can_execute`` is False. ``#apply-confirm`` was therefore absent from the DOM.
    * So ``assert dialogs == []`` could not distinguish "the correct in-product dialog opened"
      from "there is no confirmation control here at all" -- and it read as coverage of the
      product's final safety boundary, which is the worst place to hold a vacuous assertion.

    The invariant it claimed is real and IS properly guarded, one layer down and with the seeded
    state this suite lacks: ``tests/shared/core/test_review_apply_workspaces.py``'s
    ``test_apply_workspace_hosts_the_execute_trigger`` seeds an approved proposal so ``can_execute``
    is True, then asserts ``hx-confirm`` is absent while ``#apply-confirm`` / ``<dialog>`` /
    ``showModal()`` are present. Nothing was lost by deleting the browser copy.

    A browser-level version -- click Execute, assert the in-product dialog opens and NO native
    prompt fires -- needs a seeded ``can_execute`` state, i.e. browser-suite data seeding, which is
    deliberately out of scope here and belongs to the rescoped browser-coverage bead. What remains
    below is what this suite can actually verify in the empty state, and it is not vacuous: the
    scope statement is asserted to render unconditionally, which is precisely the claim that an
    "only show it when there is something to show" refactor would break.

    inner_text() returns text as RENDERED, and the eyebrow headings carry ``uppercase``, so a
    case-sensitive match would fail on styling rather than on content. Compare case-insensitively.
    """
    await _open(page, "/s/apply")

    body = (await page.locator("#stage-workspace").inner_text()).lower()
    assert "not dispatched by this control" in body
    assert "execute approved runs approved filename and destination changes only." in body


# --- Workspaces render their empty / populated states in a browser -----------------------------


@pytest.mark.parametrize(
    "stage",
    [
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
    ],
)
async def test_every_workspace_renders_without_a_console_error(page: Any, stage: str) -> None:
    """Every workspace boots clean in a real browser.

    Alpine binding errors, htmx target misses and undefined-store reads are all silent server-side:
    the template renders, the assertion passes, and the page is broken. CONSOLE-02 (a rail badge
    reading 0 while 2,183 jobs were in flight) was exactly this, and only a browser surfaced it.
    """
    errors: list[str] = []
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    await _open(page, f"/s/{stage}")

    assert await page.locator("#stage-workspace").count() == 1, f"/s/{stage} did not render the workspace host"
    assert errors == [], f"/s/{stage} raised browser errors:\n  " + "\n  ".join(errors)
