"""phaze-8p1uq: scan management and duplicate resolution, plus the OOB swap that carries them.

Both flows were empty-state-only under ``phaze-tzy6s.14``: with no scan batches the Recent Scans
table renders a placeholder and its delete control does not exist, and with no duplicate groups the
Dedupe workspace renders "No duplicates found" and there is nothing to resolve. Both are also the
suite's best OOB and error-state surfaces, because both answer a mutation with a fragment aimed
somewhere other than where the click happened.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from phaze.models.scan_batch import ScanStatus
from tests.browser.helpers import click_swap, open_shell, settled


pytestmark = pytest.mark.browser


# --- Scan management ------------------------------------------------------------------------


async def test_a_failed_scan_shows_its_error_message_next_to_the_row(page: Any, seed: Any) -> None:
    """The ERROR state of the scan table: a failed batch renders its reason, not just "failed".

    ``recent_scans_table.html``'s own header records what this protects. The partial was orphaned
    for a stretch -- nothing mounted ``id="recent-scans"`` -- and during it an operator whose scan
    failed saw the bare word "failed" with no reason and no way to remove the half-ingested batch
    short of psql. A rendered error row is cheap to lose again (it is a conditional sub-row inside a
    table that gets re-rendered by poll, sort and delete alike) and expensive to be without.
    """
    await seed.scan_batch(status=ScanStatus.COMPLETED, age=timedelta(minutes=20))
    await seed.scan_batch(
        status=ScanStatus.FAILED,
        error_message="Scan root is unreadable: permission denied",
        age=timedelta(minutes=5),
    )

    await open_shell(page, "/s/discover")

    table = page.locator("#recent-scans")
    body = await table.inner_text()
    assert "failed" in body, "the failed batch does not appear in Recent Scans"
    assert "Scan root is unreadable: permission denied" in body, "a failed scan renders no error message — the operator is told nothing actionable"
    assert "completed" in body, "the completed batch vanished when a failed one was added"


async def test_deleting_a_terminal_scan_removes_its_row_without_reloading(page: Any, seed: Any) -> None:
    """Scan management's only mutation: DELETE, swapped back into the table in place.

    ``hx-delete`` + ``hx-target="#recent-scans"`` is markup any server-side test can read. What it
    cannot read is whether the re-rendered table replaced the table -- ``hx-swap="outerHTML"`` on a
    self-polling section is the shape that most easily lands wrong, and landing wrong here nests one
    table inside another so the delete appears to have done nothing while the row count doubles.
    """
    keep = await seed.scan_batch(status=ScanStatus.COMPLETED, age=timedelta(minutes=20), scan_path="/archive-mount/incoming")
    doomed = await seed.scan_batch(status=ScanStatus.FAILED, error_message="Interrupted", age=timedelta(minutes=5))

    await open_shell(page, "/s/discover")
    await page.evaluate("window.__documentAlive = true")

    assert await page.locator(f"#recent-scans [hx-delete*='{doomed.id}']").count() == 1, "no delete control on a terminal scan row"

    page.on("dialog", lambda dialog: dialog.accept())
    await page.click(f"#recent-scans [hx-delete*='{doomed.id}']")
    await settled(page)

    await page.wait_for_function("""id => !document.querySelector(`#recent-scans [hx-delete*="${id}"]`)""", arg=str(doomed.id))
    assert await page.evaluate("window.__documentAlive === true"), "deleting a scan reloaded the document"
    assert await page.locator("#recent-scans").count() == 1, "the delete response nested a second table instead of replacing the first"
    assert await page.locator(f"#recent-scans [hx-delete*='{keep.id}']").count() == 1, "the surviving scan lost its row in the re-render"


# --- Duplicate resolution -------------------------------------------------------------------


async def test_resolving_a_duplicate_group_oob_swaps_its_undo_toast_into_the_shell(page: Any, seed: Any) -> None:
    """THE out-of-band assertion (bead item 4), on the OOB swap that matters most.

    32 templates use ``hx-swap-oob`` and no test in the repo has ever proven htmx honoured one. The
    mechanism is worth stating precisely, because it is what makes this unprovable server-side: the
    resolve response's PRIMARY target is the group card inside ``#stage-workspace``, while its
    ``hx-swap-oob="beforeend:#toast-container"`` block is aimed at a container declared in
    ``shell.html``, OUTSIDE the workspace and never re-rendered by a stage swap. The fragment
    therefore has to be split by htmx and delivered to two different subtrees. A server test sees
    one response body and can say nothing about where either half went.

    The failure mode when it does not work is silent and total: htmx fires
    ``htmx:oobErrorNoTarget`` and DISCARDS the block. ``resolve_response.html`` carries a long
    comment about exactly that happening to a ``#stats-header`` block whose host had been deleted --
    computed on every resolve, thrown away every time, with nothing on screen to suggest it. Here
    the discarded half is the ONLY undo affordance for an irreversible-looking bulk archive.

    Asserting on ``#toast-container`` rather than on "a toast exists somewhere" is deliberate: the
    dedupe workspace comments record that a second element sharing that id makes htmx (which
    resolves OOB selectors with ``querySelectorAll``) append a clone into EACH match. Counting the
    container and its children is what would catch that.
    """
    files = await seed.duplicate_group(count=3)
    group_hash = files[0].sha256_hash

    await open_shell(page, "/s/dedupe")

    assert await page.locator("#toast-container").count() == 1, (
        "the shell must declare exactly one #toast-container (a duplicate id doubles every OOB toast)"
    )
    assert (await page.locator("#toast-container").inner_html()).strip() == "", "the toast container is not empty before any resolution"
    # The OOB target lives outside the swapped workspace -- that is what makes this a real OOB test.
    assert await page.evaluate("!document.querySelector('#stage-workspace').contains(document.getElementById('toast-container'))"), (
        "#toast-container is inside #stage-workspace, so the resolve response would not need an out-of-band swap to reach it"
    )

    # Two-step: review stages the plan, confirm commits it. Each click goes through click_swap
    # rather than a bare click + wait_for_selector/settled (phaze-boyl9): the confirm button below
    # lives in a <form hx-post="...resolve"> that htmx has JUST swapped in, and DOM presence
    # (what wait_for_selector proves) is not the same as htmx having finished PROCESSING that node
    # -- attaching the hx-post interception happens in htmx's deferred settle phase, not the
    # synchronous swap. A click that wins that race falls through to the browser's own native form
    # submission (the <form> carries no method/action, so that's a plain GET back to the current
    # URL with the hidden plan_id as a query param) instead of the intended POST, and the resolve
    # never happens -- the toast this test waits for next then times out at 30s. Measured: this
    # reproduced in 4/40 attempts (2/25 warm-shape repeats in one process, 2/15 cold-shape separate
    # processes), every one with the identical signature (a stray `GET /s/dedupe?plan_id=...`
    # where `POST .../resolve` should have been). click_swap waits for htmx:afterSettle -- the
    # event that fires once htmx has actually finished processing the swapped-in content -- closing
    # the window instead of racing it.
    await click_swap(page, f"#dupe-group-{group_hash} button[type=submit]")
    plan = await page.locator(f"#dupe-group-{group_hash}").inner_text()
    assert "Archive 2 reviewed copies" in plan, f"the review step did not describe the plan it staged: {plan[:200]!r}"

    await click_swap(page, f"#dupe-group-{group_hash} button[type=submit]")

    toasts = page.locator("#toast-container > *")
    await toasts.first.wait_for()
    assert await toasts.count() == 1, f"expected exactly one OOB toast, found {await toasts.count()} — a duplicated container id clones the swap"

    toast_text = await page.locator("#toast-container").inner_text()
    assert "2 files marked as duplicates" in toast_text, f"the OOB toast landed but says the wrong thing: {toast_text!r}"
    assert "Undo" in toast_text, "the OOB toast carries no Undo — the only affordance that makes the resolve reversible"

    # The primary half of the same response landed too: the card is gone from the workspace.
    assert not await page.locator(f"#dupe-group-{group_hash}").is_visible(), "the resolved group card is still on screen"


async def test_an_already_resolved_group_is_not_offered_for_resolution(page: Any, seed: Any) -> None:
    """A resolved group leaves the workspace, so the same archive cannot be committed twice.

    ``_dup_hash_subquery`` filters on ``~dedup_resolved_clause()``, and this is the browser-visible
    consequence. Seeding the resolution directly (rather than resolving through the UI) is the
    point: it proves the workspace reads the marker rows, not some in-page state left over from the
    click that created them.
    """
    resolved = await seed.duplicate_group(count=2)
    await seed.resolve_duplicate(keeper=resolved[0], archived=resolved[1:])
    live = await seed.duplicate_group(count=2)

    await open_shell(page, "/s/dedupe")

    assert await page.locator(f"#dupe-group-{live[0].sha256_hash}").count() == 1, "the unresolved group is missing from the workspace"
    assert await page.locator(f"#dupe-group-{resolved[0].sha256_hash}").count() == 0, "an already-resolved group is still offered for resolution"
