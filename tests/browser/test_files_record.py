"""phaze-tzy6s.14 (CR-14-2): the Files workspace and the record slide-in it opens.

``/s/files`` was reachable from the suite but never driven: the all-workspace console-error sweep
loaded it once and the responsive matrix measured it for overflow, and nothing clicked anything on
it. Everything below needs a browser, and two of them need a browser holding SEEDED rows -- an
empty Files table renders neither a Details button nor a row to filter away.

The record slide-in is the "files detail" half. It is chrome that lives OUTSIDE ``#stage-workspace``
(``shell/partials/record_host.html``), is opened by an Alpine ``record:open`` event dispatched from a
row, and is FILLED by a separate htmx GET. Server-side that is three unrelated fragments; only a
browser can say whether a click on a row in one of them ends with the right file's record on screen,
focused, and dismissible back to where the operator was.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.helpers import click_swap, open_shell, settled, settled_focus, swap_settles


pytestmark = pytest.mark.browser


# The desktop table and the sub-``md`` card list BOTH render a Details button per row, wired to the
# same ``/record/{id}``. A bare ``[hx-get="/record/..."]`` therefore matches two elements and
# Playwright's strict mode fails the click -- scope to the ``hidden md:block`` table wrapper.
_DESKTOP_TABLE = "#files-table-view .md\\:block"

# An INVENTED filename in the product's naming shape (CLAUDE.md sanctions these explicitly), used by
# the ⌘K path only. It has to be word-shaped for the reason test_command_palette_search.py documents
# at length: ``/search/`` is Postgres full-text search, so a ``<track-01>.mp3`` placeholder is one
# opaque lexeme with no searchable word in it and no query would ever surface it as a palette row.
_PALETTE_FILENAME = "Example Artist - Norwood Festival - Opening Set (2024).mp3"


def _details_button(file_id: Any) -> str:
    return f'{_DESKTOP_TABLE} button[hx-get="/record/{file_id}"]'


async def _record_dialog_label(page: Any) -> str:
    """The record panel's live ``aria-label``.

    Read through ``#record-body``'s own ancestor rather than by selecting on
    ``[role=dialog][aria-modal=true]``: the command palette (``cmdk_modal.html``) is a second element
    matching that selector and is earlier in the document, so a selector-based read silently asserts
    against the palette instead -- it returned "Command palette" on the first draft of this file.
    """
    return str(await page.evaluate("document.getElementById('record-body').closest('[role=dialog]').getAttribute('aria-label')"))


async def _wait_for_record(page: Any) -> None:
    """Wait until the record body has swapped in AND been revealed.

    The after-swap handler refines the aria-label and flips ``loaded`` (which is what reveals
    ``#record-body`` through ``x-show``), so waiting on the heading alone can observe the panel one
    Alpine tick before either has happened. Visibility is the honest post-condition: it is the last
    of the three to land.
    """
    await page.wait_for_selector("#record-body h2")
    await page.wait_for_function("() => document.getElementById('record-body').checkVisibility()")


async def _settled_focus_in_record(page: Any, *, timeout_ms: int = 5_000) -> str:
    """Wait, bounded, for focus to land inside ``#record-body``; describe where it ended up.

    A bounded wait rather than a bare read, for the reason ``helpers.settled_focus`` documents on the
    dismiss side: the focus move is queued in JavaScript, so it lands AFTER the post-condition a test
    can wait on. Here it is queued behind the x-show reveal specifically (phaze-f65nu), so it is one
    or more animation frames later than ``#record-body`` becoming visible -- reading
    ``document.activeElement`` the instant ``_wait_for_record`` returns is a race that passes on a
    fast machine and fails on a slow one.

    Returns a description of the focused element either way, so the caller's assertion can name what
    it got instead of raising a bare selector timeout.
    """
    describe = """() => {
        const el = document.activeElement;
        if (! el) return 'nothing';
        const inside = document.getElementById('record-body').contains(el);
        return (inside ? 'IN ' : 'OUT ') + el.tagName + '[' + (el.getAttribute('aria-label') || el.textContent.trim().slice(0, 40)) + ']';
    }"""
    for _ in range(max(1, timeout_ms // 100)):
        where = str(await page.evaluate(describe))
        if where.startswith("IN "):
            return where
        await page.wait_for_timeout(100)
    return str(await page.evaluate(describe))


async def test_opening_a_file_record_names_that_file(page: Any, seed: Any) -> None:
    """The open contract of the record slide-in, which no server-side assertion reaches.

    Three separate things have to happen for one click, across fragments the server renders
    independently and never together: the Alpine ``record:open`` dispatch has to flip the host
    visible, the htmx GET has to land in ``#record-body``, and ``hx-on::after-swap`` has to refine
    the dialog's ``aria-label`` from the generic "File record" to the loaded file's name.

    The aria-label refinement is the accessibility crux: the panel is ``role="dialog"``
    ``aria-modal="true"`` with the shell behind it inert, so its label is the ONLY thing announcing
    which file a screen-reader operator is now looking at. It is computed in JavaScript from the
    swapped heading's ``textContent`` -- there is no server-rendered form of it to assert on.

    Whether focus actually ENTERS the dialog is a separate claim, asserted on its own by
    ``test_opening_a_record_moves_focus_into_the_dialog`` below (phaze-f65nu).
    """
    target = await seed.file(filename="<track-01>.mp3")
    await seed.file(filename="<track-02>.m4a", file_type="m4a")

    await open_shell(page, "/s/files")
    await settled(page)

    assert await _record_dialog_label(page) == "File record", "the record panel is labelled for a file before any file has been opened"

    await page.click(_details_button(target.id))
    await _wait_for_record(page)

    assert await _record_dialog_label(page) == "File record: <track-01>.mp3", (
        "the record dialog's aria-label was not refined to the opened file — a screen-reader operator is told only 'File record'"
    )
    assert await page.locator("#record-body").is_visible(), "the record body never became visible"
    assert target.current_path in await page.locator("#record-body").inner_text(), "the record shows a different file than the row that opened it"


async def test_record_drawer_opens_the_same_file_as_a_full_page(page: Any, seed: Any) -> None:
    """The drawer's ordinary link reaches an addressable page with stable section anchors."""
    target = await seed.file(filename="<track-01>.mp3")

    await open_shell(page, "/s/files")
    await settled(page)
    await page.click(_details_button(target.id))
    await _wait_for_record(page)

    full_page_link = page.locator(f'#record-body a[href="/files/{target.id}"]')
    # ``inner_text`` applies the link's intentional uppercase text-transform; the DOM label is the
    # stable product copy this assertion owns.
    assert (await full_page_link.text_content() or "").strip() == "Open full page"
    await full_page_link.click()
    await page.wait_for_url(f"**/files/{target.id}")

    assert await page.locator(f'[data-file-id="{target.id}"]').is_visible()
    assert target.current_path in await page.locator("main").inner_text()
    for anchor in ("analysis", "tracklist", "history"):
        assert await page.locator(f"#{anchor}").count() == 1
        assert await page.locator(f'a[href="#{anchor}"]').count() == 1


async def test_opening_a_record_moves_focus_into_the_dialog(page: Any, seed: Any) -> None:
    """A modal dialog must take the keyboard with it when it opens.

    phaze-f65nu, FOUND BY THIS TEST and now fixed: opening a record used to leave focus on the
    Details button that opened it -- an element ``x-trap.inert`` has just marked inert -- so an
    ``aria-modal`` dialog opened with the keyboard operator outside it and the refined aria-label was
    never announced. ``record_host.html``'s after-swap handler flipped ``loaded = true`` and called
    ``h.focus()`` on the next statement, but ``loaded`` is what reveals ``#record-body`` through
    ``x-show``, and Alpine defers that reveal onto a later task -- so the focus ran against a
    ``display:none`` element and silently no-opped. The handler now waits for the reveal.

    Kept as a running assertion rather than a comment: the panel is ``aria-modal="true"`` and inert-s
    the shell behind it, so "did focus follow the dialog" is not a nicety here -- it decides whether
    the operator is inside the trap or stranded next to it. Only a browser can answer it, because
    both the reveal and the focus call happen in JavaScript after the swap.
    """
    target = await seed.file(filename="<track-01>.mp3")

    await open_shell(page, "/s/files")
    await settled(page)
    await page.click(_details_button(target.id))
    await _wait_for_record(page)

    where = await _settled_focus_in_record(page)
    assert where.startswith("IN "), f"focus is outside the open record dialog — landed on {where}"
    assert await page.evaluate("document.activeElement.tagName") == "H2", (
        f"focus entered the record but not on its heading ({where}) — the heading is the tabindex=-1 landing "
        "point that names the opened file to a screen reader"
    )


async def test_opening_a_record_from_the_command_palette_also_moves_focus_into_it(page: Any, seed: Any) -> None:
    """The ⌘K Files row is the second opener of the SAME host, so it gets the same guarantee.

    A palette row and a Details button reach ``record_host.html`` by different routes -- different
    template, different fragment, and the palette additionally closes ITSELF on ``record:open`` -- but
    they share one after-swap handler, so covering only one of them leaves the other free to regress
    on its own. The palette path is also the harsher case: it dismisses the layer the operator was
    typing in, so a record that fails to take focus leaves it on a row inside a now-hidden palette
    rather than merely on a still-visible button.
    """
    await seed.file(filename=_PALETTE_FILENAME)

    await open_shell(page, "/s/files")
    await settled(page)

    await page.click("#cmdk-trigger")
    await page.locator("#cmdk-dialog").wait_for(state="visible")
    await page.fill("#cmdk-dialog input[name='q']", "Norwood")
    await page.wait_for_selector("#cmdk-file-1", timeout=15_000)
    await page.click("#cmdk-file-1")
    await _wait_for_record(page)

    assert await _record_dialog_label(page) == f"File record: {_PALETTE_FILENAME}", (
        "the palette opened a record that does not announce the row's file"
    )
    where = await _settled_focus_in_record(page)
    assert where.startswith("IN "), f"a record opened from the command palette left focus outside it — landed on {where}"


async def test_dismissing_a_record_returns_focus_to_the_row_that_opened_it(page: Any, seed: Any) -> None:
    """Esc closes the slide-in and puts the keyboard back where it was.

    ``record_host.html`` records the opener element on ``show()`` and refocuses it from a
    ``$nextTick`` on ``hide()`` -- deferred precisely because a synchronous focus is undone when the
    browser resets focus to ``<body>`` as the hidden panel's focused child is removed on the same
    tick. That ordering bug is invisible everywhere except in a real browser: the code reads
    correctly either way, and the symptom is a keyboard operator dumped at the top of the document
    after every record they close.
    """
    target = await seed.file(filename="<track-01>.mp3")

    await open_shell(page, "/s/files")
    await settled(page)
    await page.click(_details_button(target.id))
    await _wait_for_record(page)

    await page.keyboard.press("Escape")
    await page.wait_for_function("() => !document.getElementById('record-body').checkVisibility()")

    focused_label = await settled_focus(page, "aria-label")
    assert focused_label == f"Open details for {target.original_filename}", (
        f"Esc left focus on {focused_label!r} instead of returning it to the Details button that opened the record"
    )


async def test_opening_a_second_record_never_leaves_the_first_file_on_screen(page: Any, seed: Any) -> None:
    """phaze-7yet: the panel must not display the PREVIOUS file while the next one is in flight.

    ``show()`` flips the panel visible synchronously and always wins the race against the async
    ``GET /record/{id}``, so ``#record-body`` still holds the last open's content — including its live
    Approve / Skip controls, wired to the *previous* file's proposal ids — for the duration of the
    fetch. The guard is a ``loaded`` flag reset on every ``show()`` and flipped true only by the
    after-swap handler, which is a pure runtime state machine: the server renders one record fragment
    per request and cannot express "which file was on screen while this one loaded".

    Asserting on the dialog's ``aria-label`` rather than on the body text is deliberate. The label is
    reset to the generic "File record" only when a response carries no ``<h2>``; on a normal second
    open it goes straight from file A's name to file B's, so a stale label is the observable form of
    a stale panel.
    """
    first = await seed.file(filename="<track-01>.mp3")
    second = await seed.file(filename="<track-02>.m4a", file_type="m4a")

    await open_shell(page, "/s/files")
    await settled(page)

    await page.click(_details_button(first.id))
    await _wait_for_record(page)
    assert await _record_dialog_label(page) == "File record: <track-01>.mp3"

    await page.keyboard.press("Escape")
    await page.wait_for_function("() => !document.getElementById('record-body').checkVisibility()")

    await page.click(_details_button(second.id))
    await _wait_for_record(page)

    assert await _record_dialog_label(page) == "File record: <track-02>.m4a", "the second open still announces the first file"
    body = await page.locator("#record-body").inner_text()
    assert second.current_path in body, "the second file's record never landed"
    assert first.current_path not in body, "the previous file's record is still mounted underneath the second — the phaze-7yet stale-panel defect"


async def test_filtering_the_files_table_swaps_in_place_without_nesting_a_second_view(page: Any, seed: Any) -> None:
    """phaze-mrhq: the filter bar swaps CONTENT under ``#files-table-view``, never a second copy of it.

    The host/fragment split exists because ``files_table_view.html`` used to carry the same
    ``id="files-table-view"`` it was being swapped into, so every filter, sort and pager click nested
    another id-bearing div one level deeper. Duplicate ids are legal HTML: the server renders a valid
    fragment, every string assertion on it passes, and the damage is only visible once htmx has
    actually performed the swap into a live document. Counting the id after a real swap is the only
    way to see it.

    The filter is driven through ``select_option`` on the real ``<select>``s so the form's
    ``hx-trigger="change"`` fires the way an operator's does, rather than by navigating to the
    filtered URL (which would prove the route works and nothing about the swap). Both selects are
    set because ``_files_page_stmt`` narrows only when stage AND bucket are present -- a bucket on
    its own is accepted, echoed into the URL, and filters nothing.
    """
    await seed.file(filename="<track-01>.mp3")
    await seed.file(filename="<track-02>.m4a", file_type="m4a")

    await open_shell(page, "/s/files")
    await settled(page)
    assert await page.locator(f"{_DESKTOP_TABLE} tbody tr").count() == 2, "the seeded files are not in the table"
    await page.evaluate("window.__documentAlive = true")

    # No seeded file has a metadata failure, so Metadata=failed is a filter that matches nothing --
    # which is also the branch that has to keep the filter bar reachable so it can be cleared.
    # Each select is its own swap into #files-table-view, so the second must not be driven while the
    # first is still settling -- see helpers.swap_settles for the race that shape produces.
    async with swap_settles(page):
        await page.select_option("#filter-stage", "metadata")
    async with swap_settles(page):
        await page.select_option("#filter-bucket", "failed")
    await page.wait_for_function("() => document.querySelector('#files-table-view').innerText.includes('No failed files in Metadata')")

    assert await page.evaluate("window.__documentAlive === true"), "the filter reloaded the document instead of swapping"
    assert await page.locator("#files-table-view").count() == 1, "the filter response nested a second #files-table-view inside the first (phaze-mrhq)"
    assert "bucket=failed" in page.url, "hx-push-url did not carry the filter into the address bar, so the filtered view is not bookmarkable"
    assert await page.locator("#status-filter-bar").count() == 1, "the filter bar did not survive its own empty result — the filter cannot be cleared"

    async with swap_settles(page):
        await page.select_option("#filter-bucket", "")
    await page.wait_for_function("() => document.querySelectorAll('#files-table-view tbody tr').length === 2")
    assert await page.locator("#files-table-view").count() == 1, "clearing the filter nested a second #files-table-view"


async def test_sorting_a_column_re_renders_its_own_aria_sort(page: Any, seed: Any) -> None:
    """A sort click has to re-render the header that announces the sort, not just the rows.

    This is the phaze-xxp2 shape, on the Files table: a control that swaps a container must be
    rendered BY that swap, or it keeps announcing the state it had before the click. ``aria-sort``
    is the accessible signal for column order, and it lives on a ``<th>`` inside ``#files-table-view``
    -- the very element the sort click replaces. If the fragment ever stopped including the header
    row, sorting would still reorder the rows correctly while telling assistive technology the table
    is sorted by something else.
    """
    await seed.file(filename="<track-01>.mp3")
    await seed.file(filename="<track-02>.m4a", file_type="m4a")

    await open_shell(page, "/s/files")
    await settled(page)

    file_header = f'{_DESKTOP_TABLE} th:has(button:text-is("File"))'
    type_header = f'{_DESKTOP_TABLE} th:has(button:text-is("Type"))'
    assert await page.get_attribute(file_header, "aria-sort") == "ascending", "the default Files order is not announced on the File column"
    assert await page.get_attribute(type_header, "aria-sort") == "none"

    await click_swap(page, f"{type_header} button")
    await page.wait_for_function(
        """() => {
            const headers = [...document.querySelectorAll('#files-table-view th')];
            return headers.some(th => th.innerText.trim().startsWith('TYPE') && th.getAttribute('aria-sort') === 'ascending');
        }"""
    )

    assert await page.get_attribute(file_header, "aria-sort") == "none", (
        "the previously-sorted column still announces itself as sorted — two columns now claim to order the table"
    )
    assert "sort=type" in page.url, "the chosen order was not pushed, so a reload would silently drop it"
