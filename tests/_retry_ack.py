"""Shape checks for the Files-matrix per-row retry acknowledgement (phaze-itjat + phaze-wzfzo.10, merged in phaze-wzfzo.19).

A per-row ack must not land in the table row (an inline span there overlapped the neighbouring cells): it goes OOB to the
shell's ``#toast-container``. A per-row SUCCESS auto-dismisses; a per-row FAILURE is ``role="alert"`` and stays until the
operator dismisses it. ``tests/shared/routers/pipeline/test_retry_ack_toast.py`` runs each predicate against the markup
it exists to reject.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag


TOAST_OOB = "beforeend:#toast-container"


def top_level(html: str) -> list[Tag]:
    return [node for node in BeautifulSoup(html, "html.parser").contents if isinstance(node, Tag)]


def toasts(html: str) -> list[Tag]:
    """Every toast the response appends to ``#toast-container``."""
    return [child for node in top_level(html) if node.get("hx-swap-oob") == TOAST_OOB for child in node.children if isinstance(child, Tag)]


def ack_lands_in_the_row(html: str) -> bool:
    """True when any top-level node would swap into the row's result span rather than OOB somewhere.

    A route response also carries the stage-pill OOB swaps (``hx-swap-oob="true"``) that refresh the row's pill; those
    are the row's own cells, not an ack, so only a node with NO ``hx-swap-oob`` counts as landing in the row.
    """
    return any(not node.has_attr("hx-swap-oob") for node in top_level(html))


def auto_dismisses(node: Tag) -> bool:
    """True when the node, or anything inside it, schedules its own hiding or removal."""
    for el in [node, *node.find_all(True)]:
        attrs = " ".join(str(value) for value in el.attrs.values())
        if "setTimeout" in attrs or "x-show" in el.attrs or el.has_attr("data-action-status"):
            return True
    return False


def assert_auto_dismissing_success_toast(html: str, text: str) -> None:
    assert not ack_lands_in_the_row(html), html
    (toast,) = toasts(html)
    assert text in toast.get_text(" ", strip=True)
    assert toast["role"] == "status"
    assert auto_dismisses(toast)


def assert_persistent_failure_toast(html: str, text: str) -> None:
    assert not ack_lands_in_the_row(html), html
    (toast,) = toasts(html)
    assert text in toast.get_text(" ", strip=True)
    assert toast["role"] == "alert"
    assert not auto_dismisses(toast), "a per-row retry failure must stay until the operator dismisses it (phaze-wzfzo.10)"
    # The shell's action-failure contract (shell/partials/action_feedback.html) owns the dismissal.
    assert toast.has_attr("data-action-failure")
    dismiss = toast.select_one("button[data-dismiss-action-error]")
    assert dismiss is not None
    assert dismiss.get("type") == "button"
