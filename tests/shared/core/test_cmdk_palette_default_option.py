"""phaze-45uu — the ⌘K palette's default-highlighted row must not be an unconfirmed write.

``cmdk_modal.html``'s ``onResults()`` sets ``activeIndex = 0`` after every swap, and the
input's ``@keydown.enter.prevent="activate()"`` clicks ``items[0]``. So whatever the FIRST
selectable ``role="option"`` row is, a bare ⌘K + Enter fires it. When the query is empty (or
returns nothing) the Commands group is the only rendered group, so ``items[0]`` is a command.

The invariant guarded here: **in the commands-only fragment, no state-changing HTMX row may
be reachable by an accidental Enter without a confirmation** — and specifically the row the
palette auto-highlights must not be one. Before the fix, ``#cmdk-cmd-scan``
(``hx-post="/pipeline/scan-live-sets"``, ``hx-swap="none"``) was row 0 with no ``hx-confirm``,
so ⌘K + Enter silently enqueued a live-set scan with zero on-screen feedback.

This renders the real template (no DB, no client fixture — it stays in the fast lane) and
asserts over the parsed DOM by ROLE and HTMX VERB, never by element id or position, so the
guard survives the Commands rows being reordered or renamed. It follows the repo's existing
template-guard idiom (``test_a11y_guards.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader
import pytest


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_PALETTE_RESULTS = "search/partials/palette_results.html"

# htmx verbs that mutate server state. hx-get is a read and is safe as a default row.
_STATE_CHANGING_ATTRS = ("hx-post", "hx-put", "hx-patch", "hx-delete")


def _render_commands_only(query: str | None) -> str:
    """Render palette_results.html the way search_page() does for a no-result query.

    ``search_page`` passes ``results=[]`` whenever ``q`` is falsy and ``artists=[]`` unless
    ``len(q) >= 2``, so an empty or unmatched query yields exactly this context — a fragment
    containing only the always-rendered Commands group.
    """
    # autoescape=True mirrors Jinja2Templates' own configuration in phaze.routers.search.
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    context: dict[str, Any] = {
        "query": query,
        "file_results": [],
        "tracklist_results": [],
        "discogs_results": [],
        "artists": [],
        "artist": None,
    }
    return env.get_template(_PALETTE_RESULTS).render(**context)


def _options(html: str) -> list[Tag]:
    """Every selectable row, in DOM order. Group headers are role="presentation" and skipped."""
    return [el for el in BeautifulSoup(html, "html.parser").find_all(attrs={"role": "option"}) if isinstance(el, Tag)]


def _state_changing_verb(option: Tag) -> str | None:
    """The htmx write verb this row issues, if any."""
    return next((attr for attr in _STATE_CHANGING_ATTRS if option.has_attr(attr)), None)


@pytest.mark.parametrize("query", [None, "", "zzzz-no-such-file-zzzz"])
def test_commands_only_palette_has_selectable_rows(query: str | None) -> None:
    """Sanity: the Commands group is always rendered, so items[0] always exists."""
    assert _options(_render_commands_only(query)), "commands-only palette rendered no role=option rows"


@pytest.mark.parametrize("query", [None, "", "zzzz-no-such-file-zzzz"])
def test_default_highlighted_row_is_not_an_unconfirmed_write(query: str | None) -> None:
    """items[0] — the row a bare ⌘K + Enter activates — must not be an unconfirmed POST."""
    first = _options(_render_commands_only(query))[0]
    verb = _state_changing_verb(first)
    assert verb is None or first.has_attr("hx-confirm"), (
        f"the default-highlighted palette row (id={first.get('id')!r}) issues {verb}="
        f"{first.get(verb)!r} with no hx-confirm; bare ⌘K + Enter would fire it silently"
    )


@pytest.mark.parametrize("query", [None, "", "zzzz-no-such-file-zzzz"])
def test_every_state_changing_command_row_is_confirmed(query: str | None) -> None:
    """Defense in depth: ↑/↓ + Enter must not reach any unconfirmed write either."""
    unconfirmed = [
        (option.get("id"), verb)
        for option in _options(_render_commands_only(query))
        if (verb := _state_changing_verb(option)) and not option.has_attr("hx-confirm")
    ]
    assert not unconfirmed, f"palette rows issue state-changing htmx requests with no hx-confirm: {unconfirmed}"
