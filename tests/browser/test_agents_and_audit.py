"""phaze-tzy6s.14 (CR-14-2): the two utility panes -- Agents refresh, and the Audit Log's filters.

Grouped like ``test_scans_and_duplicates.py`` groups its pair: both are ``UTILITY_PANES`` rather than
DAG stages, and each was reached exactly once by the all-workspace console sweep and never operated.

**Agents** is the app's only self-replacing surface: ``#agents-table-section`` re-fetches ITSELF
every 5s with ``hx-swap="outerHTML"``, so the element carrying the trigger is the element the
response destroys and rebuilds. That shape has two failure modes a server-side test cannot see -- the
swap landing INSIDE rather than replacing (nesting a section per tick), and the poll dying because
the rebuilt node lost its own trigger. It is also the suite's cleanest live-refresh surface: rows
written to the database while the page is open have to appear without any navigation.

**Audit** is the filter-tab surface. Its tabs live INSIDE the container they swap, which is the whole
point: a control that must reflect the swap's own state has to be re-rendered BY that swap, or it
keeps announcing the state it had before the click. phaze-xxp2 is that exact defect on the Propose
tabs -- rows updated, ``aria-pressed`` left on the old tab, screen-reader users told the wrong
filter. Nothing about that is visible in one rendered response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from phaze.enums.execution import ExecutionStatus
from phaze.models.tag_write_log import TagWriteLog
from tests.browser.helpers import click_swap, open_shell, settled


if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.browser


@pytest.mark.parametrize("viewport,theme", [("desktop", "light"), ("desktop", "dark"), ("phone", "light"), ("phone", "dark")])
async def test_completed_tag_history_is_reachable_after_review_and_audit_swaps(
    open_page: Any, seed: Any, tmp_path: Path, viewport: str, theme: str
) -> None:
    """Follow real guidance after HTMX navigation, then open the existing disclosure natively."""
    file = await seed.file(filename="history-example.mp3")
    seed.session.add(TagWriteLog(file_id=file.id, before_tags={}, after_tags={"artist": "Example"}, source="manual", status="completed"))
    seed.session.add(TagWriteLog(file_id=file.id, before_tags={"artist": "Example"}, after_tags={}, source="undo", status="completed"))
    await seed.session.commit()
    page = await open_page(viewport, theme=theme)
    for stage in ("audit", "rename"):
        await open_shell(page, "/s/summary")
        await settled(page)
        await page.evaluate("window.__historyGuidanceDocument = true")
        if viewport == "phone":
            await page.click("#rail-drawer-trigger")
        await click_swap(page, f'a[data-rail-stage="{stage}"]')
        panel = page.locator("[data-tagwrite-history-links]")
        await panel.wait_for(state="visible")
        assert await page.evaluate("window.__historyGuidanceDocument === true"), "rail navigation must preserve the live document"
        assert "Completed" in await panel.inner_text()
        assert "Tag undo" in await panel.inner_text()
        await panel.scroll_into_view_if_needed()
        await page.screenshot(path=str(tmp_path / f"{stage}-{viewport}-{theme}.png"))
        link = panel.locator(f'a[href="/files/{file.id}#history"]').first
        await link.click()
        await page.wait_for_url(f"**/files/{file.id}#history")
        await page.wait_for_function("() => document.querySelector('details#history')?.open === true")
        assert await page.locator("#history").is_visible()
        history = await page.locator("#history").inner_text()
        assert "tag write" in history and "completed" in history and "undo" in history
        assert await page.locator("#stage-workspace").count() == 1
        await page.screenshot(path=str(tmp_path / f"history-{stage}-{viewport}-{theme}.png"))
        await page.go_back()
        await page.locator("[data-tagwrite-history-links]").wait_for(state="visible")
        await page.locator(f'[data-tagwrite-history-links] a[href="/files/{file.id}#history"]').first.click()
        await page.wait_for_function("() => document.querySelector('details#history')?.open === true")


_AGENTS_SECTION = "#agents-table-section"


@pytest.mark.parametrize("surface,viewport,theme", [("agents", "desktop", "light"), ("activity", "phone", "dark")])
async def test_refresh_age_clamps_clock_skew_without_hiding_unknown_or_stale_times(
    open_page: Any, seed: Any, tmp_path: Path, surface: str, viewport: str, theme: str
) -> None:
    """Exercise the rendered Alpine getter and text, retaining the server stamp for diagnosis."""
    await seed.agent("agent-alpha", name="Alpha fileserver")
    page = await open_page(viewport, theme=theme)
    await open_shell(page, "/s/agents")
    await settled(page)
    if surface == "activity":
        await page.click("#agent-trigger-agent-alpha-details")
    selector = f'[data-refresh-age="{surface}"]'
    await page.locator(selector).wait_for(state="visible")
    stamp = await page.get_attribute(selector, "data-refreshed-at")
    assert stamp
    # Evaluate the real component; hold its ticker only during the synchronous edge-case checks.
    cases = await page.evaluate(
        """async selector => {
            const el = document.querySelector(selector);
            const scope = Alpine.$data(el);
            clearInterval(scope.ticker);
            const now = Date.parse('2026-01-01T00:00:00Z');
            const cases = [];
            for (const [stamp, expected] of [
                [new Date(now + 60000), '0s ago'], [new Date(now + 100), '0s ago'],
                [new Date(now), '0s ago'], [new Date(now - 900), '0s ago'],
                [new Date(now - 25400), '25s ago'], [new Date(now - 86400000), '86400s ago'],
                [new Date('invalid'), 'at an unknown time'], [new Date(''), 'at an unknown time'],
                [null, 'at an unknown time'], [undefined, 'at an unknown time']
            ]) {
                scope.now = new Date(now);
                scope.refreshedAt = stamp;
                await Alpine.nextTick();
                cases.push([el.querySelector('[x-text]').innerText.trim(), expected]);
            }
            scope.refreshedAt = new Date(el.dataset.refreshedAt);
            scope.now = new Date();
            scope.init();
            await Alpine.nextTick();
            return cases;
        }""",
        selector,
    )
    assert all(actual == expected for actual, expected in cases), cases
    assert await page.get_attribute(selector, "data-refreshed-at") == stamp
    # The actual five-second poll must replace the clock with a fresh server stamp.
    await page.wait_for_function(
        """args => document.querySelector(args[0])?.dataset.refreshedAt !== args[1]""",
        arg=[selector, stamp],
        timeout=20_000,
    )
    refreshed = await page.locator(f"{selector} [x-text]").inner_text()
    assert refreshed.strip() in [f"{age}s ago" for age in range(5)], refreshed
    await page.locator(selector).scroll_into_view_if_needed()
    await page.screenshot(path=str(tmp_path / f"refresh-{surface}-{viewport}-{theme}.png"))


async def test_the_agents_table_refreshes_itself_and_picks_up_a_new_agent(page: Any, seed: Any) -> None:
    """The live-refresh path: a row written while the page is open arrives on the pane's own tick.

    Every part of this is runtime. The 5s trigger is declared on the section that each response
    REPLACES, so the poll surviving at all depends on the swapped-in markup re-arming it -- a
    property that exists only across two ticks in a live document. The new agent is committed by this
    test process to the database the live server is reading, so its appearance proves a genuine
    server round trip rather than a client-side re-render.

    ``data-refreshed-at`` is the server's own stamp on each response, which makes "a tick landed"
    observable without guessing at content. Counting the sections afterwards is the other half: an
    ``outerHTML`` swap that lands as ``innerHTML`` nests a section per tick, and the page then looks
    right while growing a copy of itself every five seconds.
    """
    await seed.agent("agent-alpha", name="Alpha fileserver")

    await open_shell(page, "/s/agents")
    await settled(page)
    await page.evaluate("window.__documentAlive = true")

    first_stamp = await page.get_attribute(_AGENTS_SECTION, "data-refreshed-at")
    assert first_stamp, "the agents table carries no refresh stamp, so a tick cannot be told from a stale render"
    assert "1 agent" in await page.locator(_AGENTS_SECTION).inner_text(), "the seeded agent is not listed"

    await seed.agent("agent-beta", name="Beta fileserver")

    await page.wait_for_function(
        """args => {
            const section = document.querySelector(args[0]);
            return section !== null && section.getAttribute('data-refreshed-at') !== args[1];
        }""",
        arg=[_AGENTS_SECTION, first_stamp],
        timeout=20_000,
    )

    assert await page.evaluate("window.__documentAlive === true"), "the agents pane reloaded the document instead of polling"
    assert await page.locator(_AGENTS_SECTION).count() == 1, (
        "the 5s refresh nested a second #agents-table-section instead of replacing the first — the outerHTML swap landed as innerHTML"
    )
    body = await page.locator(_AGENTS_SECTION).inner_text()
    assert "Beta fileserver" in body, "an agent registered while the pane was open never appeared — the refresh is not reading the database"
    assert "Alpha fileserver" in body, "the refresh dropped the agent that was already listed"
    assert "2 agents" in body, "the agent count line did not follow the refreshed rows"


async def test_an_opened_agent_detail_stays_open_across_the_next_refresh(page: Any, seed: Any) -> None:
    """phaze-w92dg: a self-replacing table must not tear down the detail the operator just opened.

    The Details control expands a row IN PLACE by re-fetching the whole section with a new selection
    parameter, and the 5s poll then re-fetches that same section over and over. So the expanded row
    is rebuilt from scratch on every tick, and it survives only because the poll carries the live
    ``?agent=`` selection forward and the response keeps emitting the row's id. Lose either and the
    detail collapses under the operator a few seconds after they opened it -- the exact incident that
    bead records, and one with no server-side symptom at all: each individual response is correct.

    Waiting for TWO stamp changes rather than one is deliberate. One tick could be answered from the
    click's own response; the second proves the selection is being carried by the POLL, which is the
    channel that was broken.
    """
    await seed.agent("agent-alpha", name="Alpha fileserver")

    await open_shell(page, "/s/agents")
    await settled(page)

    trigger = "#agent-trigger-agent-alpha-details"
    assert await page.get_attribute(trigger, "aria-expanded") == "false", "the agent detail is expanded before anything was clicked"
    detail_row = await page.get_attribute(trigger, "aria-controls")
    assert detail_row, "the Details control names no row it expands"

    await page.click(trigger)
    await settled(page)
    await page.wait_for_selector(f"#{detail_row}")

    assert "agent=agent-alpha" in page.url, "the opened detail was not pushed, so it cannot be linked to or restored"
    assert await page.get_attribute(trigger, "aria-expanded") == "true", "the expanded row's trigger still reports itself collapsed"

    for _ in range(2):
        stamp = await page.get_attribute(_AGENTS_SECTION, "data-refreshed-at")
        await page.wait_for_function(
            """args => {
                const section = document.querySelector(args[0]);
                return section !== null && section.getAttribute('data-refreshed-at') !== args[1];
            }""",
            arg=[_AGENTS_SECTION, stamp],
            timeout=20_000,
        )

    assert await page.locator(f"#{detail_row}").count() == 1, (
        "the 5s refresh collapsed the open agent detail out from under the operator (phaze-w92dg)"
    )
    assert await page.get_attribute(trigger, "aria-expanded") == "true", "the refreshed trigger forgot that its row is expanded"
    assert await page.locator(_AGENTS_SECTION).count() == 1, "the refresh nested a second agents section while a detail was open"


def _tab(status: str) -> str:
    """A filter tab, addressed by the status it requests.

    An attribute selector rather than Playwright's ``:has-text()``: these selectors are also handed
    to ``wait_for_function``, which runs them through the page's own ``document.querySelector`` where
    Playwright's engine extensions do not exist and raise ``SyntaxError``. ``hx-get`` additionally
    carries the sort state, hence the prefix match.
    """
    return f'#audit-content nav[aria-label="Audit log status filter"] button[hx-get^="/audit/?status={status}"]'


async def _seed_audit_history(seed: Any) -> None:
    """Two terminal outcomes, so every filter tab has a distinguishable answer."""
    await seed.execution_log(status=ExecutionStatus.COMPLETED, filename="<set-01>.mp3")
    await seed.execution_log(
        status=ExecutionStatus.FAILED,
        filename="<set-02>.mp3",
        sha256_verified=False,
        error_message="Destination volume is read-only",
    )


async def test_switching_the_audit_filter_moves_aria_pressed_with_the_rows(page: Any, seed: Any) -> None:
    """The tabs have to be re-rendered by the swap they trigger, not just the rows under them.

    ``filter_tabs.html`` sits INSIDE ``#audit-content``, which is the container every tab click
    replaces. That is the fix shape, not an accident: if the tabs sat outside it, a click would
    update the rows and leave the previous tab underlined and still carrying ``aria-pressed="true"``
    -- announcing the wrong filter to exactly the users who cannot see which rows changed. Only a
    browser that has performed the swap can show which tab the DOM ends up claiming.

    The failed row's error message is asserted too: it is the audit log's ERROR state, and a filter
    that returns the right count while dropping the reason is a filter that has stopped being useful.
    """
    await _seed_audit_history(seed)

    await open_shell(page, "/s/audit")
    await settled(page)

    assert await page.get_attribute(_tab("all"), "aria-pressed") == "true", "the audit log does not open on All"
    assert await page.locator("#audit-table-container tbody tr:not(.hidden)").count() == 2, "both seeded operations are not listed under All"

    await click_swap(page, _tab("failed"))
    await page.wait_for_function("""sel => document.querySelector(sel)?.getAttribute('aria-pressed') === 'true'""", arg=_tab("failed"))

    assert await page.get_attribute(_tab("all"), "aria-pressed") == "false", (
        "the All tab still announces itself as the active filter after switching to Failed — two tabs now claim to be current"
    )
    assert "status=failed" in page.url, "the chosen filter was not pushed, so it is lost on reload"

    rows = await page.locator("#audit-table-container tbody tr:not(.hidden)").inner_text()
    assert "Destination volume is read-only" in rows, "the failed operation's reason is missing from the Failed filter"
    assert "<set-01>" not in rows, "the completed operation is still listed under the Failed filter"


async def test_an_audit_filter_that_matches_nothing_offers_the_way_back(page: Any, seed: Any) -> None:
    """The filtered-to-nothing state, and the control that undoes it, both delivered by the swap.

    "Nothing has ever run" and "this tab matches none of what has" are different facts with opposite
    operator responses, and phaze-37i1.2 split them for that reason. The second branch is only
    reachable with rows present AND a filter excluding all of them -- structurally unreachable before
    this suite could seed an audit history at all.

    Its "Show all operations" button is the part that needs a browser twice over: it is rendered
    inside the swapped container by the swap that emptied it, and clicking it has to swap the full
    set back in and restore the tabs' own state. A round trip is the only way to state that.
    """
    await _seed_audit_history(seed)

    await open_shell(page, "/s/audit")
    await settled(page)

    await click_swap(page, _tab("in_progress"))
    await page.wait_for_function("() => document.querySelector('#audit-content').innerText.includes('No entries match this filter')")

    empty = await page.locator("#audit-content").inner_text()
    assert "No renames have been executed yet" not in empty, (
        "a filtered-to-nothing tab claims nothing has ever been executed — the operator is told the archive is untouched"
    )
    assert "2 operations are recorded, but none are in this filter." in empty, f"the empty state does not say what is actually there: {empty[:400]!r}"

    await click_swap(page, '#audit-table-container button[hx-get^="/audit/?status=all"]')
    await page.wait_for_function("""sel => document.querySelector(sel)?.getAttribute('aria-pressed') === 'true'""", arg=_tab("all"))

    assert await page.locator("#audit-table-container tbody tr:not(.hidden)").count() == 2, (
        "the way back out of an empty filter did not restore the full set"
    )
    assert await page.get_attribute(_tab("in_progress"), "aria-pressed") == "false", (
        "the abandoned filter tab is still marked active after returning to All"
    )


async def test_back_restores_the_previous_audit_filter(page: Any, seed: Any) -> None:
    """A pushed filter has to be a real history entry, restored as a working pane.

    Each tab click pushes ``/audit/?status=...`` -- a URL whose route serves a FRAGMENT to htmx and a
    full page to the browser. History restore is where those two shapes get confused: a cached
    fragment restored into a full navigation yields a document that looks subtly right and passes
    every server-side assertion. Asserting that the restored view still carries its tabs, its rows
    and the correct ``aria-pressed`` is what separates a restored pane from a restored fragment.
    """
    await _seed_audit_history(seed)

    await open_shell(page, "/s/audit")
    await settled(page)

    await click_swap(page, _tab("failed"))
    await page.wait_for_function("""sel => document.querySelector(sel)?.getAttribute('aria-pressed') === 'true'""", arg=_tab("failed"))

    await click_swap(page, _tab("completed"))
    await page.wait_for_function("""sel => document.querySelector(sel)?.getAttribute('aria-pressed') === 'true'""", arg=_tab("completed"))

    await page.go_back()
    await page.wait_for_function("""sel => document.querySelector(sel)?.getAttribute('aria-pressed') === 'true'""", arg=_tab("failed"))

    assert await page.locator('aside[aria-label="Pipeline navigation"]').count() == 1, (
        "the restored audit filter dropped a bare fragment into the document"
    )
    assert await page.locator("#audit-table-container tbody tr:not(.hidden)").count() == 1, (
        "the restored Failed filter shows the wrong number of operations"
    )
    assert "Destination volume is read-only" in await page.locator("#audit-table-container").inner_text(), (
        "the restored filter lost the row it was filtering to"
    )
