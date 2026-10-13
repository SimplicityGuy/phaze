"""Per-row retry acks leave the table row; per-row failures persist; bulk acks keep their card shapes.

Two intended behaviours meet in these partials. phaze-itjat (main 7719069a): the Files-matrix per-row Retry swapped
its ack inline into the row, where it overlapped neighbouring cells, so a per-row ack goes OOB to the shell's
``#toast-container`` and a per-row SUCCESS auto-dismisses. phaze-wzfzo.10: an action FAILURE is announced
(``role="alert"``) and never disappears on a timer. phaze-wzfzo.19 resolved the merge of the two: a per-row failure is
a persistent, dismissible alert toast, never an auto-dismissing one and never an inline span in the row.

Each predicate is first run against the markup it exists to reject (the negative controls), so a predicate that
silently stopped discriminating fails here instead of passing everything.
"""

from __future__ import annotations

import pytest

from phaze.routers.pipeline._common import templates
from tests._retry_ack import (
    ack_lands_in_the_row,
    assert_auto_dismissing_success_toast,
    assert_persistent_failure_toast,
    auto_dismisses,
    toasts,
    top_level,
)


_ANALYZE = "pipeline/partials/retry_failed_response.html"
_METADATA = "pipeline/partials/metadata_retry_response.html"

_SUCCESSES = [
    (_ANALYZE, {"count": 1, "no_active_agent": False}, "Re-queued 1 failed file(s) for analysis."),
    (_ANALYZE, {"count": 0, "no_active_agent": False}, "No failed files to retry."),
    (_METADATA, {"count": 1, "no_active_agent": False}, "Re-queued 1 failed file(s) for metadata extraction."),
    (_METADATA, {"count": 0, "no_active_agent": False}, "No failed files to retry."),
]
_FAILURES = [
    (_ANALYZE, {"count": 0, "no_active_agent": True}, "No active agent"),
    (_ANALYZE, {"count": 0, "no_active_agent": False, "enqueue_failed": True}, "could not re-queue"),
    (_ANALYZE, {"count": 0, "no_active_agent": False, "blocked": True}, "stuck and holding its slot"),
    (_METADATA, {"count": 0, "no_active_agent": True}, "No active agent"),
]

# The shapes these partials must NOT produce, from the two sides of the merge: main 7719069a's toast (right for a
# success, wrong for a failure, which it auto-dismissed) and the epic's inline ack (right on a bulk card, wrong in a
# table row, which it overlapped).
_MAIN_AUTO_DISMISSING_FAILURE = (
    '<div hx-swap-oob="beforeend:#toast-container"><div class="rounded bg-gray-800 px-4 py-2 text-sm text-white" role="status" '
    'x-data="{ show: true }" x-show="show" x-init="setTimeout(() => show = false, 5000)" x-transition>'
    "No active agent — nothing re-queued.</div></div>"
)
_EPIC_INLINE_FAILURE = '<span role="alert" class="text-sm text-warn font-medium">No active agent — nothing re-queued.</span>'
_EPIC_INLINE_SUCCESS = '<p role="status" data-action-status class="text-sm text-info">Re-queued 1 failed file(s) for analysis.</p>'
_PILL_OOB = '<span id="files-stage-pill-analyze-x" hx-swap-oob="true">pill</span>'


# --- negative controls: every predicate rejects the shape it guards against ------------------------------------------


def test_row_predicate_rejects_an_inline_ack_and_accepts_toasts_plus_pill_swaps() -> None:
    assert ack_lands_in_the_row(_EPIC_INLINE_FAILURE)
    assert ack_lands_in_the_row(_EPIC_INLINE_SUCCESS)
    assert ack_lands_in_the_row(_MAIN_AUTO_DISMISSING_FAILURE + _EPIC_INLINE_SUCCESS)
    assert not ack_lands_in_the_row(_MAIN_AUTO_DISMISSING_FAILURE + _PILL_OOB)


def test_dismiss_predicate_rejects_main_7719069a_failure_toast_and_the_transient_status() -> None:
    assert auto_dismisses(toasts(_MAIN_AUTO_DISMISSING_FAILURE)[0])
    assert auto_dismisses(top_level(_EPIC_INLINE_SUCCESS)[0])
    assert not auto_dismisses(top_level(_EPIC_INLINE_FAILURE)[0])


def test_failure_assertion_rejects_main_7719069a_failure_toast() -> None:
    with pytest.raises(AssertionError, match="must stay until the operator dismisses it"):
        assert_persistent_failure_toast(_MAIN_AUTO_DISMISSING_FAILURE.replace('role="status"', 'role="alert"'), "No active agent")


def test_failure_assertion_rejects_the_epic_inline_span_in_a_row() -> None:
    with pytest.raises(AssertionError):
        assert_persistent_failure_toast(_EPIC_INLINE_FAILURE, "No active agent")


# --- per-row (toast=True) --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "ctx", "text"), _SUCCESSES)
def test_per_row_success_is_an_auto_dismissing_toast_outside_the_row(name: str, ctx: dict[str, object], text: str) -> None:
    html = templates.get_template(name).render(**ctx, toast=True)

    assert_auto_dismissing_success_toast(html, text)
    assert "setTimeout(() => show = false, 5000)" in toasts(html)[0]["x-init"]


@pytest.mark.parametrize(("name", "ctx", "text"), _FAILURES)
def test_per_row_failure_is_a_persistent_alert_toast_outside_the_row(name: str, ctx: dict[str, object], text: str) -> None:
    assert_persistent_failure_toast(templates.get_template(name).render(**ctx, toast=True), text)


# --- bulk (toast unset) ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "ctx", "text"), _FAILURES)
def test_bulk_failure_stays_inline_and_never_reaches_the_toasts(name: str, ctx: dict[str, object], text: str) -> None:
    html = templates.get_template(name).render(**ctx)

    assert "toast-container" not in html
    (span,) = top_level(html)
    assert span.name == "span"
    assert text in span.get_text(" ", strip=True)
    assert not auto_dismisses(span)


@pytest.mark.parametrize("ctx", [ctx for name, ctx, _ in _FAILURES if name == _ANALYZE])
def test_bulk_analysis_failure_is_announced(ctx: dict[str, object]) -> None:
    (span,) = top_level(templates.get_template(_ANALYZE).render(**ctx))

    assert span["role"] == "alert"


@pytest.mark.parametrize(("ctx", "text"), [(ctx, text) for name, ctx, text in _SUCCESSES if name == _ANALYZE])
def test_bulk_analysis_success_is_the_transient_action_status(ctx: dict[str, object], text: str) -> None:
    html = templates.get_template(_ANALYZE).render(**ctx)

    assert "toast-container" not in html
    (status,) = top_level(html)
    assert status.has_attr("data-action-status")
    assert status.get_text(strip=True) == text


@pytest.mark.parametrize(("ctx", "text"), [(ctx, text) for name, ctx, text in _SUCCESSES if name == _METADATA])
def test_bulk_metadata_success_keeps_its_inline_span(ctx: dict[str, object], text: str) -> None:
    html = templates.get_template(_METADATA).render(**ctx)

    assert "toast-container" not in html
    (span,) = top_level(html)
    assert span.name == "span"
    assert span.get_text(" ", strip=True) == text
