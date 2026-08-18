"""phaze-tzy6s.14 (CR-14-2): the Analyze workspace's lane detail pane and its own-tick polling.

``/s/analyze`` was loaded once by the all-workspace console sweep and no lane was ever opened. The
detail pane (``pipeline/partials/_detail_pane.html``) is the densest piece of runtime behaviour in
the product and none of it survives a server-side reading:

* opening it is a click that swaps a body into ``#detail-pane`` and, through an ``hx-on::after-swap``
  hook, calls an Alpine method that flips the pane open, parks focus and records the selection;
* the swapped body then carries its OWN ``hx-trigger="every 5s"``, so the pane refreshes itself from
  an element that did not exist when the page was rendered;
* every one of those ticks re-fires the PARENT's after-swap handler, which is why the open path has
  to distinguish a first open from a refresh -- refocusing on each tick would rip focus off the
  operator's ✕ Close or the ⌘K palette (the pane's own docstring says so);
* dismissal wipes the target's innerHTML, which is also what STOPS the poll (htmx cancels an
  ``every 5s`` trigger when its element leaves the DOM), rewrites the URL without a navigation, and
  returns focus by stable id because the 5s lane-grid poll may have replaced the trigger node.

A lane always exists once the workspace itself does: with no ``backends.toml`` the registry
synthesizes one implicit ``local`` backend, so ``#lane-trigger-local`` is the single card in the
grid. The workspace has to be reached first, though -- ``shell._render_stage`` swaps ``/s/analyze``
to a first-run empty-state partial when ``_analyze_file_count`` is zero, and that partial contains
neither the lane grid nor the detail pane. Every test here therefore seeds one file: not for the
lane's data (the lane is config-derived and reads the same either way) but to get past that fork.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.helpers import open_shell, settled, settled_focus


pytestmark = pytest.mark.browser


_LANE = "local"
_TRIGGER = f"#lane-trigger-{_LANE}"
_PANE_SECTION = 'section[aria-labelledby="detail-pane-heading"]'


def _watch_lane_fetches(page: Any) -> list[str]:
    fetched: list[str] = []
    page.on("request", lambda request: fetched.append(request.url) if f"/pipeline/lanes/{_LANE}" in request.url else None)
    return fetched


async def _reach_analyze(page: Any, seed: Any) -> None:
    """Seed past the first-run fork, then open the Analyze workspace.

    The file is a precondition, not data under test: with an empty corpus ``/s/analyze`` renders the
    first-run guide instead of the dashboard, and the lane grid this module drives does not exist on
    it at all.
    """
    await seed.file(filename="<track-01>.mp3")
    await open_shell(page, "/s/analyze")
    await settled(page)


async def _open_lane(page: Any) -> None:
    await page.click(_TRIGGER)
    await page.wait_for_selector(f'#detail-pane [data-lane-id="{_LANE}"]')
    await page.wait_for_function(
        """sel => document.querySelector(sel).className.includes('translate-x-0')""",
        arg=_PANE_SECTION,
    )


async def test_opening_a_lane_slides_the_detail_pane_in_and_deep_links_it(page: Any, seed: Any) -> None:
    """One click has to drive a swap, an Alpine state flip, a focus move and a history push.

    The pane's ``open`` state is owned by ``onLoaded()``, which htmx invokes through
    ``hx-on::after-swap`` -- so nothing opens unless the response actually landed, and the visible
    result is a CSS transform (``translate-x-full`` to ``translate-x-0``) rather than a rendered
    attribute. The server sends the same lane body either way.

    Focus parking is asserted because it is the pane's stated keyboard contract and because the
    heading is ``tabindex="-1"``: it is reachable only by that scripted ``focus()`` call, so if the
    call is lost the pane is unreachable from the keyboard while still looking correct.
    """
    await _reach_analyze(page, seed)

    assert await page.locator(_TRIGGER).count() == 1, "the implicit local lane did not render a trigger"
    assert "No lane selected" in await page.locator(_PANE_SECTION).inner_text(), "the pane is not resting on its empty state before a selection"

    await _open_lane(page)

    assert await page.evaluate("document.activeElement.id") == "detail-pane-heading", (
        "opening a lane did not park focus on the pane heading — a keyboard operator is left on the grid behind it"
    )
    assert f"lane={_LANE}" in page.url, "the open lane was not pushed to the URL, so the pane cannot be linked to or restored"
    assert "No lane selected" not in await page.locator(_PANE_SECTION).inner_text(), "the resting empty state is still showing over the open pane"

    # D-02 poll survival. The click does NOT mark the trigger current -- the lane grid is re-rendered
    # wholesale by the shell's 5s stats poll, which re-resolves the pushed `?lane` and re-emits the
    # selection ring. So the highlight appears one tick later and, more to the point, has to SURVIVE
    # every subsequent tick: a poll that dropped the parameter would silently un-select the lane the
    # operator is looking at, leaving the pane open next to a grid that says nothing is selected.
    # Only a browser can watch a poll land on top of a selection.
    await page.wait_for_function("""sel => document.querySelector(sel)?.getAttribute('aria-current') === 'true'""", arg=_TRIGGER, timeout=15_000)


async def test_the_open_lane_detail_refreshes_itself_without_stealing_focus_back(page: Any, seed: Any) -> None:
    """The own-tick: a fragment that arrived by swap goes on to poll ITSELF, and must not grab focus.

    Two claims, and both need a browser. The first is that the poll exists at all -- it is declared
    on an element the initial page never contained, so only a live document can show that htmx
    processed the swapped-in subtree and armed the trigger.

    The second is the subtle one the pane's docstring calls out: every own-tick swap re-fires the
    PARENT ``#detail-pane``'s ``hx-on::after-swap``, so ``onLoaded()`` runs again on each refresh.
    It captures the pre-swap ``open`` state and only parks focus on a closed-to-open transition. If
    that guard is lost, the pane yanks focus back to its heading every five seconds -- off the ✕
    Close, off the ⌘K palette, off whatever the operator was doing. There is no rendered difference
    between the guarded and unguarded versions; the only way to see it is to hold focus somewhere
    else and watch a tick go by.
    """
    await _reach_analyze(page, seed)
    await _open_lane(page)

    fetched = _watch_lane_fetches(page)
    await page.focus('button[aria-label="Close detail"]')

    # A fixed wait, unusually for this suite: the assertion is that a 5s interval FIRED, and there
    # is no post-condition to poll for that is not the thing under test. 7s is one tick plus slack.
    await page.wait_for_timeout(7000)

    assert fetched, "the open lane detail never refreshed itself — the own-tick trigger was not armed on the swapped-in body"
    assert await page.locator(f'#detail-pane [data-lane-id="{_LANE}"]').count() == 1, (
        f"the own-tick left {await page.locator('#detail-pane [data-lane-id]').count()} lane bodies mounted — the refresh nested instead of replacing"
    )
    assert await page.evaluate("document.activeElement.getAttribute('aria-label')") == "Close detail", (
        "a routine 5s refresh moved focus back to the pane heading, tearing it off the control the operator was on"
    )
    assert "translate-x-0" in (await page.get_attribute(_PANE_SECTION, "class") or ""), "the pane closed itself on its own refresh"


async def test_dismissing_the_lane_detail_stops_its_poll_and_returns_focus(page: Any, seed: Any) -> None:
    """Esc has to undo all four things opening did — including the one with no visible trace.

    Clearing ``#detail-pane``'s innerHTML is not tidiness: the own-tick trigger lives on the swapped
    body, so removing the body is what CANCELS the poll. A dismiss that only hid the pane would leave
    a request every five seconds for the life of the tab, invisible on screen and unfalsifiable from
    the server. Counting requests made AFTER the dismiss is the only way to state it.

    The focus return goes by stable id rather than by a captured element handle, because the lane
    grid's own 5s poll replaces the trigger node wholesale; a handle captured on open would be
    detached by the time Esc is pressed. That is a browser-lifetime property with no server analogue.
    """
    await _reach_analyze(page, seed)
    await _open_lane(page)

    await page.keyboard.press("Escape")
    await page.wait_for_function("() => document.getElementById('detail-pane').innerHTML.trim() === ''")

    focused = await settled_focus(page)
    assert focused == f"lane-trigger-{_LANE}", (
        f"Esc left focus on {focused!r} instead of the lane trigger that opened the pane — the keyboard operator is stranded"
    )
    assert page.url.endswith("/s/analyze"), f"dismissing the pane left the ?lane deep link in the URL: {page.url}"
    assert "No lane selected" in await page.locator(_PANE_SECTION).inner_text(), (
        "the resting empty state did not come back — the ✕-only-disappears trap (CONSOLE-03)"
    )

    after_dismiss = _watch_lane_fetches(page)
    await page.wait_for_timeout(7000)
    assert after_dismiss == [], f"the dismissed pane is still polling its lane every 5s: {after_dismiss}"


async def test_a_deep_linked_lane_opens_the_pane_on_a_cold_load(page: Any, seed: Any) -> None:
    """phaze-m662: a bookmarked ``?lane=`` has to open the pane, and only after its body has landed.

    The server resolves the query parameter and emits a one-shot ``hx-trigger="load"`` self-fetch,
    but ``open`` is deliberately NOT set by ``x-init``: on a cold load nothing has swapped a body in
    yet, so flipping ``open`` there suppressed the empty state and showed a ✕ over a BLANK pane that
    never refreshed. The fix routes the deep link through the SAME ``onLoaded()`` the click uses.

    This is a pure ordering property between a server-emitted trigger and a client-side state
    machine. The markup for the working and the broken version differs by one attribute nobody can
    evaluate without running it.
    """
    await seed.file(filename="<track-01>.mp3")

    await page.goto(f"/s/analyze?lane={_LANE}", wait_until="domcontentloaded")
    await page.wait_for_selector("#stage-workspace", state="attached")
    await page.wait_for_function("() => window.Alpine !== undefined && window.htmx !== undefined")

    await page.wait_for_selector(f'#detail-pane [data-lane-id="{_LANE}"]')
    await page.wait_for_function("""sel => document.querySelector(sel).className.includes('translate-x-0')""", arg=_PANE_SECTION)

    assert await page.locator('button[aria-label="Close detail"]').is_visible(), "the deep-linked pane offers no way to close itself"
    assert await page.get_attribute(_TRIGGER, "aria-current") == "true", "a deep-linked lane is open but its trigger is not marked current"

    # The deep-linked pane must be as dismissible as a clicked one — same handler, same restore.
    await page.keyboard.press("Escape")
    await page.wait_for_function("() => document.getElementById('detail-pane').innerHTML.trim() === ''")
    assert page.url.endswith("/s/analyze"), "dismissing a deep-linked pane left the parameter that would re-open it on reload"
