"""phaze-tp52f: the Changes Review facet tabs must ship the selection they DISPLAY.

``_changes_list.html``'s ``sync()`` rewrites each ``[data-changes-nav]`` element's ``hx-get`` (and
``#changes-bulk-form``'s ``hx-patch``) to carry ``selected=<ids>`` whenever the operator ticks or
unticks a row. The attribute genuinely changes -- reading it back shows the new value -- and until
this bead that was the ONLY thing that changed. htmx 2.0.10 reads a verb's path once inside
``processVerbs`` and closes over the string::

    const path = getAttributeValue(elt, 'hx-' + verb)
    addTriggerHandler(elt, spec, nodeData, function (node, evt) {
        issueAjaxRequest(verb, path, elt, evt)   // the captured string, never re-read
    })

so the tab requested the URL it was SERVER-RENDERED with, not the URL ``sync()`` wrote.

Why every test here reads the WIRE
==================================

An assertion on the attribute proves nothing about this defect: the attribute was always correct.
Every test below captures the real request through Playwright's ``request`` event -- the URL the
browser actually put on the socket -- and compares it against the attribute the DOM was claiming at
the moment of the click. That comparison is the whole point; a test that only read one of the two
would have passed for the entire lifetime of the bug.

``htmx:configRequest`` would have been the cheaper hook and is deliberately not used: it reports
what htmx INTENDS, which is one layer above the thing in doubt.

The consequence these tests pin down
====================================

Measured before the fix, on a seeded two-row list:

* Select a row, switch tab -> the wire carried ``selected=`` (empty) and the selection was silently
  dropped. A narrowing; annoying, harmless to the archive.
* DESELECT a row, switch tab -> the wire carried the stale id, and the list came back with the row
  RE-CHECKED and "Approve selected eligible" RE-ENABLED. That is the widening, and it is sticky:
  each swap re-renders the tabs from the stale value it was just handed, so unchecking again and
  switching again resurrects it again. Three consecutive rounds measured (see
  :func:`test_deselecting_a_row_and_switching_tabs_does_not_resurrect_it`); the loop only breaks
  when the destination tab does not contain the row at all, which is luck rather than intent.

No route scopes a WRITE from ``selected=``. ``PATCH /proposals/bulk`` scopes from ``review_tokens``
/ ``proposal_ids`` in the request BODY, which htmx serializes from the live DOM at request time, and
the bulk tag write scopes from server-rendered hidden ``review_tokens`` that never mention
``selected`` at all. The stale URL therefore cannot widen a write directly; it widens one
indirectly, because the checkbox it resurrects IS the bulk scope -- and the resurrected state is
rendered on screen before the operator's next click, which is what keeps this a discarded-intent
defect rather than a silent commit.

Barriers
========

``settled()`` is not usable here (helpers.py's ``swap_settles`` docstring has the general form): it
returns once no request is in flight, which right after a click is usually BEFORE the request has
started, and always before htmx's deferred settle task has run. Every navigation below goes through
``swap_settles``, and every read of a post-swap DOM state waits on an explicit predicate.
"""

from __future__ import annotations

from typing import Any

import pytest

from phaze.models.proposal import ProposalStatus
from tests.browser.helpers import open_shell, settled, swap_settles


pytestmark = pytest.mark.browser


_APPROVE = "button[value='approve_eligible']"
_ALL_TAB = "a[data-changes-nav]:has-text('All')"


def _watch_wire(page: Any, needle: str) -> list[str]:
    """Record every request URL containing ``needle``, in issue order.

    Playwright's ``request`` event, not ``htmx:configRequest``: the socket is the thing under test.
    """
    seen: list[str] = []
    page.on("request", lambda request: seen.append(request.url) if needle in request.url else None)
    return seen


async def _selection_synced(page: Any, expected: str) -> None:
    """Wait until ``sync()`` has finished writing ``expected`` into the nav attributes.

    The checkbox ``@change`` handler runs ``sync()`` synchronously, but Playwright's ``check()``
    returns on the input event, so reading the attribute straight afterwards is a race. Waiting on
    the attribute is safe precisely BECAUSE the attribute was never the broken half.
    """
    await page.wait_for_function(
        """want => [...document.querySelectorAll('[data-changes-nav]')].every(el => {
            const value = new URL(el.getAttribute('hx-get'), window.location.origin).searchParams.get('selected') || '';
            return value === want;
        })""",
        arg=expected,
        timeout=5_000,
    )


async def test_a_facet_tab_requests_the_selection_the_dom_is_advertising(page: Any, seed: Any) -> None:
    """AC1: the wire must carry what ``sync()`` wrote, and it did not.

    The DOM attribute is read back in the same test and asserted to hold the id -- not because that
    half was ever in doubt, but so the failure message can say "the DOM claimed X and the socket
    carried Y" rather than leaving a reader to wonder which of the two moved.
    """
    target = await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-02>.mp3")

    wire = _watch_wire(page, "/s/rename")

    await open_shell(page, "/s/rename")
    await settled(page)

    await page.get_by_label("Select <set-01>.mp3").check()
    await _selection_synced(page, str(target.id))

    advertised = await page.evaluate("() => document.querySelector(\"a[data-changes-nav][hx-get*='status=all']\").getAttribute('hx-get')")
    assert str(target.id) in advertised, f"sync() never rewrote the tab attribute, so this test proves nothing about the wire: {advertised!r}"

    wire.clear()
    async with swap_settles(page):
        await page.click(_ALL_TAB)

    assert len(wire) == 1, f"expected exactly one facet-tab request, got {wire}"
    assert str(target.id) in wire[0], f"the facet tab dropped the selection on the wire.\n  DOM advertised: {advertised}\n  socket carried: {wire[0]}"

    # And the round trip lands: the swapped-in list still shows the row selected.
    await page.wait_for_function(
        "id => { const el = document.querySelector(`input[aria-label='Select <set-01>.mp3']`); return el !== null && el.checked; }",
        arg=str(target.id),
        timeout=5_000,
    )


async def test_deselecting_a_row_and_switching_tabs_does_not_resurrect_it(page: Any, seed: Any) -> None:
    """The widening direction, and the reason this bead was not a cosmetic tab bug.

    Pre-fix this was STICKY, which is the property that makes it worse than a one-off glitch: the
    swap re-renders the tabs from the stale value it just shipped, so the next uncheck-and-switch
    resurrects the same row again. Three rounds are driven here rather than one because one round is
    also what a plain "selection is lost" bug looks like -- only the repetition distinguishes
    "discarded once" from "cannot be discarded at all", and only the repetition would fail if a
    future change fixed the first switch and not the ones after it.

    Each round asserts the operator's intent on THREE surfaces that the pre-fix code disagreed on:
    the wire (no stale id), the swapped-in checkbox (unchecked), and the bulk control (disabled,
    read from the browser's ``:disabled`` so a ``<fieldset disabled>`` ancestor counts -- see
    phaze-tckiy).
    """
    target = await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-02>.mp3")

    wire = _watch_wire(page, "/s/rename")

    # Arrive the way an operator arrives with a live selection: `sync()` mirrors every tick into the
    # address bar via replaceState, so a reload, a bookmark or a Back lands here.
    await open_shell(page, f"/s/rename?status=needs_review&selected={target.id}")
    await settled(page)
    assert await page.evaluate("() => document.querySelector(`input[aria-label='Select <set-01>.mp3']`).checked"), (
        "the seeded selection never arrived, so the deselect below would prove nothing"
    )

    for round_no, tab in enumerate(("All", "Needs Review", "All"), start=1):
        await page.get_by_label("Select <set-01>.mp3").uncheck()
        await _selection_synced(page, "")

        wire.clear()
        async with swap_settles(page):
            await page.click(f"a[data-changes-nav]:has-text('{tab}')")

        assert len(wire) == 1, f"round {round_no}: expected exactly one facet-tab request, got {wire}"
        assert str(target.id) not in wire[0], (
            f"round {round_no} ({tab}): the operator unticked this row and the tab shipped it anyway — "
            f"the deselect is discarded on every switch, not just the first.\n  socket carried: {wire[0]}"
        )

        await page.wait_for_selector("input[aria-label='Select <set-01>.mp3']", timeout=5_000)
        assert not await page.evaluate("() => document.querySelector(`input[aria-label='Select <set-01>.mp3']`).checked"), (
            f"round {round_no} ({tab}): the unticked row came back CHECKED after the swap"
        )
        assert await page.evaluate("sel => document.querySelector(sel).matches(':disabled')", _APPROVE), (
            f"round {round_no} ({tab}): 'Approve selected eligible' is live against a selection the operator had cleared"
        )


async def test_the_pagination_links_carry_the_live_selection_too(page: Any, seed: Any) -> None:
    """``[data-changes-nav]`` is seven elements, not five: Previous/Next are the other two.

    They are rewritten by the same loop and were broken by the same closure, and they are the pair a
    facet-tab-only fix would leave behind. Seeded past the smallest ``PAGE_SIZE_CHOICES`` entry so a
    pager exists at all -- see the comment below for why the obvious ``?page_size=1`` does not work.
    """
    # PAGE_SIZE_CHOICES is (25, 50, 100) and a URL-borne page_size is CLAMPED to it, so a pager only
    # exists past 25 rows -- `?page_size=1` silently renders one un-paginated page and the Next link
    # never appears. Seeding 26 is the cheapest honest way to get the two elements under test.
    for index in range(26):
        await seed.proposal(status=ProposalStatus.PENDING, filename=f"<set-{index:02d}>.mp3")

    wire = _watch_wire(page, "/s/rename")

    await open_shell(page, "/s/rename?status=needs_review")
    await settled(page)
    await page.wait_for_selector("a[data-changes-nav]:has-text('Next')", timeout=5_000)

    label = await page.evaluate("() => document.querySelector('input[name=review_tokens]').getAttribute('aria-label')")
    await page.get_by_label(label).check()
    # Which row sorts first is the page's business, not this test's; the claim is about the link.
    on_screen = await page.evaluate(
        "() => new URL(document.querySelector('[data-changes-nav]').getAttribute('hx-get'), location.origin).searchParams.get('selected')"
    )
    assert on_screen, "nothing got selected, so the pager assertion below is vacuous"
    await _selection_synced(page, on_screen)

    wire.clear()
    async with swap_settles(page):
        await page.click("a[data-changes-nav]:has-text('Next')")

    assert len(wire) == 1, f"expected exactly one pager request, got {wire}"
    assert f"selected={on_screen}" in wire[0], f"the Next pager dropped the selection on the wire: {wire[0]}"


async def test_the_bulk_patch_carries_the_live_selection_and_still_names_its_action(page: Any, seed: Any) -> None:
    """``#changes-bulk-form``'s ``hx-patch`` is rewritten by the same ``sync()`` and re-processed too.

    Two claims in one test, because the second is the risk the first one creates.

    1. The PATCH URL carries the live selection. It seeds the RESPONSE's checkbox state
       (``build_changes_review_context`` reads ``selected`` from the query), so a stale one
       resurrects the selection in exactly the way the tabs did.
    2. The PATCH still names its ``action``. ``htmx.process`` re-inits the form, and ``deInitNode``
       wipes the form's internal data -- including ``lastButtonClicked``, the ONLY channel through
       which htmx sends a submit button's own ``name``/``value``. ``bulk_action`` declares
       ``action: str = Form(...)``, so losing it is a 422 rather than a subtle mis-scope: the toast
       below never appears. This is the assertion that says the re-processing is not paid for with a
       broken submit.

    The write itself is deliberately NOT scoped by ``selected``: ``bulk_action`` reads
    ``review_tokens`` from the body. That is why this test asserts the toast counts ONE approval
    while two rows exist -- proving the body-scoped write stayed narrow while the URL got wider.
    """
    target = await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-02>.mp3")

    wire = _watch_wire(page, "/proposals/bulk")

    await open_shell(page, "/s/rename")
    await settled(page)

    await page.get_by_label("Select <set-01>.mp3").check()
    await _selection_synced(page, str(target.id))
    advertised = await page.evaluate("() => document.querySelector('#changes-bulk-form').getAttribute('hx-patch')")
    assert str(target.id) in advertised, f"sync() never rewrote hx-patch: {advertised!r}"

    async with swap_settles(page):
        await page.click(_APPROVE)

    assert len(wire) == 1, f"expected exactly one bulk PATCH, got {wire}"
    assert str(target.id) in wire[0], (
        f"the bulk PATCH dropped the selection on the wire.\n  DOM advertised: {advertised}\n  socket carried: {wire[0]}"
    )

    # Scoped to the list container: the shell renders its own `[role=status]` live regions, and an
    # unscoped query picks up a stats counter instead of the bulk result.
    toast = await page.evaluate(
        """() => {
            const list = document.querySelector('#changes-bulk-form').closest('[x-data]');
            const el = list && list.querySelector('[role=status]');
            return el ? el.innerText : '';
        }"""
    )
    assert "1 proposal approved" in toast, (
        f"the bulk PATCH did not land as a one-row approval — a lost `action` field answers 422 and renders no toast at all: {toast!r}"
    )


async def test_the_guard_still_releases_exactly_once_across_repeated_syncs(page: Any, seed: Any) -> None:
    """AC4: what the re-processing does to phaze-tckiy's ``[data-hx-guard]`` release listener.

    The answer is *nothing*, and this asserts both halves of why.

    **It is never invoked.** ``htmx.process`` -> ``processNode`` fires ``htmx:beforeProcessNode`` and
    ``htmx:afterProcessNode``; in htmx 2.0.10 ``htmx:load`` comes only from ``makeAjaxLoadTask`` (the
    deferred settle task) and the one-shot boot trigger on ``document.body``. The counter below
    tallies real ``htmx:load`` events on the bulk form's subtree and must not move across ten
    ``sync()`` calls -- ten, not one, because the failure being excluded is a per-call one.

    **And if it were, it would be a no-op.** ``releaseGuards`` REMOVES ``data-hx-guard`` before it
    clears ``disabled``, so a second pass matches nothing. That is asserted directly by firing
    ``htmx:load`` at the form by hand while the business rule says the buttons must stay disabled:
    a listener that keyed on anything other than the consumed attribute would re-enable them here.
    Doing it by hand rather than by argument is the point -- "the listener is idempotent" is exactly
    the kind of claim this repo keeps shipping unverified.

    The two failure shapes named in the acceptance criterion are therefore both covered: a control
    left STUCK (the guard is confirmed released and the button reachable once a row is selected) and
    one left PREMATURELY ENABLED (the buttons stay ``:disabled`` with nothing selected, through ten
    syncs and a hand-fired ``htmx:load``).
    """
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")

    await open_shell(page, "/s/rename")
    await settled(page)

    # The guard must have been released by the normal path before any of this means anything.
    await page.wait_for_function(
        "sel => { const el = document.querySelector(sel); return el !== null && !el.hasAttribute('data-hx-guard'); }",
        arg=_APPROVE,
        timeout=5_000,
    )

    await page.evaluate(
        """() => {
            window.__loads = 0;
            document.querySelector('#changes-bulk-form').addEventListener('htmx:load', () => { window.__loads++; });
        }"""
    )

    # Ten syncs: tick and untick five times. Each one rewrites hx-patch and re-processes the form.
    for _ in range(5):
        await page.get_by_label("Select <set-01>.mp3").check()
        await _selection_synced(
            page,
            await page.evaluate(
                "() => document.querySelector('input[name=review_tokens]').checked ? new URL(document.querySelector('[data-changes-nav]').getAttribute('hx-get'), location.origin).searchParams.get('selected') : ''"
            ),
        )
        await page.get_by_label("Select <set-01>.mp3").uncheck()
        await _selection_synced(page, "")

    assert await page.evaluate("() => window.__loads") == 0, (
        "htmx.process() re-fired htmx:load on the bulk form — the guard release listener is being re-entered by a code path that only meant to update a URL"
    )
    assert await page.evaluate("sel => !document.querySelector(sel).hasAttribute('data-hx-guard')", _APPROVE), (
        "the guard attribute came BACK across the re-processing, which would re-arm a released control"
    )
    for selector, label in ((_APPROVE, "Approve selected eligible"), ("button[value='reject']", "Reject selected pending")):
        assert await page.evaluate("sel => document.querySelector(sel).matches(':disabled')", selector), (
            f"{label!r} is clickable with nothing selected after ten syncs — the re-processing broke phaze-tckiy's fieldset/guard composition"
        )

    # Now the over-release half: fire the listener's own event at the form by hand.
    #
    # A canary rides along, and it is not decoration. Asserting only that the two real buttons stayed
    # disabled would pass just as well if the hand-fired event never reached shell.html's listener at
    # all -- a vacuous green, and the exact shape of "verified against a proxy that cannot exhibit
    # the failure". The canary is a control that IS still guarded, so the listener releasing IT is
    # positive proof the listener ran on this very event; the two real buttons then staying disabled
    # is a statement about idempotence rather than about whether anything happened.
    released_canary = await page.evaluate(
        """() => {
            const form = document.querySelector('#changes-bulk-form');
            const canary = document.createElement('button');
            canary.type = 'button';
            canary.disabled = true;
            canary.setAttribute('data-hx-guard', '');
            form.appendChild(canary);
            window.htmx.trigger(form, 'htmx:load', { elt: form });
            const released = !canary.disabled && !canary.hasAttribute('data-hx-guard');
            canary.remove();
            return released;
        }"""
    )
    assert released_canary, (
        "the hand-fired htmx:load never reached shell.html's release listener, so the over-release assertions below would be vacuous"
    )
    assert await page.evaluate("() => window.__loads") == 1, "the hand-fired htmx:load was not observed on the form — the dispatch itself failed"
    for selector, label in ((_APPROVE, "Approve selected eligible"), ("button[value='reject']", "Reject selected pending")):
        assert await page.evaluate("sel => document.querySelector(sel).matches(':disabled')", selector), (
            f"{label!r} was re-enabled by a second htmx:load — the guard release is not idempotent"
        )

    # And the buttons are not STUCK: the business rule still lets them through.
    await page.get_by_label("Select <set-01>.mp3").check()
    await page.wait_for_function("sel => !document.querySelector(sel).matches(':disabled')", arg=_APPROVE, timeout=5_000)
