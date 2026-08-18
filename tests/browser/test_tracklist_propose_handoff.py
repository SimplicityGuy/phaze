"""phaze-tzy6s.14 (CR-14-2): the Tracklist -> Propose handoff.

``/s/tracklist`` and ``/s/propose`` were each rendered once by the all-workspace console sweep and
the step BETWEEN them was never taken. That step is a real product affordance, not a rail click: the
Tracklist workspace ends with a "Continue to Propose" link that carries ``hx-get`` /
``hx-target="#stage-workspace"`` / ``hx-push-url``, i.e. an in-workspace control that performs a
stage navigation. It is the workflow's own next-step, and it is a different code path from the rail
node the shell suite already exercises.

Two properties make it worth a browser rather than a route test:

* it must be a SWAP, not a navigation. The link has a real ``href``, so a browser that ignored the
  ``hx-get`` would still land on a correct-looking Propose page -- by reloading the whole document,
  destroying the shell, the palette, the record host and the theme the operator had. The server
  cannot tell those two outcomes apart; both are a 200 with the same body.
* the destination has to arrive HYDRATED. Propose's list is server-rendered inside the swapped
  fragment while its search box, tabs and pager are htmx controls, so "did the swap land" and "is the
  workspace usable afterwards" are separate questions.
"""

from __future__ import annotations

from typing import Any

import pytest

from phaze.models.proposal import ProposalStatus
from tests.browser.helpers import click_swap, open_shell, settled, wait_for_stage


pytestmark = pytest.mark.browser


_CONTINUE = '#stage-workspace a[href="/s/propose"]:not([data-rail-stage])'


async def test_continue_to_propose_swaps_the_workspace_instead_of_reloading(page: Any, seed: Any) -> None:
    """The handoff is an htmx swap on a link that also has a working href.

    That combination is the whole risk. ``<a href="/s/propose" hx-get="/s/propose" ...>`` degrades to
    a full navigation whenever htmx does not claim the click -- which is the correct fallback and
    also exactly what a regression looks like. Both outcomes render the Propose workspace, both
    return 200, and only one of them keeps the shell alive. A sentinel written onto ``window`` before
    the click is the only way to tell them apart, because a full navigation is precisely the event
    that erases it.

    The rail's own Propose node is excluded from the selector: this test is about the workflow link
    inside the workspace, and matching the rail instead would silently re-test what
    ``test_shell_contract`` already covers.

    ``seed`` is requested but not called. It is the suite's isolation fixture -- it truncates before
    yielding -- and this test asserts on an EMPTY Propose workspace, which a previous test's leftover
    proposals would quietly turn into a different assertion.
    """
    await open_shell(page, "/s/tracklist")
    await settled(page)
    await page.evaluate("window.__shellAlive = true")

    handoff = page.locator(_CONTINUE)
    assert await handoff.count() == 1, "the Tracklist workspace offers no Continue to Propose affordance"
    assert "Continue to Propose" in await handoff.inner_text()

    await handoff.click()
    await wait_for_stage(page, "Propose changes")
    await settled(page)

    assert await page.evaluate("window.__shellAlive === true"), (
        "Continue to Propose reloaded the document — the href fallback ran instead of the htmx swap"
    )
    assert page.url.endswith("/s/propose"), f"the handoff did not push its destination: {page.url}"
    assert await page.locator('aside[aria-label="Pipeline navigation"]').count() == 1, "the shell did not survive the handoff"
    assert await page.get_attribute('a[data-rail-stage="propose"]', "aria-current") == "page", (
        "the rail still highlights Tracklist after handing off to Propose — the operator is told they are somewhere they are not"
    )


async def test_the_handed_off_propose_workspace_lists_the_pending_proposals(page: Any, seed: Any) -> None:
    """Arriving by handoff has to deliver the real Propose workspace, not an empty shape of one.

    A swap that lands the wrong response shape is the failure this catches: the destination is a
    dual-shape route (full page vs fragment), so a handoff that requested the wrong one drops either
    a chrome-less document into the workspace or an empty container that renders its heading and
    nothing else. Both look plausible in a screenshot. Asserting that the SEEDED proposals are listed
    after the swap is what distinguishes "the fragment arrived" from "the fragment arrived with the
    content it was supposed to carry".

    Seeded through ``proposal`` rather than by driving GENERATE ALL, because generation is an
    LLM-costing operator action with no worker running in this harness -- the state is reachable only
    by writing it.
    """
    await seed.proposal(
        status=ProposalStatus.PENDING, filename="<set-01>.mp3", proposed_filename="Example Artist - Example Event - Set One (2024).mp3"
    )
    await seed.proposal(
        status=ProposalStatus.PENDING, filename="<set-02>.mp3", proposed_filename="Example Artist - Example Event - Set Two (2024).mp3"
    )

    await open_shell(page, "/s/tracklist")
    await settled(page)

    await page.locator(_CONTINUE).click()
    await wait_for_stage(page, "Propose changes")
    await settled(page)

    workspace = await page.locator("#stage-workspace").inner_text()
    assert "Example Artist - Example Event - Set One (2024).mp3" in workspace, (
        f"the handed-off Propose workspace lists no proposals: {workspace[:300]!r}"
    )
    assert "Example Artist - Example Event - Set Two (2024).mp3" in workspace, "the handed-off Propose workspace listed only part of the pending set"
    assert await page.locator("#propose-search").count() == 1, (
        "the Propose search control did not arrive with the swap — the workspace rendered without the controls that operate it"
    )


async def test_going_back_from_the_handoff_restores_a_working_tracklist_workspace(page: Any, seed: Any) -> None:
    """Back has to return a LIVE Tracklist workspace, deferred panels and all.

    The handoff pushes history, so Back restores the previous stage -- and the restored response has
    to be the full shell shape, not the fragment the swap used. The retrospective's ``Vary:
    HX-Request`` family lives exactly here.

    The stronger claim is the second one: Tracklist's lookup panel and set table are EMPTY hosts that
    fetch themselves with ``hx-trigger="load"``. On a history restore those triggers have to be armed
    again, or the operator gets a workspace permanently stuck on "Loading lookup state…" -- an
    outcome that is invisible to any assertion on the restored HTML, because the restored HTML is
    byte-for-byte the same placeholder either way.

    Tracklist is reached by a RAIL CLICK rather than by navigating to it, so that both history
    entries are htmx pushes and Back exercises htmx's own restore. Navigating directly would make the
    first entry a full document, and Back would then be a plain browser reload -- a different
    mechanism, already covered by the direct-nav tests, and one where ``data-document-title`` sits on
    ``#stage-workspace`` itself rather than on the fragment marker ``wait_for_stage`` reads.
    """
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")

    await open_shell(page, "/s/summary")
    await settled(page)
    await click_swap(page, 'a[data-rail-stage="tracklist"]')
    await wait_for_stage(page, "Tracklists")
    await settled(page)

    await page.locator(_CONTINUE).click()
    await wait_for_stage(page, "Propose changes")

    await page.go_back()
    await wait_for_stage(page, "Tracklists")
    await settled(page)

    assert await page.locator('aside[aria-label="Pipeline navigation"]').count() == 1, "the restore produced a fragment, not the shell"
    assert await page.locator(_CONTINUE).count() == 1, "the restored Tracklist workspace lost the handoff affordance"

    for panel, placeholder in (("#tracklist-drain-status-view", "Loading lookup state"), ("#tracklist-sets-view", "Loading sets")):
        await page.wait_for_function(
            "args => !document.querySelector(args[0]).innerText.includes(args[1])",
            arg=[panel, placeholder],
        )
