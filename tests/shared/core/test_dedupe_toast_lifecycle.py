"""Static template guards for the dedupe toast partials (phaze-ebyjh / phaze-yjl9v / phaze-yc1qk).

Bug-hunt 2026-08-08 (tpl-duplicates finder slice) confirmed three distinct defects in
``duplicates/partials/toast.html`` and its sibling ``bulk_resolve_response.html``, all born
of the same root cause: the toast idiom was authored once and never revisited when it was
hoisted into shell-level chrome (``#toast-container``, phaze-gzrd) that outlives a stage swap.

These are static, source-level checks -- like ``test_no_uncleaned_timers.py`` and
``test_hx_target_contracts.py`` in this directory -- so they run without a database or a
rendered response, and they pin the SHAPE of the fix rather than any particular wording.
"""

from __future__ import annotations

from pathlib import Path
import re


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_TOAST = _TEMPLATES / "duplicates" / "partials" / "toast.html"
_BULK_RESOLVE_RESPONSE = _TEMPLATES / "duplicates" / "partials" / "bulk_resolve_response.html"

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _rendered_prose(path: Path) -> str:
    """Strip ``{# ... #}`` dev-comment blocks so scans only see markup that actually renders."""
    return _JINJA_COMMENT.sub(" ", path.read_text(encoding="utf-8"))


def test_toast_removes_its_dom_node_after_the_leave_transition() -> None:
    """phaze-ebyjh: the toast must detach itself, not just hide via x-show.

    ``x-show`` alone only toggles ``display:none`` -- the node (and its Alpine component,
    and any hidden ``file_states``/``group_hashes`` inputs it carries) stays in
    ``#toast-container`` forever in the never-reloading v7 shell. The fix wires a
    ``transitionend`` listener that removes the element once its leave animation
    (``x-transition:leave``) finishes.
    """
    src = _TOAST.read_text(encoding="utf-8")
    assert "x-transition:leave" in src, "toast lost its leave transition -- removal-on-transitionend has nothing to hook"
    assert "transitionend" in src, "toast.html no longer listens for transitionend -- the DOM node is never detached (phaze-ebyjh regression)"
    assert "$el.remove()" in src, "toast.html's transitionend handler no longer removes the element"


def test_toast_undo_does_not_hide_optimistically_on_click() -> None:
    """phaze-yjl9v: the undo buttons must not self-dismiss the toast on click alone.

    The toast lives in shell-level chrome that outlives a stage swap, but both undo forms
    ``hx-target`` an id that only exists inside the current stage. When that target is gone,
    htmx raises ``htmx:targetError`` and never issues the request -- so a bare
    ``@click="show = false"`` hid the toast (and discarded its file_states payload, the ONLY
    copy of the undo data) while the undo silently never happened. The toast must only
    dismiss once htmx confirms the request actually completed.
    """
    src = _TOAST.read_text(encoding="utf-8")
    assert '@click="show = false"' not in src, (
        "toast.html's undo button still hides the toast optimistically on click -- a request that "
        "never fires (htmx:targetError, stale stage target) leaves the operator believing an undo "
        "happened when it did not (phaze-yjl9v regression)"
    )
    # The dismissal must be driven by request outcome, not the click itself.
    assert "htmx:after-request" in src, "toast.html no longer reacts to htmx:afterRequest -- dismissal is not tied to request success"
    assert "successful" in src, "toast.html's afterRequest handler no longer checks event.detail.successful"


def test_toast_undo_surfaces_a_failed_or_dropped_request() -> None:
    """phaze-yjl9v: a dropped/failed undo must be visible to the operator, not silent.

    At minimum (per the bead's fix hint) a target-error / non-2xx response must be
    surfaced so the operator does not believe an undo succeeded when the request never
    ran, or ran and failed.
    """
    src = _TOAST.read_text(encoding="utf-8")
    assert "htmx:target-error" in src, "toast.html does not react to htmx:targetError -- a dropped undo request is still silent"
    # Both undo forms (bulk + single) must carry the failure-surfacing wiring, not just one.
    assert src.count("htmx:target-error") >= 2, "only one of the two undo forms (bulk/single) surfaces htmx:targetError"
    assert src.count("undoFailed") >= 2, "no visible failure state is rendered for a dropped/failed undo"


def test_bulk_resolve_empty_state_carries_no_pagination_wording() -> None:
    """phaze-yc1qk: the bulk-resolve status strip must not reference pages/pagination.

    ``bulk_resolve_response.html``'s primary swap body is the last surviving fragment of
    the deleted, paginated ``duplicates/list.html``-era UI. Its only live landing spot,
    the v7 dedupe workspace's ``#dedupe-bulk-response`` status strip, has no pagination at
    all -- so any "next page" style wording sends the operator hunting for a control that
    was removed in the Phase-62 cutover.
    """
    src = _rendered_prose(_BULK_RESOLVE_RESPONSE).lower()
    for banned in ("next page", "this page", "page.", " page,", "paginat"):
        assert banned not in src, f"bulk_resolve_response.html still carries pagination wording ({banned!r}) the v7 workspace has no control for"
    assert "undo toast" in src, "bulk_resolve_response.html no longer tells the operator how to reverse the bulk resolve"
