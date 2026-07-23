"""phaze-fdo5 -- the detail pane's Esc guard must detect fixed-position ``role="dialog"`` layers.

``_detail_pane.html`` is a NON-modal ``role="region"`` shell (D-01) that borrows the record host's
Esc discipline: while a modal dialog is layered on top, its own ``@keydown.escape.window`` must
stand down and let the dialog's own handler close only itself. The guard did this by testing
``d.offsetParent !== null`` over every ``[role="dialog"]`` in the document -- but BOTH of the app's
dialog layers (the (cmd)K palette's ``#cmdk-dialog`` in ``cmdk_modal.html`` and the force-skip
confirm in ``_force_skip_dialog.html``) are ``position: fixed``, and per CSSOM ``offsetParent`` is
ALWAYS ``null`` for a fixed-position element regardless of visibility. So the guard's ``.some(...)``
was silently always ``false``, and ``hide()`` ran on every Esc even with a fixed dialog open on top
of the pane -- a single Esc closed both the palette/dialog AND the detail pane underneath it.

``record_host.html`` documents (and ``test_force_skip_dialog_escape_does_not_close_record.py``
locks in) the correct visibility test for a mix of fixed and non-fixed dialogs:
``d.getClientRects().length > 0``. This test asserts the detail pane's guard uses the same check
and never reintroduces ``offsetParent``.

Like the sibling record-host test, this renders the real template with no DB and no client fixture
(fast lane) and asserts over the rendered guard expression, not by id/position, so it survives the
fix being reshaped.
"""

from __future__ import annotations

from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_DETAIL_PANE = "pipeline/partials/_detail_pane.html"


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)


def _render_detail_pane() -> str:
    return _env().get_template(_DETAIL_PANE).render()


def _escape_guard(rendered: str) -> str:
    match = re.search(r'@keydown\.escape\.window="([^"]*)"', rendered)
    assert match, "expected the detail pane root to register @keydown.escape.window"
    return match.group(1)


def test_detail_pane_escape_guard_uses_get_client_rects() -> None:
    """The guard must check live visibility via getClientRects(), which is correct for both
    fixed- and non-fixed-position dialogs (phaze-fdo5 fix)."""
    guard = _escape_guard(_render_detail_pane())
    assert "getClientRects" in guard, f"the detail pane's Escape guard ({guard!r}) does not check getClientRects()"


def test_detail_pane_escape_guard_does_not_use_offset_parent() -> None:
    """offsetParent is always null for position:fixed dialogs (the (cmd)K palette and the
    force-skip confirm both are), so an offsetParent-based check is silently always false --
    the exact regression this bead fixes."""
    guard = _escape_guard(_render_detail_pane())
    assert "offsetParent" not in guard, (
        f"the detail pane's Escape guard ({guard!r}) reintroduces the always-null offsetParent check -- "
        f"both cmdk_modal.html's #cmdk-dialog and _force_skip_dialog.html are position:fixed, so it "
        f"must use getClientRects() instead."
    )


def test_detail_pane_escape_guard_still_defers_to_open_dialogs() -> None:
    """Sanity: the guard still stands down (does not call hide()) when a dialog is present --
    this bead fixes the visibility TEST, not the deferral logic itself."""
    guard = _escape_guard(_render_detail_pane())
    assert 'querySelectorAll(\'[role=&quot;dialog&quot;]\')' in guard or 'querySelectorAll("[role=\\"dialog\\"]")' in guard, (
        f"the detail pane's Escape guard ({guard!r}) no longer inspects role=dialog elements for a live nested dialog"
    )
    assert guard != "if (open) hide()", "the detail pane's Escape guard must not unconditionally close the pane"
