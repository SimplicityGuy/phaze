"""phaze-hltu — a ⌘K palette row that opens the record slide-in must also close the palette.

``cmdk_modal.html``'s root is a MODAL layer: ``fixed inset-0 z-50`` with a ``bg-black/60``
backdrop and ``x-trap.inert.noscroll`` on the panel. ``record_host.html`` — the only
``record:open`` listener — is ``z-40``. So a palette row that opens the record without closing
the palette renders it *underneath* the palette's full-viewport backdrop: dimmed, and inert,
because the focus trap makes it unreachable. Both roots also listen
``@keydown.escape.window``, so a single Esc tore down both layers at once. Net: no interaction
sequence yielded a usable record view from the ⌘K Files row.

The invariant guarded here: **if a palette row opens the record slide-in, the palette closes.**
Two wirings satisfy it and this guard accepts either —

1. the row's own click handler calls the palette's ``hide()`` alongside the dispatch, or
2. ``cmdkPalette`` listens for ``record:open`` and closes itself (the shipped shape — it covers
   every current *and* future record-opening row rather than one).

A second invariant covers the Esc coupling: with both layers somehow open, one Esc must not
close both. The palette is the upper layer, so the record host's Esc handler must stand down
while the palette is open.

Like ``test_cmdk_palette_default_option.py`` and ``test_cmdk_palette_row_actionability.py``,
this renders the real templates with no DB and no client fixture (fast lane) and asserts over
the parsed DOM by ATTRIBUTE and COMPONENT WIRING — never by element id or position — so the
guard survives rows being reordered, renamed, or regrouped, and survives the fix being moved
between the two shapes above.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader
import pytest


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_PALETTE_RESULTS = "search/partials/palette_results.html"
_CMDK_MODAL = "shell/partials/cmdk_modal.html"
_RECORD_HOST = "shell/partials/record_host.html"

# The window event that opens the record slide-in (record_host.html's @record:open.window).
_RECORD_OPEN_EVENT = "record:open"
# The record host's HTMX swap target. A row aimed here is opening the record, whether or not
# it also dispatches the event.
_RECORD_BODY_TARGET = "#record-body"

_CLICK_HANDLER_ATTRS = ("@click", "x-on:click", "onclick")


def _env() -> Environment:
    # autoescape=True mirrors Jinja2Templates' own configuration in phaze.routers.search.
    return Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)


class _Row:
    """A stand-in for a SearchResult row; the template only reads .id/.title/.artist."""

    def __init__(self, id: str, title: str, artist: str | None = None) -> None:
        self.id = id
        self.title = title
        self.artist = artist


_FILE_ROWS = [_Row("11111111-1111-4111-8111-111111111111", "bonobo - migration.mp3", "Bonobo")]
_TRACKLIST_ROWS = [_Row("22222222-2222-4222-8222-222222222222", "Bonobo @ Coachella 2024", "Bonobo")]
_DISCOGS_ROWS = [_Row("33333333-3333-4333-8333-333333333333", "Migration", "Bonobo")]

_CONTEXTS: dict[str, dict[str, Any]] = {
    "files_only": {"file_results": _FILE_ROWS},
    "all_groups": {
        "file_results": _FILE_ROWS,
        "tracklist_results": _TRACKLIST_ROWS,
        "discogs_results": _DISCOGS_ROWS,
        "artists": ["Bonobo"],
    },
}


def _render_results(**overrides: Any) -> str:
    context: dict[str, Any] = {
        "query": "bonobo",
        "file_results": [],
        "tracklist_results": [],
        "discogs_results": [],
        "artists": [],
        "artist": None,
    }
    context.update(overrides)
    return _env().get_template(_PALETTE_RESULTS).render(**context)


def _soup(template: str) -> BeautifulSoup:
    """Render a variable-free shell partial (strips {# #} comments) and parse it."""
    return BeautifulSoup(_env().get_template(template).render(), "html.parser")


def _attr(el: Tag, needle: str) -> str:
    """Concatenated values of every attribute whose NAME contains ``needle``.

    Alpine listeners can be spelled ``@evt.window`` or ``x-on:evt.window``, so match on the
    attribute name rather than pinning one syntax.
    """
    parts: list[str] = []
    for name, value in el.attrs.items():
        if needle in name.lower():
            parts.append(" ".join(value) if isinstance(value, list) else str(value))
    return " ".join(parts)


def _click_handler(el: Tag) -> str:
    return " ".join(str(el.get(attr) or "") for attr in _CLICK_HANDLER_ATTRS)


def _opens_the_record(el: Tag) -> bool:
    """True if activating this row opens the record slide-in."""
    return _RECORD_OPEN_EVENT in _click_handler(el) or el.get("hx-target") == _RECORD_BODY_TARGET


def _record_opening_options(html: str) -> list[Tag]:
    """Every selectable row that opens the record — exactly what onResults() can activate."""
    return [el for el in BeautifulSoup(html, "html.parser").find_all(attrs={"role": "option"}) if isinstance(el, Tag) and _opens_the_record(el)]


def _describe(el: Tag) -> str:
    return f"<{el.name} id={el.get('id')!r} hx-target={el.get('hx-target')!r} @click={el.get('@click')!r}>"


def _palette_root() -> Tag:
    """The cmdkPalette component root — found by its x-data wiring, not by id or position."""
    root = next(
        (el for el in _soup(_CMDK_MODAL).find_all(attrs={"x-data": True}) if isinstance(el, Tag) and "cmdkPalette" in str(el.get("x-data"))),
        None,
    )
    assert root is not None, "could not find the cmdkPalette component root in cmdk_modal.html"
    return root


def _record_host_root() -> Tag:
    """The record slide-in host root — the element that listens for record:open."""
    root = next(
        (el for el in _soup(_RECORD_HOST).find_all(attrs={"x-data": True}) if isinstance(el, Tag) and _attr(el, _RECORD_OPEN_EVENT)),
        None,
    )
    assert root is not None, f"could not find the element listening for {_RECORD_OPEN_EVENT} in record_host.html"
    return root


def _palette_closes_on_record_open() -> bool:
    """Shape 2: the palette component itself closes when record:open fires."""
    return "hide" in _attr(_palette_root(), _RECORD_OPEN_EVENT)


@pytest.mark.parametrize("name", sorted(_CONTEXTS))
def test_palette_has_rows_that_open_the_record(name: str) -> None:
    """Sanity, and a guard against over-correction: the Files rows still open the record."""
    rows = _record_opening_options(_render_results(**_CONTEXTS[name]))
    assert rows, f"context {name!r} rendered no role=option row that opens the record slide-in"


@pytest.mark.parametrize("name", sorted(_CONTEXTS))
def test_opening_the_record_from_the_palette_closes_the_palette(name: str) -> None:
    """The core invariant. Before the phaze-hltu fix this failed for every context with files.

    Satisfied by either the row closing the palette itself, or the palette closing on
    ``record:open`` — the latter covers every row at once, so it short-circuits the per-row check.
    """
    if _palette_closes_on_record_open():
        return
    stranded = [_describe(row) for row in _record_opening_options(_render_results(**_CONTEXTS[name])) if "hide" not in _click_handler(row)]
    assert not stranded, (
        f"context {name!r}: these palette rows open the record slide-in (z-40) but leave the "
        f"palette (fixed inset-0 z-50, bg-black/60 backdrop, x-trap.inert) open, so the record "
        f"renders dimmed under the backdrop and inert inside the focus trap: {stranded}. Fix by "
        f"calling the palette's hide() in the row's click handler, or by having cmdkPalette "
        f"listen for {_RECORD_OPEN_EVENT!r} and close itself."
    )


def test_palette_hide_on_record_open_cannot_loop() -> None:
    """The shipped shape must not re-enter itself: hide() must not dispatch record:open."""
    body = _env().get_template(_CMDK_MODAL).render()
    hide_body = re.search(r"hide\s*\([^)]*\)\s*\{(.*?)\n            \},", body, re.DOTALL)
    assert hide_body is not None, "could not locate cmdkPalette.hide() in cmdk_modal.html"
    assert _RECORD_OPEN_EVENT not in hide_body.group(1), (
        f"cmdkPalette.hide() dispatches {_RECORD_OPEN_EVENT} — closing the palette would re-trigger itself"
    )


def test_palette_record_open_listener_is_guarded_by_open_state() -> None:
    """The non-modal file table dispatches the same event; the palette must ignore it when closed.

    Without the guard, clicking a file row in files_table_view.html would run the palette's
    focus-return and yank focus to #cmdk-trigger while the record is opening.
    """
    if not _palette_closes_on_record_open():
        pytest.skip("palette does not use the listener shape; per-row hide() is guarded by construction")
    handler = _attr(_palette_root(), _RECORD_OPEN_EVENT)
    assert "open" in handler, f"the palette's {_RECORD_OPEN_EVENT} handler ({handler!r}) is not guarded by its own open state"


def test_escape_with_both_layers_open_does_not_close_both() -> None:
    """One Esc, one layer. The palette is above the record host, so the host stands down.

    Both roots listen ``@keydown.escape.window`` on the same target, so absent a guard a single
    Esc runs both handlers.
    """
    palette_escape = _attr(_palette_root(), "escape")
    host_escape = _attr(_record_host_root(), "escape")
    assert palette_escape, "the palette root no longer handles Escape"
    assert host_escape, "the record host root no longer handles Escape"
    assert "palette" in host_escape.lower(), (
        f"the record host's Escape handler ({host_escape!r}) closes the record unconditionally "
        f"while the palette's ({palette_escape!r}) closes the palette — both fire on the same "
        f"window keydown, so one Esc tears down both layers. The lower layer must defer while "
        f"the palette is open."
    )


def test_record_host_escape_guard_reads_live_palette_visibility() -> None:
    """The guard must not depend on shell.html's include order (listener registration order)."""
    host = _env().get_template(_RECORD_HOST).render()
    assert "getClientRects" in host or "getElementById('cmdk-dialog')" in host, (
        "the record host's palette guard does not read the palette's live visibility"
    )
