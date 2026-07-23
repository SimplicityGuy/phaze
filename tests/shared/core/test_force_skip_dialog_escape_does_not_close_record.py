"""phaze-xmce — the force-skip confirm dialog's Esc must not also close the record slide-in.

``_force_skip_dialog.html`` (rendered once per ENRICH stage inside ``record_body.html``, which is
the record slide-in's ``#record-body`` swap target) registers its own ``@keydown.escape.window``
that closes only itself. ``record_host.html`` -- the persistent record slide-in host -- ALSO
registers ``@keydown.escape.window`` unconditionally (``if (open) hide()``), tearing down the
WHOLE panel. Both listeners are bound to the same ``window`` target and neither uses ``.stop``
(which would not help anyway: ``.stop`` maps to ``stopPropagation``, and a second listener already
attached to the SAME target needs ``stopImmediatePropagation`` to be silenced, which Alpine's
modifiers don't expose). So a single Esc fired both handlers, closing the confirm dialog AND the
entire record panel.

This mirrors the already-fixed ``phaze-hltu`` guard (record host vs the ⌘K palette,
``test_cmdk_palette_record_open_closes_palette.py``): the host must check the DOM for a live,
VISIBLE nested dialog rather than assume anything about listener registration order (the force-skip
dialog is inserted well after the host's own listener registers, via the record body's async HTMX
fetch, so order is not a safe assumption here either).

Dispatcher note (do not reintroduce): a SIBLING escape guard in ``pipeline/partials/
_detail_pane.html`` checks ``d.offsetParent !== null`` to detect an open nested dialog (phaze-fdo5,
a later fixgroup). ``offsetParent`` is always ``null`` for ``position: fixed`` elements -- exactly
what both the force-skip dialog and the record panel are -- so that check is silently always
false. The record host's guard must use ``getClientRects()`` (already established by the
paletteIsOpen() guard), never ``offsetParent``.

Like ``test_cmdk_palette_record_open_closes_palette.py``, this renders the real templates with no
DB and no client fixture (fast lane) and asserts over template SOURCE / parsed DOM by attribute and
component wiring, never by id/position -- so it survives the fix being reshaped.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_RECORD_HOST = "shell/partials/record_host.html"
_FORCE_SKIP_DIALOG = "pipeline/partials/_force_skip_dialog.html"


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)


def _render_force_skip_dialog() -> str:
    return (
        _env()
        .get_template(_FORCE_SKIP_DIALOG)
        .render(
            stage_value="analyze",
            stage_label="Analyze",
            file_id="11111111-1111-4111-8111-111111111111",
        )
    )


def _attr(el: Tag, needle: str) -> str:
    """Concatenated values of every attribute whose NAME contains ``needle``."""
    parts: list[str] = []
    for name, value in el.attrs.items():
        if needle in name.lower():
            parts.append(" ".join(value) if isinstance(value, list) else str(value))
    return " ".join(parts)


def _record_host_root() -> Tag:
    """The record slide-in host root -- the element that listens for record:open."""
    soup = BeautifulSoup(_env().get_template(_RECORD_HOST).render(), "html.parser")
    root = next(
        (el for el in soup.find_all(attrs={"x-data": True}) if isinstance(el, Tag) and _attr(el, "record:open")),
        None,
    )
    assert root is not None, "could not find the element listening for record:open in record_host.html"
    return root


def _force_skip_dialog_root() -> Tag:
    """The force-skip confirm dialog's own dialog element (role=dialog, aria-modal)."""
    soup = BeautifulSoup(_render_force_skip_dialog(), "html.parser")
    root = next((el for el in soup.find_all(attrs={"role": "dialog"}) if isinstance(el, Tag)), None)
    assert root is not None, "could not find the force-skip confirm dialog's role=dialog element"
    return root


def test_force_skip_dialog_has_its_own_escape_handler() -> None:
    """Sanity: the confirm dialog still closes itself on Esc (this guard is not disabling that)."""
    dialog = _force_skip_dialog_root()
    handler = _attr(dialog, "escape")
    assert handler, "the force-skip confirm dialog no longer handles Escape"
    assert "hide" in handler


def test_force_skip_dialog_is_position_fixed() -> None:
    """The dialog must be position:fixed (offsetParent is always null for it -- phaze-fdo5 pitfall).

    This is *why* the record host's guard (below) must use getClientRects(), not offsetParent.
    """
    dialog = _force_skip_dialog_root()
    assert "fixed" in str(dialog.get("class") or ""), "the force-skip dialog root is expected to be position:fixed"


def test_record_host_escape_handler_defers_to_a_nested_open_dialog() -> None:
    """One Esc, one layer. With the force-skip confirm open, the record host must stand down.

    Both roots listen ``@keydown.escape.window`` unconditionally listening for the SAME window
    keydown; absent a guard, a single Esc runs both handlers and tears down the entire record panel
    when the operator only meant to dismiss the confirm.
    """
    host_escape = _attr(_record_host_root(), "escape")
    assert host_escape, "the record host root no longer handles Escape"
    assert host_escape != "hide()", (
        f"the record host's Escape handler ({host_escape!r}) closes the record unconditionally -- "
        f"it must defer to a nested, currently-open modal (e.g. the force-skip confirm dialog) "
        f"nested inside #record-body."
    )


def test_record_host_escape_guard_checks_record_body_for_open_dialogs() -> None:
    """The guard must inspect #record-body (where _force_skip_dialog.html is rendered), not assume
    listener registration order -- the dialog is inserted well after the host's own listener
    registers, via the record body's async HTMX fetch, so order is not a safe basis for the guard
    (mirrors the phaze-hltu paletteIsOpen() guard's own reasoning).
    """
    host = _env().get_template(_RECORD_HOST).render()
    assert "record-body" in host, "the record host's nested-modal guard does not inspect #record-body"
    assert "getClientRects" in host, "the record host's nested-modal guard does not check live visibility via getClientRects()"


def test_record_host_escape_guard_does_not_use_offset_parent() -> None:
    """phaze-fdo5 pitfall guard: offsetParent is always null for position:fixed dialogs (both the
    record panel and the force-skip confirm are), so an offsetParent-based check is silently always
    false. The record host's guard must not reintroduce that pattern as actual CODE (the palette
    guard's own explanatory comment mentions the word ``offsetParent`` by name to justify why it
    uses getClientRects() instead -- that mention is fine; an actual ``.offsetParent`` property
    access is not).
    """
    host = _env().get_template(_RECORD_HOST).render()
    assert ".offsetParent" not in host, "the record host's Escape guard must use getClientRects(), not the always-null offsetParent check"
