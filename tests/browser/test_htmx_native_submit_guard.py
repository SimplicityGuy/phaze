"""phaze-5i74w: neither approval-commit <form> may silently no-op on a premature click.

``duplicates/partials/review_response.html``'s "Confirm resolution" step and
``pipeline/partials/_changes_list.html``'s bulk tag-write approval both declared no ``method``/
``action`` and relied entirely on htmx intercepting their submit. htmx wires that interception up
(and, for the tag-write form, its ``hx-confirm`` prompt) during its deferred SETTLE phase, not
synchronously on swap -- see shell.html's ``htmx:load`` listener comment for the mechanism. A click
that landed in the gap between "attached to the DOM" and "processed by htmx" fell through to the
browser's own native submission: no method -> GET, no action -> the current URL, hidden fields ->
query parameters. Both forms ARE the approval step in this app's human-in-the-loop workflow, so that
fallback was a silent no-op indistinguishable from an ordinary reload -- measured via Playwright
network trace at 4/40 (10%) for a click fired the instant the confirm form attached to the DOM
(phaze-boyl9, 2026-08-20).

The fix renders each button ``disabled`` + ``data-hx-guard`` and releases the guard only once htmx's
own ``htmx:load`` event proves the element has been processed (shell.html). Playwright's own click
actionability rules refuse to click a disabled control, so a plain, unsynchronized ``page.click()``
issued the instant a guarded button exists now WAITS for it structurally -- there is no explicit
``settled()``/``click_swap`` call in either test below, which is deliberate: that is what "still
fails to race, without the test doing anything special" is supposed to look like post-fix. Every
test here reproduces the exact "click the instant it exists" methodology phaze-boyl9's PRE-fix test
used, not the ``click_swap``-guarded version it shipped with (AC4: that fix stays exactly as merged
in 303f7761 -- a test should not depend on a race even once the product tolerates it -- this file
is additional coverage, not a replacement for it).
"""

from __future__ import annotations

from typing import Any

import pytest

from phaze.models.proposal import ProposalStatus
from tests.browser.helpers import click_swap, open_shell, settled


pytestmark = pytest.mark.browser


# Matches phaze-boyl9's own "cold shape" repeat count (a fresh navigation per attempt) -- the shape
# closest to a real operator, who never gets a warm in-process repeat either.
_DEDUPE_RACE_ATTEMPTS = 15

# The tag-write race needs a heavier per-attempt seed (a file + metadata + an executed proposal, vs
# dedupe's file-only duplicate group), so it runs fewer repeats -- still enough to make a ~10%
# pre-fix failure rate implausible to pass by chance.
_TAG_WRITE_RACE_ATTEMPTS = 6


async def test_confirming_a_duplicate_resolution_the_instant_the_button_exists_still_commits(page: Any, seed: Any) -> None:
    """Race the dedupe "Confirm resolution" click, repeatedly, with no settle wait of any kind.

    Step 1 (staging the plan) goes through ``click_swap`` exactly as phaze-boyl9's test does --
    it is not the button under test here, and racing it would just add noise. Step 2 (the confirm
    click that commits the resolution) is a bare ``page.click()`` issued the moment the confirm form
    exists, reproducing phaze-boyl9's PRE-fix repro precisely. Before phaze-5i74w's fix this was
    measured at 4/40 (10%) stray native-GET fallbacks; every attempt below must commit.
    """
    for attempt in range(_DEDUPE_RACE_ATTEMPTS):
        files = await seed.duplicate_group(count=3)
        group_hash = files[0].sha256_hash

        await open_shell(page, "/s/dedupe")
        await page.evaluate("window.__documentAlive = true")

        await click_swap(page, f"#dupe-group-{group_hash} button[type=submit]")

        # THE RACE: no wait between the swap that just inserted this form and this click.
        await page.click(f"#dupe-group-{group_hash} button[type=submit]")

        assert await page.evaluate("window.__documentAlive === true"), (
            f"attempt {attempt}: the confirm click fell through to a native form submission "
            "(a full page navigation), which is exactly the pre-fix bug"
        )
        toasts = page.locator("#toast-container > *")
        await toasts.first.wait_for(timeout=5_000)
        assert await toasts.count() == 1, f"attempt {attempt}: no resolve toast landed — the confirm click did not commit"
        assert "plan_id=" not in page.url, (
            f"attempt {attempt}: the confirm click fell through to the native GET fallback (plan_id promoted to a query param)"
        )


async def test_approving_bulk_tag_writes_the_instant_the_form_swaps_in_still_confirms_and_writes(page: Any, seed: Any) -> None:
    """AC3: the raced click must still show ``hx-confirm``'s prompt, not just still write the tags.

    Unlike the dedupe form, the bulk tag-write form is not itself reached via a two-step reveal --
    it is present as soon as the Changes Review workspace swaps in, so the race here is between that
    workspace swap (a real rail-nav ``hx-get`` into ``#stage-workspace``, not a full page load —
    htmx's initial-boot ``processNode(document.body)`` is synchronous and does not carry this race;
    only an AJAX swap's deferred settle task does) and the very first click on its button.

    A fix that only stopped the native-GET fallback without restoring the confirmation would be a
    different, smaller bug than the one filed: the destructive bulk write would proceed with no
    prompt at all. This asserts BOTH halves: a ``dialog`` event fires with the expected message
    (proving ``hx-confirm`` is live, not skipped), and accepting it actually queues the write (the
    same toast-in-``#toast-container`` signal the dedupe form uses for "the POST truly happened").
    """
    for attempt in range(_TAG_WRITE_RACE_ATTEMPTS):
        await seed.bulk_eligible_tag_change(filename=f"Artist - Race Track {attempt}.mp3")

        # Start somewhere other than the Changes Review workspace so the form under test is reached
        # by a real htmx swap, not folded into the initial full-page HTML.
        await open_shell(page, "/s/summary")
        await page.evaluate("window.__documentAlive = true")

        dialog_messages: list[str] = []

        async def _accept(dialog: Any, messages: list[str] = dialog_messages) -> None:
            messages.append(dialog.message)
            await dialog.accept()

        page.once("dialog", _accept)

        await page.click("[data-rail-stage='rename']")
        # THE RACE: no settled()/click_swap between that nav swap and this click.
        await page.click("button:has-text('Approve visible eligible tag writes')")

        assert await page.evaluate("window.__documentAlive === true"), (
            f"attempt {attempt}: the approval click fell through to a native form submission"
        )
        toasts = page.locator("#toast-container > *")
        await toasts.first.wait_for(timeout=5_000)
        assert dialog_messages, f"attempt {attempt}: no confirm dialog fired — a raced click bypassed hx-confirm on a destructive bulk write"
        assert "Approve the eligible tag changes" in dialog_messages[-1], f"attempt {attempt}: unexpected confirm prompt: {dialog_messages[-1]!r}"
        toast_text = await toasts.first.inner_text()
        assert "tag write" in toast_text and "queued" in toast_text, f"attempt {attempt}: the approval did not actually queue a write: {toast_text!r}"


# --- phaze-tckiy: the remaining eight forms -------------------------------------------------
#
# phaze-5i74w asserted the two forms above were the only method-less htmx forms in the templates.
# They were not: a multi-line-aware scan across hx-post/hx-patch/hx-put/hx-delete found ELEVEN, and
# the survey that missed nine of them was a single-line `grep -o '<form[^>]*hx-post[^>]*>'` which
# cannot match a <form> tag whose attributes wrap across lines and never looked for hx-patch at all.
# tests/shared/core/test_htmx_native_submit_guard_coverage.py is that scan made permanent, so an
# unguarded ADDITION cannot land. Nine of that eleven were phaze-tckiy's; eight remain, because
# phaze-ur8o3 deleted comparison_table.html (and the compare route that was its only host) after
# verifying it unreachable, which is also why that scan's counted population is now 10 rather than
# 11. What follows is the runtime half it cannot supply: that the guard is actually released, on the
# paths where "released" is not obvious.
#
# Not every one of the eight gets a race here, and that is deliberate rather than an omission of
# convenience. Three of them (_force_skip_dialog.html's skip confirm, empty_state.html's per-root
# Scan, trigger_scan_card.html's Start Scan) sit behind the SAME shell.html listener on the SAME kind
# of primary swap already proven above and by the two races below, and reaching them costs multi-step
# setup that would test the setup more than the guard. The
# four raced here each answer a question the others do not: the dedupe group card, raced against a
# rail swap, is the plain case and the control; the resolve-all is destructive AND its hx-confirm is
# itself htmx-provided, so a won race skipped the prompt as well as the write; the hx-patch form has
# no native PATCH behind it, so its fallback is worth proving rather than assuming; and the undo
# toast arrives by an OUT-OF-BAND swap, a different htmx code path on which the guard would simply
# never be released if htmx did not queue the same settle task for OOB content.

_DEDUPE_STAGE = "[data-rail-stage='dedupe']"

# Repeat counts, chosen the way phaze-5i74w chose its own: high enough that the measured pre-fix
# failure rate is implausible to pass by chance, low enough to keep a browser test cheap.
#
# These are calibrated against a measurement, not guessed. With the nine template guards REVERTED on
# this branch and this file run 6 times single-attempt, the resolve-all and hx-patch races each fell
# through on 3 of 6 runs -- often enough to need no repetition -- while the two dedupe races below
# landed inside the window on only about 1 run in 6, which is not evidence a regression would be
# caught. With the loops these constants set, the same reverted-guard file went red on 3 of 3 runs
# (every one of the four races was seen failing at least once), and green on 3 of 3 with the guards
# in place. A race test that only reproduces one run in six trains reviewers to re-run it.
_KEEPER_RACE_ATTEMPTS = 6
_UNDO_RACE_ATTEMPTS = 5


async def test_staging_a_keeper_the_instant_the_dedupe_workspace_swaps_in_still_posts(page: Any, seed: Any) -> None:
    """``_dupe_group.html``: the FIRST of dedupe's two steps, raced against the rail swap.

    The test above races dedupe's second step (the confirm form, which arrives by htmx swap) but
    reaches it through ``click_swap``, so the group card's own form -- the one that stages the keeper
    -- was never itself raced. It carries the identical defect: no ``method``, no ``action``, a
    ``canonical_id`` radio that a native GET would promote to a query parameter, and a silent no-op
    that looks like a reload.

    Reaching the workspace by rail nav rather than ``open_shell`` is what makes this a race at all.
    htmx's initial-boot ``processNode(document.body)`` is synchronous, so a full page load has no
    window; only an AJAX swap's deferred settle task does.
    """
    for attempt in range(_KEEPER_RACE_ATTEMPTS):
        files = await seed.duplicate_group(count=3)
        group_hash = files[0].sha256_hash

        await open_shell(page, "/s/summary")
        await page.evaluate("window.__documentAlive = true")

        await page.click(_DEDUPE_STAGE)
        # THE RACE: no settled()/click_swap between that nav swap and this click.
        await page.click(f"#dupe-group-{group_hash} button[type=submit]")

        await page.wait_for_function(
            """hash => {
                const card = document.querySelector(`#dupe-group-${hash}`);
                return card !== null && card.innerText.includes('Archive');
            }""",
            arg=group_hash,
        )
        assert await page.evaluate("window.__documentAlive === true"), f"attempt {attempt}: staging the keeper fell through to a native submission"
        assert "canonical_id=" not in page.url, f"attempt {attempt}: the click fell through to the native GET fallback: {page.url}"


async def test_confirming_resolve_all_the_instant_the_review_lands_still_prompts_and_archives(page: Any, seed: Any) -> None:
    """``bulk_review.html``: the highest-stakes form of the nine, and the AC2 evidence.

    "Confirm N resolutions" archives every reviewed group's non-keeper copies in one POST, and its
    confirmation is ``hx-confirm`` -- which htmx attaches in the same deferred settle task as the
    ``hx-post`` interception it is meant to gate. Pre-fix, a click that won that race did BOTH wrong
    things at once: no prompt appeared AND the archive silently never happened, in an application
    whose stated core value is that nothing moves without review.

    So this asserts the prompt, not merely the outcome. A fix that made resolve-all work while
    skipping its confirmation would be a different bug, not a solution: the dialog is DRIVEN here
    (Playwright's ``dialog`` event fires only for a real ``window.confirm``, and the request cannot
    proceed until it is accepted), and its message is checked, rather than reasoned about from the
    markup.

    ``#dedupe-bulk-response`` is empty until "Review auto-selection" answers, so there is no stale
    pre-swap button for the raced click to resolve against -- the click below can only ever land on
    the freshly swapped-in form.
    """
    first = await seed.duplicate_group(count=3)
    second = await seed.duplicate_group(count=2)

    await open_shell(page, "/s/dedupe")
    await page.evaluate("window.__documentAlive = true")

    dialog_messages: list[str] = []

    async def _accept(dialog: Any, messages: list[str] = dialog_messages) -> None:
        messages.append(dialog.message)
        await dialog.accept()

    page.once("dialog", _accept)

    await page.click("button[aria-label='Review highest-quality choices for every duplicate group']")
    # THE RACE: no settle wait between the swap that inserts the review summary and this click on
    # the destructive confirm inside it.
    await page.click("#dedupe-bulk-response button[type=submit]")

    toasts = page.locator("#toast-container > *")
    await toasts.first.wait_for(timeout=5_000)

    assert await page.evaluate("window.__documentAlive === true"), "the resolve-all click fell through to a native form submission"
    assert dialog_messages, "no confirm dialog fired — a raced click bypassed hx-confirm on a destructive bulk archive"
    assert "Confirm these reviewed keepers" in dialog_messages[-1], f"unexpected confirm prompt: {dialog_messages[-1]!r}"
    assert "plan_ids=" not in page.url, f"the click fell through to the native GET fallback (plan_ids promoted to query params): {page.url}"

    toast_text = await page.locator("#toast-container").inner_text()
    assert "Resolved 2 groups" in toast_text, f"the resolve-all did not commit: {toast_text!r}"
    assert "Undo All" in toast_text, "the resolve-all toast carries no Undo All"
    for group_hash in (first[0].sha256_hash, second[0].sha256_hash):
        assert await page.locator(f"#dupe-group-{group_hash}").count() == 0, "a resolved group's card survived the bulk resolve"


async def test_undoing_a_resolution_the_instant_its_oob_toast_appears_still_restores_the_group(page: Any, seed: Any) -> None:
    """``toast.html``: the guard has to survive htmx's OUT-OF-BAND delivery path, not just a swap.

    Both Undo forms live in a fragment htmx extracts from the response and swaps somewhere the
    request never pointed at (``hx-swap-oob="beforeend:#toast-container"``, a shell-level container
    outside ``#stage-workspace``). That is a different code path from the primary swap every other
    form here arrives through, and the guard is only safe on it if htmx queues the same
    ``makeAjaxLoadTask`` -- i.e. still runs ``processNode`` and still fires ``htmx:load`` -- for
    OOB-inserted content. Reasoning says it does; this proves it, and would fail loudly (the click
    would wait out its timeout on a button nothing ever enables) if it did not.

    The stakes are higher than "an undo button" suggests: the toast removes itself 10 seconds after
    it appears, so a lost Undo click is unrecoverable rather than merely repeatable, and the thing it
    reverses is an archive.
    """
    for attempt in range(_UNDO_RACE_ATTEMPTS):
        files = await seed.duplicate_group(count=3)
        group_hash = files[0].sha256_hash

        await open_shell(page, "/s/dedupe")
        await page.evaluate("window.__documentAlive = true")

        # Step 1 stages the plan; it is raced by the test above, so here it goes through click_swap
        # to keep this test's failures attributable to the toast alone.
        await click_swap(page, f"#dupe-group-{group_hash} button[type=submit]")
        # Step 2 commits it and OOB-delivers the toast. Deliberately NOT click_swap: waiting for
        # afterSettle would run the very settle task that releases the toast's guard, which is the
        # window under test.
        await page.click(f"#dupe-group-{group_hash} button[type=submit]")

        # THE RACE: a bare click on the OOB-delivered Undo, the instant it attaches.
        await page.click("#toast-container button[type=submit]")

        await page.wait_for_selector(f"#dupe-group-{group_hash}", timeout=5_000)
        assert await page.evaluate("window.__documentAlive === true"), f"attempt {attempt}: the undo click fell through to a native submission"
        assert "file_states=" not in page.url, f"attempt {attempt}: the undo click fell through to the native GET fallback: {page.url}"


async def test_bulk_approving_changes_the_instant_the_list_swaps_in_still_patches(page: Any, seed: Any) -> None:
    """``_changes_list.html``'s ``hx-patch`` bulk form: AC3, proven rather than argued by analogy.

    Browsers implement no native PATCH. A ``<form>`` with ``hx-patch``, no ``method`` and no
    ``action`` therefore degrades to exactly the same native GET back to the current URL as an
    ``hx-post`` one -- the verb never reaches the wire at all -- so both the defect and the fix
    transfer. "They transfer" is nonetheless the kind of claim this bead exists because someone
    asserted it without checking, so both halves are asserted below: no native navigation happened,
    and the PATCH really landed.

    This form also needed the guard applied differently, which is its own reason to race it. Its two
    buttons already carry an Alpine ``:disabled`` binding, and Alpine binds in the microtask after
    the swap while shell.html's listener runs from htmx's settle task ~20 ms later -- so the generic
    ``btn.disabled = false`` would land last and leave "Approve selected eligible" clickable with
    nothing selected. The guard therefore stays on the buttons unchanged and the business rule moved
    up to a wrapping ``<fieldset>`` Alpine owns alone (see the template comment). This test drives
    the composition: the click can only succeed if the guard released AND the fieldset agrees.

    Why ``htmx.ajax`` rather than clicking a facet tab
    ==================================================

    The natural operator gesture -- check a row, then click a facet tab -- cannot serve as the swap
    here, because the selection does not survive it. ``sync()`` rewrites each ``[data-changes-nav]``
    element's ``hx-get`` to carry ``selected=<id>``, and the attribute really does change, but htmx
    captures a verb's path in the trigger-handler closure at ``processNode`` time (htmx 2.0.10
    ``processVerbs``) and never re-reads the attribute at request time. The tab therefore requests
    the URL it was SERVER-RENDERED with -- ``selected=`` empty -- and the swapped-in list comes back
    with an empty selection, leaving the fieldset correctly disabled and nothing to race.

    That is a real defect in the selection-carrying behaviour, found while writing this test and
    handed back for its own bead rather than fixed here -- it changes a write's scope and deserves
    its own review. It is emphatically NOT this guard: instrumenting the same swap showed
    ``htmx:load`` firing on the new list and the guard releasing correctly. Rather than couple this
    regression test to a bug it does not own, the swap is issued directly: the same
    ``htmx.ajax`` -> deferred settle -> ``htmx:load`` path, with a URL that carries the selection.
    ``htmx.ajax``'s promise is deliberately NOT awaited -- awaiting it would settle the swap and
    dissolve the very race this exists to lose.
    """
    target = await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")

    await open_shell(page, "/s/rename")
    await page.evaluate("window.__documentAlive = true")

    list_id = await page.evaluate("() => document.querySelector('#changes-bulk-form').closest('[x-data]').id")
    assert list_id, "the changes list container has no id to swap against"

    # The pre-swap marker is the barrier, and it is chosen to be the weakest one that is still
    # unambiguous: it waits for the new node to be ATTACHED and nothing more, which is exactly the
    # window phaze-boyl9 measured the native-GET fallback in. Waiting for ``#changes-bulk-form``
    # alone would be satisfied by the OUTGOING node and would race the swap instead of the guard.
    await page.evaluate("document.querySelector('#changes-bulk-form').dataset.preSwap = '1'")
    await page.evaluate(
        """([id, selected]) => {
            const url = `/s/rename?status=all&page=1&page_size=25&sort=confidence&order=asc&selected=${selected}`;
            htmx.ajax('GET', url, { target: `#${id}`, swap: 'outerHTML' });
        }""",
        [list_id, str(target.id)],
    )
    await page.wait_for_function(
        """() => {
            const form = document.querySelector('#changes-bulk-form');
            return form !== null && form.dataset.preSwap === undefined;
        }"""
    )

    # THE RACE: the swapped-in form is attached but not necessarily processed by htmx yet.
    await page.click("button:has-text('Approve selected eligible')")

    await page.wait_for_function("() => document.body.innerText.includes('proposal approved')")
    assert await page.evaluate("window.__documentAlive === true"), "the bulk approve click fell through to a native form submission"
    assert "review_tokens=" not in page.url, f"the hx-patch click fell through to the native GET fallback: {page.url}"


# --- The guard must not be a new way for a button to be stuck ---------------------------------
#
# Everything above asks "can a premature click still fall through?". These two ask the opposite, and
# they exist because the fix's own failure mode is the mirror image of the bug: a guard that is never
# released leaves a permanently dead control, and a guard applied over a button that was ALREADY
# conditionally disabled can enable it when it should not be.


async def test_every_guarded_button_on_its_own_route_is_released_by_htmx(page: Any, seed: Any) -> None:
    """The three forms not raced above, each reached the way an operator reaches it.

    A race test is not the only thing the three unraced forms are owed. Their guards are inert markup
    until shell.html's listener sees them, and "the same listener handles them" is an argument, not
    an observation -- the class of claim that keeps shipping through green suites here. So each is
    driven to its real route by a real htmx swap and asserted to come out USABLE: ``data-hx-guard``
    removed (the listener ran on this subtree) and the control not ``:disabled`` (the browser's own
    definition of activatable, which is what decides whether a click submits at all -- not
    Playwright's, and not the attribute the template emitted).

    This is deliberately weaker than the races above and the difference matters: it proves the guard
    is RELEASED on these routes, not that the pre-release window is closed on them. That second claim
    rests on the shared listener, which the four races do exercise.

    ``comparison_table.html``'s "Review decision" form used to be a fourth, listed here as absent
    because it was unreachable -- nothing in the v7 shell linked to ``GET /duplicates/{hash}/compare``,
    so there was no operator route to drive. phaze-ur8o3 verified that and deleted the route and the
    template, which is why only three remain. Recording the unreachable form here rather than papering
    over it with a synthesized request is what made that removal an evidenced decision.

    The barrier is a wait for the guard to clear, never ``settled()`` plus a read. ``settled()``
    polls for zero in-flight requests and returns BEFORE htmx's deferred settle task has run, so a
    count taken straight after it sees guards that are about to be released one tick later -- which
    is exactly the false positive this test reported on its first draft.
    """
    await seed.agent(scan_roots=["<archive-mount>/incoming", "<archive-mount>/sets"])

    async def released(where: str, selector: str, what: str) -> None:
        await page.wait_for_function(
            "sel => { const els = [...document.querySelectorAll(sel)]; return els.length > 0 && els.every(el => !el.hasAttribute('data-hx-guard')); }",
            arg=selector,
            timeout=5_000,
        )
        still_disabled = await page.evaluate("sel => [...document.querySelectorAll(sel)].filter(el => el.matches(':disabled')).length", selector)
        assert still_disabled == 0, f"{where}: {still_disabled} {what} stayed disabled after htmx processed them — the guard became a dead control"

    # trigger_scan_card.html, on the Discover workspace.
    await open_shell(page, "/s/summary")
    await page.click("[data-rail-stage='discover']")
    await released("discover", "#stage-workspace form button[type=submit]", "Start Scan buttons")

    # empty_state.html, on the Analyze workspace with zero files — the first-run guide, whose whole
    # purpose is getting the first scan started, so a dead button here is the worst place for one.
    await page.click("[data-rail-stage='analyze']")
    await released("analyze empty state", "button[aria-label^='Scan ']", "per-root Scan buttons")

    # _force_skip_dialog.html, in the record slide-in. Its submit lives inside an Alpine `x-show`
    # modal that is still hidden at this point, which is the reason to check it: the guard is
    # released by the record-pane swap, not by opening the modal, so a hidden control must come out
    # of the swap already live.
    target = await seed.file(filename="<track-01>.mp3")
    await open_shell(page, "/s/files")
    await settled(page)
    # Scoped to the DESKTOP table the same way test_files_record.py scopes it: the responsive list
    # renders a second control with the same hx-get, and an unscoped selector is two matches.
    await page.click(f'#files-table-view .md\\:block button[hx-get="/record/{target.id}"]')
    await page.wait_for_selector("#record-body h2")
    await released("record force-skip", "#record-body form button[type=submit]", "force-skip submits")


async def test_the_guard_does_not_enable_a_bulk_action_that_business_logic_disabled(page: Any, seed: Any) -> None:
    """The blast-radius risk of this whole change, asserted rather than argued.

    Two of the ten guarded buttons -- ``_changes_list.html``'s bulk approve and reject -- were
    ALREADY conditionally disabled before this change, by Alpine's ``:disabled="selected.length ===
    0"``. shell.html's listener enables a ``[data-hx-guard]`` button unconditionally, so applying
    the guard to those two directly would have made "Approve selected eligible" clickable with
    nothing selected: a bulk write against an empty selection, introduced by a fix for a different
    bug. That is why the guard stayed on the buttons and the business rule moved up to a wrapping
    ``<fieldset>`` (see the template comment), and this is the test that says the composition holds
    rather than the comment saying it.

    ``:disabled`` is read from the browser, not from Playwright and not from the attribute: a
    control is "actually disabled" if it is disabled OR sits inside a disabled fieldset, and that
    computed state -- not the markup -- is what decides whether a click activates a submit.
    Asserting ``el.hasAttribute('disabled')`` would report the button as ENABLED here and pass a
    broken composition.
    """
    target = await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")

    await open_shell(page, "/s/rename")
    approve = "button[value='approve_eligible']"
    reject = "button[value='reject']"

    # First: the guard really was released, so a failure below is about business logic and not about
    # a button that simply never woke up.
    await page.wait_for_function(
        "sel => { const el = document.querySelector(sel); return el !== null && !el.hasAttribute('data-hx-guard'); }",
        arg=approve,
        timeout=5_000,
    )

    for selector, label in ((approve, "Approve selected eligible"), (reject, "Reject selected pending")):
        assert await page.evaluate("sel => document.querySelector(sel).matches(':disabled')", selector), (
            f"{label!r} is clickable with nothing selected — the guard's release overrode the business rule that disabled it"
        )
    assert not await page.locator(approve).is_enabled(), (
        "Playwright disagrees with :disabled about the bulk approve button — actionability would not protect a raced click"
    )

    # And the rule is a rule, not a permanent lock: selecting a row must still enable them.
    await page.get_by_label("Select <set-01>.mp3").check()
    await page.wait_for_function("sel => !document.querySelector(sel).matches(':disabled')", arg=approve, timeout=5_000)
    assert str(target.id) in page.url, "the selection never reached the URL, so this assertion proved nothing about the fieldset"
