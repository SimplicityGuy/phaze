"""phaze-9l0qy.6: the molecule's done-gate -- one file is the SAME record from every entry point.

The four beads before this one each proved their own half in isolation: phaze-9l0qy.1 built the
shared record context and the canonical page, .2 unified file activation, .3 made the tracklist
section a complete review surface, .5 made the timeline inspectable. Every one of those is asserted
by focused route/template tests that render ONE fragment and read it back.

That is exactly the coverage shape this bead exists to close. Files, Analyze, Tracklists and the
command palette are four independently-rendered fragments, and the drawer and the full page are two
independently-routed presentations of a third. A server-side assertion can prove each one correct on
its own and still say nothing about the claim the molecule actually makes to an operator -- that the
file they clicked in one workspace is the same object, with the same facts, as the file they clicked
in another. Cross-surface drift lives precisely in the gap between those per-fragment tests, so the
gate has to drive the real thing.

``_record_fingerprint`` is the load-bearing helper: it reads the shared ``[data-record-content]``
subtree that .1 extracted, so any divergence between two entry points -- a dropped section, a
different file, a stage matrix computed from a different query -- shows up as a text diff naming
both sides, instead of as four separately-green tests.
"""

from __future__ import annotations

from datetime import date
import re
from typing import TYPE_CHECKING, Any
import uuid

import pytest

from tests.browser.helpers import open_shell, settled, settled_focus, swap_settles


if TYPE_CHECKING:
    from tests.browser.seed import Seeder


pytestmark = pytest.mark.browser


# INVENTED filenames in the product's naming shape (CLAUDE.md sanctions these explicitly). They are
# word-shaped for the reason test_files_record.py documents at length: ``/search/`` is Postgres
# full-text search, so a ``<set-01>.mp3`` placeholder is one opaque lexeme with no searchable word in
# it and no palette query would ever surface it.
_MATCHED_FILENAME = "Example Artist - Norwood Festival - Closing Set (2024).mp3"
_UNMATCHED_FILENAME = "Second Artist - Kestrel Fields - Sunrise Set (2024).mp3"

# The distinguishing word in each, used as the palette query.
_MATCHED_QUERY = "Norwood"

# Both seeded files carry a duration well past ``SET_DURATION_MIN_SECONDS`` (1200s), so the candidate
# classifier reads them as LIVE_SET. That is what makes the unmatched one ``eligible`` and -- with no
# cache row to suppress it -- ``actionable``, which is the only state in which the record offers the
# exact-file "Look up now" control this bead has to gate.
_SET_DURATION_SEC = 7_200.0

# seed.analysis_windows fills fine-window bpm with ``126.0 + (index % 5)`` -> 126..130. The timeline
# rounds bounds OUTWARD to containing multiples of ten, so the gutter must read 130 over 120 -- not
# the raw 130/126 extrema.
_EXPECTED_BPM_HI = "130"
_EXPECTED_BPM_LO = "120"

_DESKTOP_TABLE = "#files-table-view .md\\:block"


def _files_row(file_id: Any) -> str:
    return f'{_DESKTOP_TABLE} tr[hx-get="/record/{file_id}"]'


async def _wait_for_record(page: Any, file_id: Any) -> None:
    """Wait until the drawer holds THIS file's record and has actually been revealed.

    Both halves matter. The id-scoped selector is what makes this a same-file assertion rather than
    an any-record one -- ``show()`` reveals the host synchronously and always wins the race against
    the async GET, so a bare visibility wait can be satisfied by the PREVIOUS file's body (the
    phaze-7yet stale-panel shape). Visibility is then the last of the three post-conditions to land.
    """
    await page.wait_for_selector(f'#record-body [data-file-id="{file_id}"]')
    await page.wait_for_function("() => document.getElementById('record-body').checkVisibility()")


async def _close_record(page: Any) -> None:
    await page.keyboard.press("Escape")
    await page.wait_for_function("() => !document.getElementById('record-body').checkVisibility()")


async def _record_fingerprint(page: Any, scope: str) -> str:
    """Normalized text of the shared record content under ``scope`` (``#record-body`` or ``main``).

    Every value in this subtree is rendered from stored rows -- the history timestamps included, which
    are the row's own ``when`` formatted ``%Y-%m-%d %H:%M`` rather than anything derived from now() --
    so two opens of the same file produce byte-identical text. The one genuinely volatile element is
    the timeline's ``[data-timeline-readout]``, which is pointer state; it is empty until something
    moves over the inspector, and no caller here fingerprints after interacting with it.

    That stability is what lets this be a whole-subtree comparison instead of a handful of
    cherry-picked facts: a section that silently stops rendering from one entry point fails here,
    where a targeted assertion list would have to have anticipated it.
    """
    text = await page.locator(f"{scope} [data-record-content]").inner_text()
    return re.sub(r"\s+", " ", text).strip()


async def _seed_matched_file(seed: Seeder) -> Any:
    """One fully-populated file: metadata, an exhaustive timeline, and a complete tracklist."""
    from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion

    file = await seed.file(filename=_MATCHED_FILENAME)
    await seed.metadata(file, duration=_SET_DURATION_SEC)
    await seed.analysis_windows(file, fine_count=24, coarse_count=6)

    tracklist = Tracklist(
        external_id="done-gate-matched-set",
        source_url="https://example.test/tracklist",
        file_id=file.id,
        match_confidence=94,
        artist="Example Artist",
        event="Norwood Festival",
        date=date(2026, 8, 1),
    )
    seed.session.add(tracklist)
    await seed.session.flush()
    version = TracklistVersion(tracklist_id=tracklist.id, version_number=1)
    seed.session.add(version)
    await seed.session.flush()
    seed.session.add_all(
        [
            TracklistTrack(version_id=version.id, position=1, timestamp="00:00", artist="First Artist", title="Opening Track", label="First Label"),
            TracklistTrack(version_id=version.id, position=2, timestamp="41:07", artist="Second Artist", title="Closing Track", label="Second Label"),
        ]
    )
    tracklist.latest_version_id = version.id
    await seed.session.commit()
    return file


async def test_one_file_is_the_same_record_from_files_analyze_tracklists_and_search(page: Any, seed: Seeder) -> None:
    """The molecule's central claim, driven through all four openers against one seeded file.

    Each entry point is a separately-rendered fragment reaching the same record host by a different
    route -- a Files row, an Analyze row after its own lazy load, a matched Tracklists row carrying
    the file id .2 taught its projection to keep, and a command-palette row that dismisses the layer
    the operator was typing in. Fingerprinting the shared content subtree after each open is what
    turns "all four opened something" into "all four opened the SAME something".

    The Tracklists opener additionally has to land at ``#tracklist`` rather than at the top, which is
    the one behavioural difference between the four and therefore asserted separately.
    """
    target = await _seed_matched_file(seed)
    fingerprints: dict[str, str] = {}

    # --- Files: activate the row itself, which is the affordance .2 added. -------------------
    await open_shell(page, "/s/files")
    await settled(page)
    async with swap_settles(page):
        await page.click(f"{_files_row(target.id)} td:last-child")
    await _wait_for_record(page, target.id)
    fingerprints["files"] = await _record_fingerprint(page, "#record-body")
    assert f"/files/{target.id}" in (await page.locator(f'#record-body a[href="/files/{target.id}"]').get_attribute("href") or ""), (
        "the drawer opened from Files offers no canonical full-page URL"
    )
    await _close_record(page)

    # --- Analyze: same contract on a workspace that renders its own table. -------------------
    await open_shell(page, "/s/analyze")
    await page.wait_for_selector(f'#analyze-files-view tr[hx-get="/record/{target.id}"]')
    async with swap_settles(page):
        await page.click(f'#analyze-files-view tr[hx-get="/record/{target.id}"] td:first-child')
    await _wait_for_record(page, target.id)
    fingerprints["analyze"] = await _record_fingerprint(page, "#record-body")
    await _close_record(page)

    # --- Tracklists: keyboard activation, and it must arrive at the Tracklist section. -------
    await open_shell(page, "/s/tracklist")
    await page.wait_for_selector("#tracklist-set-table")
    await settled(page)
    matched_row = f'#tracklist-sets-view tr[hx-get="/record/{target.id}"]'
    await page.locator(matched_row).focus()
    async with swap_settles(page):
        await page.keyboard.press("Enter")
    await _wait_for_record(page, target.id)
    fingerprints["tracklists"] = await _record_fingerprint(page, "#record-body")
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
    assert positioned, "the matched Tracklists row opened the record but not at its Tracklist section"
    await _close_record(page)

    # --- Search: the ⌘K palette row, the fourth opener of the same host. ---------------------
    await open_shell(page, "/s/files")
    await settled(page)
    await page.click("#cmdk-trigger")
    await page.locator("#cmdk-dialog").wait_for(state="visible")
    await page.fill("#cmdk-dialog input[name='q']", _MATCHED_QUERY)
    await page.wait_for_selector("#cmdk-file-1", timeout=15_000)
    await page.click("#cmdk-file-1")
    await _wait_for_record(page, target.id)
    fingerprints["search"] = await _record_fingerprint(page, "#record-body")

    # --- The canonical page is the fifth presentation and must agree with all of them. -------
    await page.locator(f'#record-body a[href="/files/{target.id}"]').click()
    await page.wait_for_url(f"**/files/{target.id}")
    fingerprints["full page"] = await _record_fingerprint(page, "main")

    baseline_name, baseline = next(iter(fingerprints.items()))
    assert "Opening Track" in baseline and "Closing Track" in baseline, "the seeded tracklist never reached the record at all"
    for name, value in fingerprints.items():
        assert value == baseline, (
            f"the record opened from {name} differs from the one opened from {baseline_name} — the file is not one object across the UI"
        )


async def test_an_unmatched_file_states_its_own_lookup_status_without_leaking_the_matched_one(page: Any, seed: Seeder) -> None:
    """A file with no tracklist must say so honestly, and offer the lookup for ITSELF.

    Two failure modes share one symptom here and only a real browser separates them. The drawer is a
    single persistent host reused by every open, so a second file's record can inherit the first
    file's tracklist through a stale panel (phaze-7yet) -- and the exact-file lookup control can be
    wired to the wrong id while still rendering perfectly, which is the misattributed-lookup shape
    .3's design calls out by name. Asserting the button's own ``hx-post`` target is what distinguishes
    "a Look up now button exists" from "a Look up now button that would look up THIS file".
    """
    matched = await _seed_matched_file(seed)
    unmatched = await seed.file(filename=_UNMATCHED_FILENAME)
    await seed.metadata(unmatched, duration=_SET_DURATION_SEC)

    await open_shell(page, "/s/files")
    await settled(page)

    async with swap_settles(page):
        await page.click(f"{_files_row(matched.id)} td:last-child")
    await _wait_for_record(page, matched.id)
    assert "Opening Track" in await page.locator("#record-body #tracklist").inner_text()
    await _close_record(page)

    async with swap_settles(page):
        await page.click(f"{_files_row(unmatched.id)} td:last-child")
    await _wait_for_record(page, unmatched.id)

    drawer_tracklist = await page.locator("#record-body #tracklist").inner_text()
    assert "No matched 1001Tracklists tracklist yet." in drawer_tracklist, (
        f"the unmatched file does not state its empty tracklist status: {drawer_tracklist!r}"
    )
    assert "Not yet looked up." in drawer_tracklist, "an unattempted file must not be described as anything other than never looked up"
    assert "Opening Track" not in drawer_tracklist, "the previous file's tracklist is still on screen under the second record (phaze-7yet)"
    assert "Closing Track" not in drawer_tracklist

    lookup = page.locator('#record-body button[aria-label="Look up this file now on 1001Tracklists"]')
    assert await lookup.count() == 1, "the eligible unmatched file offers no exact-file lookup action"
    assert await lookup.get_attribute("hx-post") == f"/pipeline/tracklists/{unmatched.id}/prioritize", (
        "the lookup control would queue a different file than the record it is rendered in"
    )

    # The full page is the second presentation of the same state and must not diverge from it.
    await page.locator(f'#record-body a[href="/files/{unmatched.id}"]').click()
    await page.wait_for_url(f"**/files/{unmatched.id}")
    page_tracklist = await page.locator("main #tracklist").inner_text()
    assert page_tracklist == drawer_tracklist, "the canonical page states a different tracklist status than the drawer"
    assert await page.locator(f'main button[hx-post="/pipeline/tracklists/{unmatched.id}/prioritize"]').count() == 1


async def test_the_canonical_page_is_addressable_on_reload_and_without_htmx(page: Any, seed: Seeder) -> None:
    """``/files/{id}`` is an ordinary page: it survives a reload and needs no client-side navigation.

    The anchors are the reason this matters beyond bookmarking. ``#analysis`` / ``#tracklist`` /
    ``#history`` are the contract later cross-feature deep links are built on -- .2 already consumes
    ``#tracklist`` from a Tracklists row -- so they have to resolve from a cold load rather than only
    after the drawer's JavaScript has run. Blocking the htmx bundle is how that gets proven rather
    than assumed: with the script aborted, nothing on the page can be a client-side swap.
    """
    target = await _seed_matched_file(seed)

    await page.route("**/htmx*.js", lambda route: route.abort())
    await page.goto(f"/files/{target.id}", wait_until="domcontentloaded")
    assert await page.evaluate("() => window.htmx === undefined"), "htmx loaded anyway — this run does not prove the page works without it"

    assert await page.locator(f'[data-file-id="{target.id}"]').is_visible()
    assert target.current_path in await page.locator("main").inner_text()
    for anchor in ("analysis", "tracklist", "history"):
        assert await page.locator(f"#{anchor}").count() == 1, f"#{anchor} is missing from the canonical page"
        await page.locator(f'a[href="#{anchor}"]').first.click()
        await page.wait_for_function("id => document.getElementById(id).getBoundingClientRect().top < window.innerHeight", arg=anchor)

    await page.reload(wait_until="domcontentloaded")
    assert await page.locator(f'[data-file-id="{target.id}"]').is_visible(), "the canonical page did not survive a reload"

    # A file id that does not exist stays friendly and says nothing about the archive.
    missing = uuid.uuid4()
    await page.goto(f"/files/{missing}", wait_until="domcontentloaded")
    body = await page.locator("body").inner_text()
    # The product's own copy, which is deliberately about the FILE rather than about a status code --
    # asserting the rendered wording is what keeps the friendly 404 from silently becoming a bare one.
    # Read through ``text_content``: the heading carries an intentional uppercase text-transform, so
    # ``inner_text`` would return the CSS-rendered form rather than the copy the template owns.
    heading = (await page.locator("main h1").text_content() or "").strip()
    assert heading == "That file no longer exists.", f"a missing file did not produce the friendly not-found page: {body[:200]!r}"
    assert "de-duplicated" in body, "the not-found page dropped the explanation of why a record can vanish"
    assert await page.title() == "File not found | Phaze", "a bookmarked missing record does not title itself as missing"
    assert target.current_path not in body, "the not-found page leaked another file's path"
    assert "Traceback" not in body and "sqlalchemy" not in body.lower(), "the not-found page leaked internals"
    await page.unroute("**/htmx*.js")


async def test_the_timeline_reads_the_same_in_the_drawer_and_on_the_page_and_returns_focus(page: Any, seed: Seeder) -> None:
    """Timeline inspection is part of the record, so it has to agree across both presentations.

    Keyboard positioning is used rather than a pointer move because it is the only way to ask both
    presentations for the SAME time: ``Home`` then one ``ArrowRight`` is the first natural midpoint in
    each, whereas a mouse coordinate depends on where the surface happens to be laid out. A readout
    that disagrees between drawer and page at an identical time is the drift this gate exists to
    catch, and it is invisible to any server-side assertion -- the value is computed in JavaScript
    from the inspection JSON.
    """
    target = await _seed_matched_file(seed)

    async def _inspect_at_first_midpoint(scope: str) -> tuple[str, str]:
        timeline = page.locator(f"{scope} [data-analysis-timeline]")
        await timeline.wait_for(state="visible")
        await page.wait_for_function(
            "selector => document.querySelector(selector).dataset.timelineReady === 'true'",
            arg=f"{scope} [data-analysis-timeline]",
        )
        inspector = timeline.locator("[data-timeline-inspector]")
        await inspector.focus()
        await page.keyboard.press("Home")
        await page.keyboard.press("ArrowRight")
        return str(await inspector.get_attribute("aria-valuenow")), str(await inspector.get_attribute("aria-valuetext"))

    async def _bpm_bounds(scope: str) -> list[str]:
        return [(text or "").strip() for text in await page.locator(f"{scope} .analysis-timeline-bpm-scale span").all_text_contents()]

    await open_shell(page, "/s/files")
    await settled(page)
    async with swap_settles(page):
        await page.click(f"{_files_row(target.id)} td:last-child")
    await _wait_for_record(page, target.id)

    drawer_reading = await _inspect_at_first_midpoint("#record-body")
    drawer_bounds = await _bpm_bounds("#record-body")
    assert drawer_bounds == [_EXPECTED_BPM_HI, _EXPECTED_BPM_LO], (
        f"the BPM gutter shows {drawer_bounds} — bounds must round OUTWARD to containing multiples of ten, not print the raw extrema"
    )

    viewport = page.locator("#record-body [data-timeline-viewport]")
    hint = page.locator("#record-body [data-timeline-scroll-hint]")
    assert await viewport.evaluate("el => el.scrollWidth > el.clientWidth"), "a 24-window timeline did not overflow its scrollport"
    assert await hint.is_visible(), "the timeline overflows but advertises no scroll affordance"
    assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), (
        "the timeline's overflow escaped its own scrollport and now scrolls the document"
    )

    # Esc must return the keyboard to the row that opened the record, not strand it in the timeline.
    await _close_record(page)
    assert await settled_focus(page, "aria-label") == f"Open details for {target.original_filename}", (
        "Esc from inside the timeline did not return focus to the row that opened the record"
    )

    await page.goto(f"/files/{target.id}", wait_until="domcontentloaded")
    page_reading = await _inspect_at_first_midpoint("main")
    assert await _bpm_bounds("main") == drawer_bounds, "the canonical page draws the timeline against different BPM bounds than the drawer"
    assert page_reading == drawer_reading, (
        f"the timeline reports {page_reading} on the page and {drawer_reading} in the drawer at the same time — the two presentations have drifted"
    )
