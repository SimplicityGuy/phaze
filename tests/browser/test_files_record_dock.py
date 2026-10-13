"""phaze-7u4sy (C3): the docked, non-modal record inspector the Files stage uses at xl and up.

Every claim here is about what a real browser does, for the same reason ``test_files_record.py`` is
browser-driven: which host receives a record, where focus sits and which of two listeners runs first
are all decided in JavaScript after the swap. The modal half (below xl, and every non-Files stage) is
covered by ``test_files_record.py`` and ``test_record_fast_open.py`` at 1024/768; this file owns the
dock, the choice between the two hosts, and the xl boundary between them.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from tests.browser.helpers import open_shell, settled


if TYPE_CHECKING:
    from tests.browser.seed import Seeder


pytestmark = pytest.mark.browser

_DOCKED_WIDTHS = [1440, 1920]
_MODAL_WIDTHS = [1024, 768]
_PALETTE_FILENAME = "Example Artist - Norwood Festival - Closing Set (2024).mp3"


def _row(file_id: Any) -> str:
    return f'#files-table-view tr[hx-get="/record/{file_id}"]'


async def _two_files(seed: Any) -> tuple[Any, Any]:
    first = await seed.file(filename="<track-01>.mp3")
    second = await seed.file(filename="<track-02>.m4a", file_type="m4a")
    return first, second


async def _open_files(page: Any, width: int) -> None:
    await page.set_viewport_size({"width": width, "height": 1000})
    await open_shell(page, "/s/files")
    await settled(page)


async def _wait_for_dock_record(page: Any, file_id: Any) -> None:
    await page.wait_for_selector(f'#record-dock-body [data-file-id="{file_id}"]')
    await page.wait_for_function("() => document.getElementById('record-dock-body').checkVisibility()")


async def _record_trees(page: Any) -> dict[str, int]:
    return dict(
        await page.evaluate(
            """() => ({
                dock: document.querySelectorAll('#record-dock-body [data-file-id]').length,
                modal: document.querySelectorAll('#record-body [data-file-id]').length,
                total: document.querySelectorAll('[data-file-id]').length,
            })"""
        )
    )


async def _no_modal_residue(page: Any) -> None:
    assert await page.evaluate("document.querySelectorAll('[inert]').length") == 0, "an element was left inert"
    assert await page.evaluate("document.querySelectorAll('[data-record-inert]').length") == 0, "the record host's inert marker was left behind"


def _collect_record_gets(page: Any) -> list[str]:
    urls: list[str] = []
    page.on("request", lambda request: urls.append(request.url) if "/record/" in request.url and request.method == "GET" else None)
    return urls


@pytest.mark.parametrize("width", _DOCKED_WIDTHS)
async def test_row_click_fills_the_dock_and_keeps_focus_on_the_row(page: Any, seed: Seeder, width: int) -> None:
    first, _second = await _two_files(seed)
    gets = _collect_record_gets(page)
    await _open_files(page, width)

    await page.click(_row(first.id))
    await _wait_for_dock_record(page, first.id)
    await settled(page)

    assert len([u for u in gets if str(first.id) in u]) == 1, f"expected exactly one GET for the record, saw {gets}"
    assert await page.evaluate(
        'id => document.activeElement === document.querySelector(`#files-table-view tr[hx-get="/record/${id}"]`)', str(first.id)
    ), "opening the record moved focus off the row"
    assert await page.locator(_row(first.id)).get_attribute("aria-current") == "true"
    assert await page.locator("#record-dock").get_attribute("aria-label") == "File record: <track-01>.mp3"
    trees = await _record_trees(page)
    assert trees == {"dock": 1, "modal": 0, "total": 1}, f"more than one record tree is mounted: {trees}"
    # Not a dialog, not modal, nothing inert, no focus trap.
    assert await page.evaluate("document.getElementById('record-dock').matches('[role=dialog], [aria-modal]')") is False
    # A record's own nested confirm dialogs are closed (not rendered) and may exist; the dock adds none of its own.
    assert (
        await page.evaluate("[...document.querySelectorAll('#record-dock [aria-modal=true]')].filter(e => e.getClientRects().length > 0).length") == 0
    )
    assert await page.evaluate("document.querySelectorAll('#record-dock > [x-trap], #record-dock [x-ref=panel]').length") == 0
    await _no_modal_residue(page)
    assert await page.evaluate("!document.getElementById('record-body').checkVisibility()")
    # Stage-pill ids stay unique with the record mounted.
    assert await page.evaluate(
        "[...document.querySelectorAll('[id]')].map(e => e.id).length === new Set([...document.querySelectorAll('[id]')].map(e => e.id)).size"
    )


@pytest.mark.parametrize("width", _DOCKED_WIDTHS)
async def test_arrow_down_then_enter_replaces_the_record_and_focus_stays_on_the_row(page: Any, seed: Seeder, width: int) -> None:
    await _two_files(seed)
    gets = _collect_record_gets(page)
    await _open_files(page, width)

    rows = page.locator("#files-table-view tr[data-record-row]")
    assert await rows.count() == 2
    first_id = await rows.nth(0).get_attribute("hx-get")
    second_id = await rows.nth(1).get_attribute("hx-get")
    assert first_id and second_id

    await rows.nth(0).focus()
    await page.keyboard.press("Enter")
    await _wait_for_dock_record(page, first_id.rsplit("/", 1)[1])
    await page.keyboard.press("ArrowDown")
    assert await page.evaluate("(n) => document.activeElement === document.querySelectorAll('#files-table-view tr[data-record-row]')[n]", 1)
    assert await rows.nth(0).get_attribute("aria-current") == "true", "moving focus alone must not change the open record"
    await page.keyboard.press("Enter")
    await _wait_for_dock_record(page, second_id.rsplit("/", 1)[1])
    await settled(page)

    assert await page.evaluate("(n) => document.activeElement === document.querySelectorAll('#files-table-view tr[data-record-row]')[n]", 1), (
        "replacing the record moved focus off the row"
    )
    assert await rows.nth(0).get_attribute("aria-current") is None
    assert await rows.nth(1).get_attribute("aria-current") == "true"
    assert await _record_trees(page) == {"dock": 1, "modal": 0, "total": 1}
    assert [u.rsplit("/", 1)[1] for u in gets] == [first_id.rsplit("/", 1)[1], second_id.rsplit("/", 1)[1]], f"expected one GET per record: {gets}"
    await _no_modal_residue(page)


@pytest.mark.parametrize("width", _DOCKED_WIDTHS)
async def test_escape_closes_only_when_focus_is_inside_the_dock(page: Any, seed: Seeder, width: int) -> None:
    first, _second = await _two_files(seed)
    await _open_files(page, width)

    await page.click(_row(first.id))
    await _wait_for_dock_record(page, first.id)

    await page.keyboard.press("Escape")  # focus is on the row
    await page.wait_for_timeout(300)
    assert await page.evaluate("document.getElementById('record-dock').checkVisibility()"), "Escape on the row closed the dock"

    await page.focus('#record-dock-body button[aria-label="Close record"]')
    await page.keyboard.press("Escape")
    await page.wait_for_function("() => !document.getElementById('record-dock').checkVisibility()")
    await page.wait_for_function(
        'id => document.activeElement === document.querySelector(`#files-table-view tr[hx-get="/record/${id}"]`)', arg=str(first.id)
    )
    assert await page.locator(_row(first.id)).get_attribute("aria-current") is None
    assert await page.evaluate("document.getElementById('record-dock-body').children.length") == 0
    await _no_modal_residue(page)


@pytest.mark.parametrize("width", _DOCKED_WIDTHS)
async def test_the_dock_fits_beside_the_table_without_page_overflow_and_keeps_saved_widths(page: Any, seed: Seeder, width: int) -> None:
    first, _second = await _two_files(seed)
    await page.add_init_script("localStorage.setItem('phaze-pipeline-col-widths', JSON.stringify({File: 300, Metadata: 200}))")
    await _open_files(page, width)
    before = await page.evaluate("JSON.stringify(window.Alpine.store('colResize').widths)")

    await page.click(_row(first.id))
    await _wait_for_dock_record(page, first.id)

    geometry = await page.evaluate(
        """() => {
            const rect = (sel) => document.querySelector(sel).getBoundingClientRect();
            const wrapper = document.querySelector('#files-table-view .md\\\\:block .overflow-x-auto');
            const main = rect('main');
            const dock = rect('#record-dock');
            return {
                docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                bodyOverflow: document.body.scrollWidth - document.body.clientWidth,
                mainRight: main.right, dockLeft: dock.left, dockRight: dock.right, vw: window.innerWidth,
                tableScrolls: wrapper.scrollWidth > wrapper.clientWidth,
                mainScrolls: document.querySelector('main').scrollWidth > document.querySelector('main').clientWidth + 1,
                fileCol: window.Alpine.store('colResize').width('File'),
            };
        }"""
    )
    assert geometry["docOverflow"] == 0 and geometry["bodyOverflow"] == 0, geometry
    assert geometry["mainRight"] <= geometry["dockLeft"] + 1 and geometry["dockRight"] <= geometry["vw"] + 1, geometry
    assert geometry["tableScrolls"], f"the table should scroll by itself beside the dock: {geometry}"
    assert not geometry["mainScrolls"], f"the workspace scrolled sideways instead of only the table: {geometry}"
    assert geometry["fileCol"] == 300
    assert await page.evaluate("JSON.stringify(window.Alpine.store('colResize').widths)") == before
    assert await page.evaluate("localStorage.getItem('phaze-pipeline-col-widths')") == '{"File":300,"Metadata":200}'


@pytest.mark.parametrize("width", _MODAL_WIDTHS)
async def test_below_xl_the_modal_owns_the_record_and_the_dock_stays_empty(page: Any, seed: Seeder, width: int) -> None:
    first, _second = await _two_files(seed)
    await _open_files(page, width)

    await page.click(f'#files-table-view .md\\:block button[hx-get="/record/{first.id}"]' if width >= 768 else _row(first.id))
    await page.wait_for_selector(f'#record-body [data-file-id="{first.id}"]')
    assert await _record_trees(page) == {"dock": 0, "modal": 1, "total": 1}
    assert await page.evaluate("!document.getElementById('record-dock').checkVisibility()")
    assert await page.evaluate("document.querySelector('[x-ref=panel]').getAttribute('aria-modal')") == "true"


async def test_crossing_xl_with_the_modal_open_leaves_no_inert_or_trap(page: Any, seed: Seeder) -> None:
    first, _second = await _two_files(seed)
    await _open_files(page, 1024)
    await page.click(f'#files-table-view .md\\:block button[hx-get="/record/{first.id}"]')
    await page.wait_for_selector(f'#record-body [data-file-id="{first.id}"]')
    await page.wait_for_function("() => document.querySelectorAll('[data-record-inert]').length > 0")

    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.wait_for_function("() => document.querySelectorAll('[data-record-inert]').length === 0")
    await page.wait_for_function("() => !document.querySelector('[x-ref=panel]').checkVisibility()")
    await _no_modal_residue(page)
    assert await _record_trees(page) == {"dock": 0, "modal": 0, "total": 0}
    # No trap: Tab leaves the (closed) panel and reaches the page.
    await page.keyboard.press("Tab")
    assert await page.evaluate("!document.querySelector('[x-ref=panel]').contains(document.activeElement)")

    # And the next open goes to the dock.
    await page.click(_row(first.id))
    await _wait_for_dock_record(page, first.id)
    assert await _record_trees(page) == {"dock": 1, "modal": 0, "total": 1}


async def test_crossing_xl_with_the_dock_open_closes_it_and_leaves_no_residue(page: Any, seed: Seeder) -> None:
    first, _second = await _two_files(seed)
    await _open_files(page, 1440)
    await page.click(_row(first.id))
    await _wait_for_dock_record(page, first.id)

    await page.set_viewport_size({"width": 1024, "height": 1000})
    await page.wait_for_function("() => document.getElementById('record-dock-body').children.length === 0")
    await _no_modal_residue(page)
    assert await _record_trees(page) == {"dock": 0, "modal": 0, "total": 0}
    assert await page.evaluate("!document.querySelector('[x-ref=panel]').checkVisibility()")
    assert await page.locator(_row(first.id)).get_attribute("aria-current") is None

    # The next open below xl is the modal, with its trap and inert background.
    await page.click(f'#files-table-view .md\\:block button[hx-get="/record/{first.id}"]')
    await page.wait_for_selector(f'#record-body [data-file-id="{first.id}"]')
    assert await _record_trees(page) == {"dock": 0, "modal": 1, "total": 1}


async def test_a_response_in_flight_across_xl_is_dropped_not_shown_in_the_wrong_host(page: Any, seed: Seeder) -> None:
    first, _second = await _two_files(seed)
    release = asyncio.Event()

    async def _held(route: Any) -> None:
        await release.wait()
        await route.continue_()

    await _open_files(page, 1440)
    await page.route("**/record/*", _held)
    await page.click(_row(first.id))
    await page.wait_for_function("() => document.getElementById('record-dock').checkVisibility()")
    await page.set_viewport_size({"width": 1024, "height": 1000})
    await page.wait_for_function("() => !document.getElementById('record-dock').checkVisibility()")
    release.set()
    await settled(page)
    await page.wait_for_timeout(300)
    assert await _record_trees(page) == {"dock": 0, "modal": 0, "total": 0}, "a stale response landed after the dock was closed by a resize"
    await _no_modal_residue(page)


async def test_a_stale_response_for_the_first_row_never_replaces_the_second(page: Any, seed: Seeder) -> None:
    first, second = await _two_files(seed)
    release_first = asyncio.Event()

    async def _hold_first(route: Any) -> None:
        if str(first.id) in route.request.url:
            await release_first.wait()
        await route.continue_()

    await _open_files(page, 1440)
    await page.route("**/record/*", _hold_first)
    await page.click(_row(first.id))
    await page.click(_row(second.id))
    await _wait_for_dock_record(page, second.id)
    release_first.set()
    await settled(page)
    await page.wait_for_timeout(300)

    assert await _record_trees(page) == {"dock": 1, "modal": 0, "total": 1}
    assert await page.locator("#record-dock-body [data-file-id]").get_attribute("data-file-id") == str(second.id)
    assert await page.locator(_row(second.id)).get_attribute("aria-current") == "true"
    assert await page.locator(_row(first.id)).get_attribute("aria-current") is None


async def test_the_command_palette_opens_exactly_one_record_in_the_dock_and_lands_focus_on_a_visible_element(page: Any, seed: Seeder) -> None:
    target = await seed.file(filename=_PALETTE_FILENAME)
    gets = _collect_record_gets(page)
    await _open_files(page, 1440)

    await page.click("#cmdk-trigger")
    await page.locator("#cmdk-dialog").wait_for(state="visible")
    await page.fill("#cmdk-dialog input[name='q']", "Norwood")
    await page.wait_for_selector("#cmdk-file-1", timeout=15_000)
    await page.click("#cmdk-file-1")
    await _wait_for_dock_record(page, target.id)
    await settled(page)

    assert len([u for u in gets if str(target.id) in u]) == 1, gets
    assert await page.evaluate("!document.getElementById('cmdk-dialog').checkVisibility()"), "the palette stayed open"
    assert await _record_trees(page) == {"dock": 1, "modal": 0, "total": 1}
    await page.wait_for_function(
        "() => document.activeElement && document.activeElement !== document.body && document.activeElement.getClientRects().length > 0"
    )
    assert await page.evaluate("document.getElementById('cmdk-dialog').contains(document.activeElement)") is False
    await _no_modal_residue(page)


async def test_a_click_before_htmx_wires_a_swapped_in_files_row_fetches_once_into_the_dock(page: Any, seed: Seeder) -> None:
    target = await seed.file(filename="<track-01>.mp3")
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await open_shell(page, "/s/summary")
    armed = page.evaluate(
        """() => new Promise((resolve) => {
            window.__recordRequests = [];
            const open = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function (method, url) {
                if (String(url).startsWith('/record/')) window.__recordRequests.push(url);
                return open.apply(this, arguments);
            };
            const observer = new MutationObserver(() => {
                const cell = document.querySelector('#files-table-view tr[hx-get^="/record/"] td');
                if (!cell) return;
                observer.disconnect();
                resolve({ htmxWired: !!cell.closest('tr')['htmx-internal-data'] });
                cell.click();
            });
            observer.observe(document.body, { childList: true, subtree: true });
        })"""
    )
    await page.evaluate("() => document.querySelector('a[href=\"/s/files\"]').click()")
    precondition = await armed
    assert precondition == {"htmxWired": False}, f"the click was not early enough to exercise the race: {precondition}"

    await _wait_for_dock_record(page, target.id)
    await page.wait_for_timeout(500)
    assert await page.evaluate("() => window.__recordRequests") == [f"/record/{target.id}"]
    assert await _record_trees(page) == {"dock": 1, "modal": 0, "total": 1}


async def test_selection_survives_a_files_table_swap_by_file_identity(page: Any, seed: Seeder) -> None:
    first, _second = await _two_files(seed)
    await _open_files(page, 1440)
    await page.click(_row(first.id))
    await _wait_for_dock_record(page, first.id)

    async with page.expect_response(lambda r: "/s/files" in r.url or "/pipeline/files" in r.url):
        await page.click("#files-table-view th button, #files-table-view th a")
    await settled(page)
    await page.wait_for_function(
        "id => document.querySelector(`#files-table-view tr[hx-get=\"/record/${id}\"]`)?.getAttribute('aria-current') === 'true'", arg=str(first.id)
    )
    assert await page.evaluate("document.querySelectorAll('#files-table-view tr[aria-current]').length") == 1
    assert await page.evaluate("document.getElementById('record-dock').checkVisibility()"), "a table swap closed the dock"
