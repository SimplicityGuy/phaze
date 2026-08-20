"""One file-activation contract across Files, Analyze, and Tracklists."""

from __future__ import annotations

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
