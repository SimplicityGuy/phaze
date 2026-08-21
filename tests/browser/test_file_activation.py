"""One file-activation contract across Files, Analyze, and Tracklists."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.browser.helpers import open_shell, settled, settled_focus, swap_settles


pytestmark = pytest.mark.browser

_INTERACTIVE_DESCENDANT_GUARD = "a,button,input,select,textarea,summary,details,[role=button],[role=separator]"


async def _wait_for_record(page: Any, file_id: Any) -> None:
    await page.wait_for_selector(f'#record-body [data-file-id="{file_id}"]')
    await page.wait_for_function("() => document.getElementById('record-body').checkVisibility()")


async def _close_and_expect_opener(page: Any, opener: str) -> None:
    await page.keyboard.press("Escape")
    await page.wait_for_function("() => !document.getElementById('record-body').checkVisibility()")
    await settled_focus(page)
    assert await page.evaluate("selector => document.activeElement === document.querySelector(selector)", opener)


async def _record_request_counter(page: Any, file_id: Any) -> None:
    await page.evaluate(
        """fileId => {
            window.__recordRequests = 0;
            document.body.addEventListener('htmx:beforeRequest', event => {
                const path = event.detail && event.detail.requestConfig && event.detail.requestConfig.path;
                if (path === '/record/' + fileId) window.__recordRequests++;
            });
        }""",
        str(file_id),
    )


async def test_files_row_keyboard_and_details_button_share_one_opener_contract(page: Any, seed: Any) -> None:
    """A post-filter row opens by Enter; its nested Details button issues one request, not two."""
    target = await seed.file(filename="<track-01>.mp3")
    await open_shell(page, "/s/files")
    await page.wait_for_selector(f'#files-table-view tr[hx-get="/record/{target.id}"]')

    # Exercise a real filtered HTMX replacement before activation, then return to the populated set.
    async with swap_settles(page):
        await page.select_option("#filter-stage", "metadata")
    async with swap_settles(page):
        await page.select_option("#filter-bucket", "not_started")

    row = f'#files-table-view tr[hx-get="/record/{target.id}"]'
    await page.locator(row).focus()
    async with swap_settles(page):
        await page.keyboard.press("Enter")
    await _wait_for_record(page, target.id)
    await _close_and_expect_opener(page, row)

    details = f'#files-table-view .md\\:block button[hx-get="/record/{target.id}"]'
    await _record_request_counter(page, target.id)
    async with swap_settles(page):
        await page.click(details)
    await _wait_for_record(page, target.id)
    assert await page.evaluate("window.__recordRequests") == 1, "the Details click bubbled into the row and loaded the record twice"
    await _close_and_expect_opener(page, details)


async def test_analyze_row_opens_after_a_filtered_htmx_swap_and_returns_focus(page: Any, seed: Any) -> None:
    """Analyze keeps the same mouse activation after its lazy table is replaced by a status lens."""
    target = await seed.file(filename="<set-01>.mp3")
    await seed.analysis(target)
    await open_shell(page, "/s/analyze")
    await page.wait_for_selector(f'#analyze-files-view tr[hx-get="/record/{target.id}"]')

    async with swap_settles(page):
        await page.select_option("#analyze-filter-status", "completed")

    row = f'#analyze-files-view tr[hx-get="/record/{target.id}"]'
    async with swap_settles(page):
        await page.click(f"{row} td:first-child")
    await _wait_for_record(page, target.id)
    await _close_and_expect_opener(page, row)


async def test_analyze_files_view_load_trigger_does_not_clobber_a_later_filtered_row(page: Any, seed: Any) -> None:
    """phaze-yzj7l: force the race the intermittent above only hit ~6% of the time.

    THE DEFECT: analyze_workspace.html:94's ``#analyze-files-view`` carries
    ``hx-get="/pipeline/analyze-files" hx-trigger="load"`` -- fired ONCE, when the shell first
    renders the page. ``_analyze_files.html``'s filter form (plus its "Clear filter" link and
    pager buttons) issues INDEPENDENT ``hx-get``s at the SAME target, with the SAME
    ``hx-swap="innerHTML"`` and no ``hx-sync``. Nothing constrains DELIVERY order to match ISSUE
    order: the load-trigger fetch is issued first but is not guaranteed to LAND first. If it is
    still in flight (a cold DB pool, a slow query -- see the bead for the measured 16.2s vs
    ~6.0s cold-boot outlier) when a filtered fetch issued later both lands AND is acted on -- the
    operator sees the filtered row, clicks it, a drawer opens holding that exact ``<tr>`` -- the
    stale load-trigger response can still arrive afterward. Its ``innerHTML`` swap is
    unconditional: it replaces ``#analyze-files-view`` with the DEFAULT view regardless of what
    is currently there, destroying the specific DOM node the drawer's ``opener`` points to (a
    brand-new node with the same file, same selector, replaces it -- record_host.html's `hide()`
    is why a mere `document.querySelector` re-check would miss this: it needs the SAME reference,
    not a matching one).

    Reproducing this naturally needs a genuinely slow cold boot (~6% of attempts, per phaze-39eiy's
    corroboration batch) -- a clean 40-run sample has a 7.6% chance of happening with NO fix at
    all, so stochastic evidence cannot carry this (bead AC5). This forces the exact ordering
    instead: hold the load-trigger's response in flight via network interception, issue a second
    request at the same target while it is held (mirroring the filter's own change-triggered
    fetch, which is what races it in the wild), let the operator click the resulting row and open
    the drawer, THEN release the held response. Verified in both directions on a git-diff-checked
    template swap (mirrors phaze-39eiy's own verification method): fails at the assertion below
    against the pre-fix templates (the held response lands and disconnects the opener), passes
    against the fix (``hx-sync="#analyze-files-view:replace"`` mirrored across every trigger that
    targets this container aborts whichever request is still in flight once a newer one for the
    same target is underway, so the container only ever ends up reflecting the LATEST one).
    """
    target = await seed.file(filename="<set-01>.mp3")
    await seed.analysis(target)

    held: dict[str, Any] = {"event": asyncio.Event()}

    async def hold_the_load_trigger_request(route: Any) -> None:
        # The load-trigger's own fetch carries no query string; every filter/page/clear request
        # carries at least `status=`. Only the former gets held.
        if "status=" not in route.request.url:
            held["route"] = route
            held["event"].set()
        else:
            await route.continue_()

    await page.route("**/pipeline/analyze-files*", hold_the_load_trigger_request)
    await page.goto("/s/analyze", wait_until="domcontentloaded")
    await page.wait_for_selector("#stage-workspace", state="attached")

    # The load-trigger fetch is now held -- #analyze-files-view is still showing its "Loading
    # files..." placeholder, and the filter form does not exist in the DOM yet (it ships as part
    # of the very response being held). A real racing request at this point cannot come from the
    # filter form for that reason; it is issued directly at the same target, from a plain,
    # unrelated source element (document.body carries no hx-sync of its own), which is exactly
    # what an uncoordinated hx-get from any OTHER element targeting this container looks like.
    await asyncio.wait_for(held["event"].wait(), timeout=10)
    row = f'#analyze-files-view tr[hx-get="/record/{target.id}"]'
    await page.evaluate(
        """() => new Promise(resolve => {
            const el = document.getElementById('analyze-files-view');
            document.body.addEventListener('htmx:afterSettle', function onSettle(evt) {
                if (evt.detail && evt.detail.target === el) {
                    document.body.removeEventListener('htmx:afterSettle', onSettle);
                    resolve();
                }
            });
            window.htmx.ajax('GET', '/pipeline/analyze-files?status=completed', {source: document.body, target: el, swap: 'innerHTML'});
        })"""
    )
    assert await page.locator(row).count() == 1, "the second (filtered) request never rendered the target row"

    # Capture a REFERENCE to this exact node -- not just its selector -- exactly like
    # record_host.html's opener capture does ($event.detail.el). Re-querying by selector after
    # the clobber would match a brand-new row for the same file and falsely read as unaffected.
    await page.evaluate(f"window.__phazeOpener = document.querySelector('{row}')")
    async with swap_settles(page):
        await page.click(f"{row} td:first-child")
    await _wait_for_record(page, target.id)

    # NOW release the load-trigger's held response -- landing well after the filtered one, and
    # well after the operator has already opened a record from a row it rendered.
    await held["route"].continue_()
    await page.wait_for_timeout(1000)

    assert await page.evaluate("window.__phazeOpener.isConnected") is True, (
        "the delayed load-trigger response disconnected the row the drawer's opener still points to"
    )


async def test_analyze_files_view_sync_favors_the_latest_of_two_real_form_triggers(page: Any, seed: Any) -> None:
    """phaze-yzj7l: confirm hx-sync="replace" empirically in a SECOND pair -- not load-vs-anything.

    The first test above proves the load-trigger fetch can no longer clobber a later filter
    change. It exercises only one of the four triggers that now share
    #analyze-files-view's hx-sync bucket (the div's own load trigger); this test exercises TWO
    of the other three -- the "Clear filter" link and the filter ``<select>`` -- to confirm the
    coordination is the general "latest ISSUED request wins" contract the fix claims, not a
    load-specific special case.

    Sequence: apply a filter (so "Clear filter" exists), then hold ITS response in flight, then
    change the filter to a DIFFERENT value while it is held. The later action (the filter change)
    must win even though the earlier one (Clear filter) is released afterward and would, without
    coordination, be the LAST response to physically land and therefore overwrite it.
    """
    target = await seed.file(filename="<set-01>.mp3")
    await seed.analysis(target)

    await open_shell(page, "/s/analyze")
    await page.wait_for_selector(f'#analyze-files-view tr[hx-get="/record/{target.id}"]')

    async with swap_settles(page):
        await page.select_option("#analyze-filter-status", "completed")
    assert await page.locator('button:has-text("Clear filter")').count() == 1

    held: dict[str, Any] = {"event": asyncio.Event()}

    async def hold_the_clear_filter_request(route: Any) -> None:
        # Clear filter's own request carries no `status=`; the second select changes below
        # always do. Only the former gets held.
        if "status=" not in route.request.url:
            held["route"] = route
            held["event"].set()
        else:
            await route.continue_()

    await page.route("**/pipeline/analyze-files*", hold_the_clear_filter_request)
    await page.click('button:has-text("Clear filter")')
    await asyncio.wait_for(held["event"].wait(), timeout=10)

    # While Clear filter's request is held in flight, issue the LATER action: change the filter
    # to a different value. Its hx-sync="#analyze-files-view:replace" should abort the held
    # request and become the one that lands.
    async with swap_settles(page):
        await page.select_option("#analyze-filter-status", "failed")
    assert await page.evaluate("document.getElementById('analyze-filter-status').value") == "failed", (
        "the later filter change did not win over the earlier, still-in-flight Clear filter request"
    )

    # NOW release Clear filter's held response -- landing well after the filter change already
    # settled. Without coordination this is exactly what would revert the view.
    await held["route"].continue_()
    await page.wait_for_timeout(1000)

    assert await page.evaluate("document.getElementById('analyze-filter-status').value") == "failed", (
        "the earlier Clear filter request's delayed response overwrote the later filter change"
    )


async def test_tracklist_rows_distinguish_matched_files_from_candidates_after_paging(page: Any, seed: Any) -> None:
    """Only the exact matched row opens, at #tracklist, after a paged HTMX replacement."""
    target = await seed.file(filename="<set-01>.mp3")
    for index in range(9):
        await seed.tracklist(artist=f"Paging Candidate {index}", external_id=f"page-{index}")
    # Seed the two asserted rows last so newest-first page 1 still contains both after Next/Previous.
    await seed.tracklist(file=target, artist="Matched Artist", external_id="matched-set")
    await seed.tracklist(artist="Unmatched Candidate", external_id="candidate-set")

    await open_shell(page, "/s/tracklist")
    await page.wait_for_selector("#tracklist-set-table")
    await settled(page)
    candidate = page.locator("#tracklist-sets-view tbody tr", has_text="Unmatched Candidate")
    candidate_state = await candidate.evaluate(
        """row => ({
            url: row.getAttribute('hx-get'),
            tabindex: row.getAttribute('tabindex'),
            recordRow: row.getAttribute('data-record-row'),
            className: row.className,
        })"""
    )
    assert candidate_state["url"] is None
    assert candidate_state["tabindex"] is None
    assert candidate_state["recordRow"] is None
    assert "cursor-pointer" not in candidate_state["className"]

    matched = f'#tracklist-sets-view tr[hx-get="/record/{target.id}"]'
    # Re-render page 1 under the smallest supported page size, then use its real Next/Previous controls.
    async with swap_settles(page):
        await page.evaluate("window.htmx.ajax('GET', '/pipeline/tracklist-sets?page_size=10', {target:'#tracklist-sets-view', swap:'innerHTML'})")
    if await page.locator('#tracklist-sets-view button:text-is("Next")').count():
        async with swap_settles(page):
            await page.click('#tracklist-sets-view button:text-is("Next")')
        if not await page.locator(matched).count():
            async with swap_settles(page):
                await page.click('#tracklist-sets-view button:text-is("Previous")')

    await page.wait_for_selector(matched)

    trigger = await page.locator(matched).get_attribute("hx-trigger")
    assert _INTERACTIVE_DESCENDANT_GUARD in (trigger or "")
    await page.locator(matched).focus()
    async with swap_settles(page):
        await page.keyboard.press("Enter")
    await _wait_for_record(page, target.id)

    await page.wait_for_function("() => window.Alpine.$data(document.getElementById('record-body')).requestedSection === 'tracklist'")
    positioned = await page.evaluate(
        """() => {
            const panel = document.getElementById('record-body').closest('[role=dialog]');
            const section = document.querySelector('#record-body #tracklist');
            const panelRect = panel.getBoundingClientRect();
            const sectionRect = section.getBoundingClientRect();
            return sectionRect.bottom > panelRect.top && sectionRect.top < panelRect.bottom;
        }"""
    )
    assert positioned, "the matched Tracklists row did not bring #tracklist into the drawer viewport"
    await _close_and_expect_opener(page, matched)


async def test_escape_falls_back_to_the_stage_heading_when_the_opener_row_has_gone_stale(page: Any, seed: Any) -> None:
    """phaze-39eiy: focus must not strand on <body> when the opener is gone by the time Escape runs.

    Measured in the wild (recorded in the since-deleted ``tests/browser/FLAKE_RECORD.md``; see
    ADR-0009 § "The browser contract suite"): the Analyze workspace's own filter re-render can race
    its container's ``hx-trigger="load"`` fetch and replace ``#analyze-files-view``'s rows while the
    drawer a click just opened is still up -- disconnecting the exact ``<tr>`` record_host.html's
    ``hide()`` was holding onto as its focus-restore target. ``.focus()`` on a disconnected element
    is a silent no-op (no error, no event), so without a liveness check the drawer's Escape handler
    left focus on ``<body>`` with nothing to signal it -- 5/80 cold-boot attempts, all with an
    identical signature: ``settled_focus`` exhausting its full timeout, ``document.activeElement``
    left on ``BODY``.

    Rather than wait on that ~6% race, this forces its exact precondition directly -- opener
    disconnected from the DOM while the drawer is still open -- so the guard is proven
    deterministically instead of probabilistically. It fails against the pre-fix ``hide()`` (focus
    stays on ``<body>``) and passes against the fix, which falls back to
    ``window._focusStageHeading()`` -- the same fallback rail.html's ``closeNav()`` already uses for
    the identical "documented target may not actually take focus" problem (phaze-bdeih) -- rather
    than a bare ``#stage-workspace`` ``.focus()`` call, which would itself be a no-op: that div
    carries no ``tabindex``.
    """
    target = await seed.file(filename="<set-01>.mp3")
    await open_shell(page, "/s/files")
    await page.wait_for_selector(f'#files-table-view tr[hx-get="/record/{target.id}"]')

    row = f'#files-table-view tr[hx-get="/record/{target.id}"]'
    await page.locator(row).focus()
    async with swap_settles(page):
        await page.keyboard.press("Enter")
    await _wait_for_record(page, target.id)

    # Force the race's precondition directly: disconnect the opener the drawer is holding onto,
    # without touching the drawer itself (still open, still showing the record).
    await page.evaluate("selector => document.querySelector(selector)?.remove()", row)
    assert await page.evaluate("selector => document.querySelector(selector) === null", row), (
        "the opener row is still connected -- this test isn't forcing the precondition it claims to"
    )

    await page.keyboard.press("Escape")
    await page.wait_for_function("() => !document.getElementById('record-body').checkVisibility()")
    await settled_focus(page)

    stranded_on_body = await page.evaluate("() => document.activeElement === document.body")
    assert not stranded_on_body, "focus was stranded on <body> after Escape closed a drawer whose opener had gone stale"
    landed_in_workspace = await page.evaluate("() => document.getElementById('stage-workspace').contains(document.activeElement)")
    assert landed_in_workspace, "focus did not fall back into the stage workspace when the opener was gone"
