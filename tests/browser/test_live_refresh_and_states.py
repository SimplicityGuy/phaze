"""phaze-8p1uq: the live-refresh path, and the warning states that only exist with data.

Bead item 3 asks for coverage beyond empty -- at least one warning, one error and one live-refresh
state. The error state lives with the surface it belongs to
(``test_scans_and_duplicates.py``'s failed-scan row, and ``test_execute_dispatch.py``'s
``complete_with_errors`` stream). This file holds the live-refresh path and the warning states,
because both are properties of the SHELL rather than of any one workspace.
"""

from __future__ import annotations

from typing import Any

import pytest

from phaze.models.proposal import ProposalStatus
from tests.browser.helpers import open_shell, wait_for_stage


pytestmark = pytest.mark.browser


# The shell polls /pipeline/stats every 5s (shell.html, WORK-05 / R-2 -- ONE poll for the whole
# document). A live-refresh assertion has to outlast a tick without depending on landing inside one,
# so the wait below is generous relative to the interval rather than tuned to it.
_POLL_WINDOW_MS = 20_000


async def test_the_shell_poll_oob_seeds_new_counts_without_a_navigation(page: Any, seed: Any) -> None:
    """The live-refresh state (bead item 3) and the OOB fan-out that implements it.

    This is the shell's whole live-update mechanism and it is a genuinely two-part htmx swap:
    ``#pipeline-stats`` lives in chrome, outside ``#stage-workspace``, and its 5s poll
    ``innerHTML``-swaps ``stats_bar.html``. That partial's payload is not the swapped content -- it
    is a set of hidden ``hx-swap-oob="true"`` paragraphs whose ids match placeholders mounted INSIDE
    the workspace by ``_workspace_poll_seeds.html``. Each one's ``x-init`` pushes a fresh count into
    ``$store.pipeline``, which is what every badge, sub-count and ``:disabled`` binding reads.

    So the chain under test is: server poll -> OOB split -> landing in a different subtree -> Alpine
    initialising the freshly-inserted node -> store write -> re-render. Server-side tests can assert
    the partial emits the attributes; nothing but a browser can assert the counts actually arrive.
    And the failure is silent by construction, exactly as ``_workspace_poll_seeds.html`` warns: an
    OOB swap whose target id is absent no-ops, so the workspace's numbers simply stick at their
    initial values while the poll keeps succeeding. CONSOLE-02 -- a rail badge reading 0 while 2,183
    jobs were in flight -- was this.

    Seeding happens AFTER the page has rendered on purpose. A reload would prove the server can
    count; only the poll proves the open page updates itself.
    """
    await open_shell(page, "/s/summary")

    assert await page.evaluate("Alpine.store('pipeline').discovered") == 0, "the corpus is empty but the store already claims files"
    assert (await page.locator("#analyze-files-ready").inner_text()).strip() == "", "the OOB landing placeholder is not empty on a fresh render"

    # Now put files in the corpus, with no interaction of any kind.
    for index in range(3):
        await seed.file(filename=f"<set-{index + 1:02d}>.mp3")

    await page.wait_for_function("() => Alpine.store('pipeline').discovered === 3", timeout=_POLL_WINDOW_MS)

    # The store moved because an OOB paragraph landed in the workspace, not because anything
    # navigated. Both halves are asserted: a store write with an empty placeholder would mean the
    # count arrived by some other route and this test would no longer be covering OOB at all.
    landed = (await page.locator("#analyze-files-ready").inner_text()).strip()
    assert landed == "3 files ready", f"the hx-swap-oob seed did not land in #analyze-files-ready (found {landed!r})"
    assert await page.evaluate("!document.getElementById('pipeline-stats').contains(document.getElementById('analyze-files-ready'))"), (
        "#analyze-files-ready is inside the poll's own swap target, so this was an ordinary innerHTML swap, not an out-of-band one"
    )
    assert page.url.endswith("/s/summary"), "the live refresh navigated — that is a reload, not a refresh"


async def test_the_poll_survives_a_stage_swap(page: Any, seed: Any) -> None:
    """One poll, in chrome, that a rail swap neither destroys nor duplicates.

    ``shell.html`` puts ``#pipeline-stats`` outside ``#stage-workspace`` specifically so a stage
    swap cannot re-render it. Both failure directions are live: destroyed, and the shell stops
    updating after the first navigation (which looks like "stale data" and gets blamed on caching);
    duplicated, and every count arrives twice per tick from two loops, which doubles the load and
    makes ordering between them undefined. Only a browser can count what is mounted after a swap.
    """
    await open_shell(page, "/s/summary")
    await page.click('a[data-rail-stage="files"]')
    await wait_for_stage(page, "Files")

    assert await page.locator("#pipeline-stats").count() == 1, "the stage swap left more or fewer than one stats poll mounted"

    await seed.file(filename="<set-01>.mp3")
    await page.wait_for_function("() => Alpine.store('pipeline').discovered === 1", timeout=_POLL_WINDOW_MS)


async def test_a_blocked_proposal_warns_in_the_workspace_it_blocks(page: Any, seed: Any) -> None:
    """The WARNING state (bead item 3): approved work that Execute will refuse to dispatch.

    Two proposals aimed at ONE destination is a collision. ``detect_collisions`` runs before any
    grouping in ``/execution/start`` and short-circuits the entire dispatch, so this is not a
    cosmetic badge -- it is the difference between Execute running and Execute doing nothing. The
    operator has to be able to see WHY from the page, since the alternative is pressing a button
    that appears live and produces a block card.

    The empty-state suite could only ever see the "nothing approved" disabled reason, which is the
    trivially-satisfied case. This is the one where there IS approved work and it still cannot run.
    """
    # The collision key is (agent_id, owning_root, dest_path) -- services/collision.py's
    # _dest_key_columns -- and dest_path is built from proposed_path AND proposed_filename. Both
    # must match for two proposals to land on one destination; sharing only the directory is not a
    # collision, it is two files in a folder.
    collision_dir = "/archive-mount/organized/Example Artist/Example Festival (2024)"
    collision_name = "01 - Opening Set.mp3"
    for source in ("<set-01>.mp3", "<set-02>.mp3"):
        await seed.proposal(
            status=ProposalStatus.APPROVED,
            filename=source,
            proposed_filename=collision_name,
            proposed_path=collision_dir,
        )

    await open_shell(page, "/s/apply")

    body = (await page.locator("#stage-workspace").inner_text()).lower()
    assert "conflict" in body or "collision" in body, (
        f"two proposals share a destination and the Execute workspace says nothing about it: {body[:400]!r}"
    )

    trigger = page.locator('button[aria-label^="Review and execute"]')
    assert await trigger.is_disabled(), "Execute is live despite an unresolvable destination collision — pressing it can only produce a block"


async def test_the_changes_review_facets_report_the_states_they_hold(page: Any, seed: Any) -> None:
    """Populated facet counts, which the empty-state suite renders as five zeroes.

    Five tabs all reading 0 satisfy any assertion about the tabs existing while proving nothing
    about the counting, and the counts are what an operator navigates by. Seeding one proposal in
    each of three states makes each tab's number falsifiable.
    """
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")
    await seed.proposal(status=ProposalStatus.APPROVED, filename="<set-02>.mp3")
    await seed.proposal(status=ProposalStatus.REJECTED, filename="<set-03>.mp3")

    await open_shell(page, "/s/rename")

    tabs = page.locator("nav[aria-label='Changes Review filters'] a")
    labelled = {}
    for index in range(await tabs.count()):
        text = (await tabs.nth(index).inner_text()).split()
        labelled[" ".join(text[:-1])] = text[-1]

    assert labelled.get("All") == "3", f"the All facet miscounts a 3-proposal corpus: {labelled}"
    assert labelled.get("Needs Review") == "1", f"Needs Review should hold the one pending proposal: {labelled}"
    assert labelled.get("Approved") == "1", f"Approved should hold the one approved proposal: {labelled}"
    assert labelled.get("Rejected") == "1", f"Rejected should hold the one rejected proposal: {labelled}"
