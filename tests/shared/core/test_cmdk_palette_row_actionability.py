"""phaze-vxg6 — every ⌘K palette row exposed as ``role="option"`` must actually do something.

``cmdk_modal.html``'s ``onResults()`` builds its roving-index list from
``querySelectorAll('[role="option"]')`` and ``activate()`` runs ``el.click()`` on the active
row. ``syncActive()`` additionally points ``aria-activedescendant`` at it and paints the
active-row highlight. So ``role="option"`` is a *promise*: this row is selectable and Enter
acts on it.

The Discogs group broke that promise. Each hit rendered as a bare
``<div role="option" aria-selected="false" tabindex="-1" class="{{ row_class }}">`` with no
``href``, no ``hx-*`` and no ``@click`` — while ``row_class`` carries ``cursor-pointer`` and a
hover highlight, so it *looked* selectable. Arrowing onto a Discogs hit and pressing Enter did
nothing at all, and a screen reader announced an active option whose activation silently failed.

The invariant guarded here: **a row is either actionable or it is not a ``role="option"``.**
Informational rows must carry neither the role nor the selectable affordances, so the roving
nav skips them.

Like ``test_cmdk_palette_default_option.py``, this renders the real template with no DB and no
client fixture (fast lane) and asserts over the parsed DOM by ROLE and ATTRIBUTE — never by
element id or position — so the guard survives rows being reordered, renamed, or regrouped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader
import pytest


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_PALETTE_RESULTS = "search/partials/palette_results.html"

# What makes a row do something when cmdk_modal.html's activate() calls el.click():
# a real navigation, an htmx request, or an Alpine click handler.
_HTMX_VERB_ATTRS = ("hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete")
_CLICK_HANDLER_ATTRS = ("@click", "x-on:click", "onclick")
_ACTION_ATTRS = ("href", *_HTMX_VERB_ATTRS, *_CLICK_HANDLER_ATTRS)

# The selectable affordances on `row_class`. An informational row must not advertise these.
_SELECTABLE_STYLE_CLASSES = ("cursor-pointer", "hover:bg-gray-100")


def _render(**overrides: Any) -> str:
    """Render palette_results.html with a search_page()-shaped context.

    ``search_page`` always passes all five result keys; each group's ``{% if %}`` decides
    whether it renders. Overrides populate individual groups.
    """
    # autoescape=True mirrors Jinja2Templates' own configuration in phaze.routers.search.
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    context: dict[str, Any] = {
        "query": "bonobo",
        "file_results": [],
        "tracklist_results": [],
        "discogs_results": [],
        "artists": [],
        "artist": None,
    }
    context.update(overrides)
    return env.get_template(_PALETTE_RESULTS).render(**context)


class _Row:
    """A stand-in for a SearchResult row; the template only reads .id/.title/.artist."""

    def __init__(self, id: str, title: str, artist: str | None = None) -> None:
        self.id = id
        self.title = title
        self.artist = artist


_FILE_ROWS = [_Row("11111111-1111-4111-8111-111111111111", "bonobo - migration.mp3", "Bonobo")]
_TRACKLIST_ROWS = [_Row("22222222-2222-4222-8222-222222222222", "Bonobo @ Coachella 2024", "Bonobo")]
_DISCOGS_ROWS = [_Row("33333333-3333-4333-8333-333333333333", "Migration", "Bonobo")]

# Every group populated at once, plus each group in isolation, so a regression in any one
# group is attributed rather than masked by its neighbours.
_CONTEXTS: dict[str, dict[str, Any]] = {
    "discogs_only": {"discogs_results": _DISCOGS_ROWS},
    "files_only": {"file_results": _FILE_ROWS},
    "tracklists_only": {"tracklist_results": _TRACKLIST_ROWS},
    "artists_only": {"artists": ["Bonobo"]},
    "all_groups": {
        "file_results": _FILE_ROWS,
        "tracklist_results": _TRACKLIST_ROWS,
        "discogs_results": _DISCOGS_ROWS,
        "artists": ["Bonobo"],
    },
}


def _options(html: str) -> list[Tag]:
    """Every selectable row, in DOM order — exactly what onResults() collects."""
    return [el for el in BeautifulSoup(html, "html.parser").find_all(attrs={"role": "option"}) if isinstance(el, Tag)]


def _is_actionable(option: Tag) -> bool:
    """True if clicking this row does anything — navigation, htmx request, or click handler."""
    return any(option.has_attr(attr) for attr in _ACTION_ATTRS)


def _describe(option: Tag) -> str:
    return f"<{option.name} id={option.get('id')!r} class={option.get('class')!r}>"


@pytest.mark.parametrize("name", sorted(_CONTEXTS))
def test_every_option_row_is_actionable(name: str) -> None:
    """The core invariant: no role="option" row is a dead end.

    Before the phaze-vxg6 fix this failed for every context containing discogs_results.
    """
    options = _options(_render(**_CONTEXTS[name]))
    assert options, f"context {name!r} rendered no role=option rows"
    inert = [_describe(o) for o in options if not _is_actionable(o)]
    assert not inert, (
        f'context {name!r}: these rows are exposed as role="option" — so cmdk_modal.html\'s '
        f"roving nav lands on them, aria-activedescendant announces them, and Enter calls "
        f".click() on them — but they carry none of {_ACTION_ATTRS}, making Enter a silent "
        f"no-op: {inert}"
    )


def test_discogs_rows_are_not_selectable_targets() -> None:
    """The Discogs group is informational: it must not enter the roving-index list at all."""
    soup = BeautifulSoup(_render(discogs_results=_DISCOGS_ROWS), "html.parser")
    rows = [el for el in soup.find_all("div") if isinstance(el, Tag) and "Migration" in el.get_text()]
    discogs_rows = [el for el in rows if "cmdk-row-static" in (el.get("class") or [])]
    assert discogs_rows, "the Discogs result row did not render"
    for row in discogs_rows:
        assert row.get("role") != "option", "a Discogs row is still advertised as a selectable option"
        assert not row.has_attr("tabindex"), "a Discogs row is still focusable via tabindex"


def test_non_actionable_rows_do_not_look_clickable() -> None:
    """A row with no action must not carry cursor-pointer / hover — it lies about being clickable."""
    soup = BeautifulSoup(_render(**_CONTEXTS["all_groups"]), "html.parser")
    liars = [
        _describe(el)
        for el in soup.find_all(class_="cmdk-row-static")
        if isinstance(el, Tag) and any(cls in (el.get("class") or []) for cls in _SELECTABLE_STYLE_CLASSES)
    ]
    assert not liars, f"informational rows carry selectable styling but no action: {liars}"


def test_discogs_group_still_renders_its_content() -> None:
    """Guard the fix against over-correction: the group must still be visible, just inert."""
    html = _render(discogs_results=_DISCOGS_ROWS)
    assert "Discogs" in html, "the Discogs group header disappeared"
    assert "Migration" in html, "the Discogs release title disappeared"
