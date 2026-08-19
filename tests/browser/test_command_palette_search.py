"""phaze-8p1uq: the ⌘K palette actually searching.

``test_shell_contract.py`` opens the palette and Escapes out of it, which covers the focus-trap and
focus-return contract and nothing else -- as the bead notes, it "never types a query". With an
empty corpus there was nothing to type: every query returns the same three static Navigate rows, so
a search box wired to the wrong endpoint, or a debounce that never fires, is indistinguishable from
one that works.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.helpers import open_shell


pytestmark = pytest.mark.browser


# cmdk_modal.html debounces on `input changed delay:200ms`. The waits below are on the RESULT, not
# on this number -- it is here to document why a result is not expected synchronously.
_DEBOUNCE_MS = 200

# Invented filenames in the product's own naming shape (CLAUDE.md explicitly sanctions these:
# "invented example filenames that illustrate a naming format"). Nothing here comes from a real
# archive; the festival names are made up.
#
# WORD-SHAPED, and that is a requirement rather than flavour. ``/search/`` is Postgres full-text
# search, so the query is lexemes, not a substring match -- and Postgres's parser glues a trailing
# extension onto the word before it (``Live Set.mp3`` lexes as ``live`` + ``set.mp3``, which is why
# searching "set" finds nothing). A filename like ``<track-42>.mp3`` is therefore ONE opaque lexeme
# with no searchable word in it at all, and a palette test built on those names asserts the
# tokenizer's behaviour instead of the palette's. The distinguishing word is placed mid-name so it
# survives lexing intact.
NORWOOD = "Example Artist - Norwood Festival - Opening Set (2024).mp3"
HALCYON = "Another Artist - Halcyon Festival - Closing Set (2024).mp3"


async def _open_palette(page: Any) -> Any:
    await page.click("#cmdk-trigger")
    dialog = page.locator("#cmdk-dialog")
    await dialog.wait_for(state="visible")
    # The input carries `hx-trigger="load, ..."`, so the initial (empty-query) fetch lands first.
    await page.wait_for_function("() => document.querySelectorAll('#cmdk-results [role=\"option\"]').length > 0")
    return dialog


async def test_typing_a_query_returns_matching_files(page: Any, seed: Any) -> None:
    """A typed query reaches /search/ and the matching file comes back as a selectable row.

    Everything between the keystroke and the row is browser-only: the debounced ``hx-get``, the
    swap into ``#cmdk-results``, and ``onResults()`` rebuilding the roving-index list from the
    freshly-swapped DOM. The last of those is the subtle one -- ``@htmx:after-swap="onResults()"``
    is what keeps the keyboard nav pointed at rows that exist, and if it stops firing the palette
    still LOOKS right (rows render, hover works) while ↑/↓ and Enter operate on a stale list from
    the previous query, i.e. they open the wrong file.

    Two distinct files are seeded so the assertion is that the query FILTERED, not merely that
    something rendered. A palette that ignores ``q`` entirely passes a one-file version of this test.
    """
    await seed.file(filename=NORWOOD)
    await seed.file(filename=HALCYON)

    await open_shell(page)
    await _open_palette(page)

    await page.fill("#cmdk-dialog input[name='q']", "Norwood")
    await page.wait_for_function(
        """expected => {
            const results = document.getElementById('cmdk-results');
            return results && results.textContent.includes(expected);
        }""",
        arg=NORWOOD,
        timeout=15_000,
    )

    results = await page.locator("#cmdk-results").inner_text()
    assert HALCYON not in results, f"the query matched one set but the other came back too — the search term was ignored:\n{results}"

    # The roving list was rebuilt against the new rows (cmdk_modal.html's onResults()).
    active = await page.evaluate("document.querySelector('#cmdk-dialog input[name=\"q\"]').getAttribute('aria-activedescendant')")
    assert active, "aria-activedescendant is unset after a result swap — the roving index was not rebuilt"
    assert await page.locator(f"#{active}").count() == 1, f"aria-activedescendant points at {active!r}, which is not in the current results"


async def test_arrow_keys_move_the_active_row_within_the_typed_results(page: Any, seed: Any) -> None:
    """↓ advances the roving index over the rows the current query produced.

    ARIA listbox semantics implemented in JavaScript: the rows are static markup with
    ``role="option"`` and the ACTIVE one is tracked by ``aria-activedescendant`` on the input rather
    than by focus. That indirection is invisible to markup assertions and is the whole reason a
    screen-reader user can hear the palette's selection change at all.
    """
    await seed.file(filename=NORWOOD)
    await seed.file(filename=HALCYON)

    await open_shell(page)
    await _open_palette(page)

    await page.fill("#cmdk-dialog input[name='q']", "Festival")
    await page.wait_for_function(
        """() => document.querySelectorAll('#cmdk-results [role="option"]').length > 1""",
        timeout=15_000,
    )

    first = await page.evaluate("document.querySelector('#cmdk-dialog input[name=\"q\"]').getAttribute('aria-activedescendant')")
    await page.keyboard.press("ArrowDown")
    await page.wait_for_function(
        """previous => document.querySelector('#cmdk-dialog input[name="q"]').getAttribute('aria-activedescendant') !== previous""",
        arg=first,
    )

    second = await page.evaluate("document.querySelector('#cmdk-dialog input[name=\"q\"]').getAttribute('aria-activedescendant')")
    assert second != first, "ArrowDown did not move the active option"
    assert (
        await page.locator(f"#{second} [aria-selected], #{second}[aria-selected='true']").count() >= 1
        or (await page.locator(f"#{second}").get_attribute("aria-selected")) == "true"
    ), "the newly active row does not announce aria-selected=true"


async def test_a_query_that_matches_nothing_still_offers_the_navigation_rows(page: Any, seed: Any) -> None:
    """An empty result set degrades to the static Navigate group rather than to a dead panel.

    ``palette_results.html`` renders those three rows unconditionally, above every conditional
    group. This asserts the operator is never left in a modal with nothing actionable in it -- and
    it is the case a "hide the group when it is empty" change would break, since the Navigate rows
    are not a group in that sense even though they look like one.
    """
    await seed.file(filename=NORWOOD)

    await open_shell(page)
    await _open_palette(page)

    await page.fill("#cmdk-dialog input[name='q']", "Nonexistent")
    await page.wait_for_timeout(_DEBOUNCE_MS * 6)
    await page.wait_for_function(
        """expected => !document.getElementById('cmdk-results').textContent.includes(expected)""",
        arg=NORWOOD,
        timeout=15_000,
    )

    options = page.locator("#cmdk-results [role='option']")
    assert await options.count() >= 3, "a non-matching query left the palette with no actionable rows at all"
    assert await page.locator("#cmdk-nav-summary").count() == 1, "the static Navigate rows disappeared on an empty result set"


async def test_the_palette_survives_a_stage_swap(page: Any, seed: Any) -> None:
    """The palette is chrome: a rail swap must not destroy it.

    ``shell.html`` mounts the cmdk modal outside ``#stage-workspace`` precisely so a stage swap
    never re-renders or destroys it. Destroyed, ⌘K stops working after the first navigation --
    which is unreachable from a server test (there is only ever one render) and easy to reintroduce
    by moving the include inside the workspace for tidiness.
    """
    await seed.file(filename=NORWOOD)
    await open_shell(page)

    await page.click('a[data-rail-stage="files"]')
    await page.wait_for_function(
        """() => document.querySelector('#stage-workspace [data-document-title]')?.dataset.documentTitle === 'Files'""",
    )

    dialog = await _open_palette(page)
    assert await dialog.is_visible(), "the command palette stopped opening after a stage swap"

    await page.fill("#cmdk-dialog input[name='q']", "Norwood")
    await page.wait_for_function(
        """expected => document.getElementById('cmdk-results').textContent.includes(expected)""",
        arg=NORWOOD,
        timeout=15_000,
    )


async def test_a_command_outcome_is_not_destroyed_by_the_next_keystroke(page: Any, seed: Any) -> None:
    """phaze-jng72: the ⌘K outcome live region survives a search swap AS THE SAME NODE.

    The region used to be rendered by ``palette_results.html``, i.e. inside ``#cmdk-results`` — the
    debounced search's ``hx-swap="innerHTML"`` target. Two things were wrong with that, and only
    the first is visible to a markup assertion: the listbox owned a ``role="status"`` child (invalid
    ARIA, axe critical), and every command outcome swapped into it was DELETED by the next
    keystroke. A live region announces a mutation of a node the screen reader is already observing,
    so a region that is replaced wholesale announces nothing — the operator got no feedback at all
    from a ⌘K command.

    Existence is therefore not the property to assert: a fresh, empty ``#cmdk-command-result``
    re-rendered by the swap satisfies "it exists" while being exactly the bug. This holds a direct
    JS reference to the node and an expando written onto it, neither of which can survive
    re-rendering, and checks both after a swap the test proves actually happened.
    """
    await seed.file(filename=NORWOOD)
    await seed.file(filename=HALCYON)

    await open_shell(page)
    await _open_palette(page)

    await page.fill("#cmdk-dialog input[name='q']", "Norwood")
    await page.wait_for_function(
        """expected => document.getElementById('cmdk-results').textContent.includes(expected)""",
        arg=NORWOOD,
        timeout=15_000,
    )

    outside = await page.evaluate(
        """() => {
            const region = document.getElementById('cmdk-command-result');
            const results = document.getElementById('cmdk-results');
            return !!region && !!results && !results.contains(region);
        }"""
    )
    assert outside, "#cmdk-command-result is inside #cmdk-results — the swap target destroys it on every keystroke"

    # Stand in for a command's hx-swap into the region (the real command posts to /pipeline/* behind
    # an hx-confirm; what is under test is the region's survival, not the pipeline).
    await page.evaluate(
        """() => {
            const region = document.getElementById('cmdk-command-result');
            region.innerHTML = 'Queued for analysis.';
            region.dataset.jng72 = 'stamped';
            window.__cmdkOutcomeNode = region;
        }"""
    )

    # A keystroke that demonstrably re-swaps #cmdk-results.
    await page.fill("#cmdk-dialog input[name='q']", "Halcyon")
    await page.wait_for_function(
        """expected => document.getElementById('cmdk-results').textContent.includes(expected)""",
        arg=HALCYON,
        timeout=15_000,
    )
    await page.wait_for_function(
        """gone => !document.getElementById('cmdk-results').textContent.includes(gone)""",
        arg=NORWOOD,
        timeout=15_000,
    )

    after = await page.evaluate(
        """() => {
            const region = document.getElementById('cmdk-command-result');
            return {
                present: !!region,
                same_node: region === window.__cmdkOutcomeNode,
                stamped: region ? region.dataset.jng72 === 'stamped' : false,
                text: region ? region.textContent.trim() : null,
            };
        }"""
    )
    assert after["present"], "the ⌘K outcome live region vanished after a search swap"
    assert after["same_node"], (
        "the ⌘K outcome live region was REPLACED by the search swap — an announcement made into it is destroyed before it is read"
    )
    assert after["stamped"], "the ⌘K outcome live region lost its node identity across the swap"
    assert after["text"] == "Queued for analysis.", f"the announced command outcome was wiped by the next keystroke: {after['text']!r}"
