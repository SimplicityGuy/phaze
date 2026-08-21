"""phaze-8p1uq: the Changes Review golden path, driven against seeded proposals.

``phaze-tzy6s.14`` could only reach this workspace's empty state, so "proposal review/approve" and
"search/tab state" were both covered by a smoke render that asserted the page did not throw. What
follows are the interaction paths themselves: a single-row approve that must not touch its
neighbours, a facet tab that must swap rather than navigate, and a row selection whose survival
across that swap is implemented entirely in JavaScript and is therefore invisible to every
server-side assertion in the repo.
"""

from __future__ import annotations

from typing import Any

import pytest

from phaze.models.proposal import ProposalStatus
from tests.browser.helpers import open_shell, settled


pytestmark = pytest.mark.browser


async def test_approving_one_proposal_swaps_only_that_row(page: Any, seed: Any) -> None:
    """APPROVE re-renders its own row and leaves every sibling exactly as it was.

    The row's markup says ``hx-target="#rename-row-{id}" hx-swap="outerHTML"``, and the server suite
    proves the response is the one targeted row. Neither establishes what this asserts: that htmx
    put the response THERE. A target typo, a duplicated id, or an ``hx-swap`` widened during a
    refactor all produce a response that is correct on the wire and lands somewhere else -- most
    destructively on the whole list, which silently discards every other row's edit-in-progress and
    checkbox state.

    Approving is also the only state transition on this page that is irreversible from the
    operator's side within one gesture, so "which rows did that touch" is the question worth
    holding.
    """
    target = await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")
    bystander = await seed.proposal(status=ProposalStatus.PENDING, filename="<set-02>.mp3")

    await open_shell(page, "/s/rename")
    bystander_before = await page.locator(f"#rename-row-{bystander.id}").inner_html()

    await page.click(f"#rename-row-{target.id} button:has-text('APPROVE')")
    await settled(page)

    approved = page.locator(f"#rename-row-{target.id}")
    await approved.get_by_text("APPROVED").wait_for()
    assert await approved.locator("button:has-text('APPROVE')").count() == 0, "the approved row still offers APPROVE — the swap did not land on it"
    assert await approved.locator("button:has-text('UNDO')").count() == 1, "an approved row must offer UNDO"

    assert await page.locator(f"#rename-row-{bystander.id}").inner_html() == bystander_before, (
        "approving one proposal re-rendered a different row — the swap was wider than its hx-target"
    )
    assert await page.locator("[id^='rename-row-']").count() == 2, "the row count changed; the response replaced the list rather than one row"


async def test_a_facet_tab_swaps_the_list_and_pushes_the_url_without_reloading(page: Any, seed: Any) -> None:
    """The Changes Review tabs are an htmx swap with ``hx-push-url``, not a navigation.

    This is the "search/tab state" flow the bead names. The tabs carry both ``href`` and ``hx-get``
    -- so they degrade to real links without JavaScript, which is why a broken ``hx-get`` does not
    look broken: the page still changes, it just does it by reloading the entire document. That
    difference is invisible in a screenshot and invisible on the server, and it costs every piece of
    unsaved client state on the page. The sentinel below is the whole test: it survives a swap and
    does not survive a load.
    """
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")
    await seed.proposal(status=ProposalStatus.APPROVED, filename="<set-02>.mp3")

    await open_shell(page, "/s/rename")
    await page.evaluate("window.__documentAlive = true")

    tabs = page.locator("nav[aria-label='Changes Review filters'] a")
    await tabs.filter(has_text="Approved").click()
    await settled(page)

    assert await page.evaluate("window.__documentAlive === true"), "the tab click reloaded the document — hx-get did not intercept the href"
    assert "status=approved" in page.url, "hx-push-url did not put the active facet in the address bar"

    current = page.locator("nav[aria-label='Changes Review filters'] a[aria-current='page']")
    assert await current.count() == 1, "exactly one tab must be marked aria-current after the swap"
    assert "Approved" in await current.inner_text()

    # The facet actually filtered, rather than merely relabelling itself.
    rows = page.locator("[id^='rename-row-']")
    assert await rows.count() == 1, f"the Approved facet rendered {await rows.count()} rows, expected the 1 approved proposal"
    assert await rows.first.locator("button:has-text('UNDO')").count() == 1, "the row shown under Approved is not an approved row"


async def test_a_row_selection_is_written_into_the_url_and_into_every_nav_attribute(page: Any, seed: Any) -> None:
    """Checking a row writes ``selected`` into the URL and into every nav target's ATTRIBUTE.

    ``_changes_list.html``'s ``sync()`` does three things on every selection change: rewrites the
    address bar via ``history.replaceState``, rewrites the ``href``/``hx-get`` of each
    ``[data-changes-nav]`` element, and rewrites the bulk form's ``hx-patch``. All three are pure
    client-side DOM mutation with no server involvement whatsoever, so this behaviour has no
    server-side test and can have none.

    Two corrections from phaze-tp52f, both of which this docstring got wrong and which are worth
    keeping written down rather than quietly edited away:

    1. **This test does not change a tab, and never did** -- it was named as though it did. Every
       assertion below reads an ATTRIBUTE, and the attribute was correct for the entire lifetime of
       the defect phaze-tp52f fixed: htmx 2.0.10 captured a verb's path in the trigger-handler
       closure at ``processNode`` time, so the rewritten ``hx-get`` never reached the wire and a tab
       change dropped (or resurrected) the selection with all of these assertions still green. What
       actually crosses a tab boundary, and reads the socket rather than the DOM, is
       ``tests/browser/test_changes_facet_selection_wire.py``. This file keeps the attribute half,
       which is a real and cheap contract; it is simply not the half that was ever in doubt.
    2. **The bulk approve write is NOT keyed on this query.** ``PATCH /proposals/bulk`` scopes from
       ``review_tokens``/``proposal_ids`` in the request BODY, which htmx serializes from the live
       DOM at request time; ``selected`` is read only by ``build_changes_review_context``, to seed
       the RESPONSE's checkbox state. So a dropped ``selected`` never approved "a different set than
       the one on screen" directly. It does so indirectly -- the checkbox it resurrects IS the bulk
       scope -- which is a slower and more visible failure than the one claimed here.
    """
    keeper = await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-02>.mp3")

    await open_shell(page, "/s/rename")
    assert "selected=" not in page.url, "nothing is selected yet, so the URL must not claim otherwise"

    # The selection checkbox is a SIBLING of the diff row, not a child of it -- _changes_list.html
    # wraps each row in a flex pair so the control sits outside the swap target the row's own
    # approve/edit actions replace. Addressed by its accessible name, which is also the only thing
    # that identifies it to a screen reader.
    checkbox = page.get_by_label("Select <set-01>.mp3")
    await checkbox.check()
    await page.wait_for_function("() => window.location.search.includes('selected=')")
    assert str(keeper.id) in page.url, "the checked row's id did not reach the address bar"

    # Every nav destination must carry the selection forward, not just the address bar.
    nav_targets = await page.locator("[data-changes-nav]").evaluate_all("els => els.map(el => el.getAttribute('hx-get'))")
    assert nav_targets, "the changes list rendered no [data-changes-nav] elements to carry the selection"
    assert all(f"selected={keeper.id}" in (target or "") for target in nav_targets), (
        f"a nav target dropped the selection: {[t for t in nav_targets if f'selected={keeper.id}' not in (t or '')]}"
    )

    # And the bulk form -- the actual write -- agrees with what is on screen.
    bulk_action = await page.locator("#changes-bulk-form").get_attribute("hx-patch")
    assert f"selected={keeper.id}" in (bulk_action or ""), f"the bulk approve form would submit {bulk_action!r}, which does not carry the checked row"

    # Unchecking must retract it everywhere, or a stale id survives into a later bulk write.
    await checkbox.uncheck()
    await page.wait_for_function("() => !window.location.search.includes('selected=')")
    assert "selected=" not in (await page.locator("#changes-bulk-form").get_attribute("hx-patch") or ""), (
        "unchecking the row left the selection in the bulk form's hx-patch"
    )
