"""Unit tests for the htmx response-shape contract helpers (phaze-qi9j).

``src/phaze/routers/response_shape.py`` is THE contract every handler composes when it chooses a
document shape or the status of a renderable error. Rule 5 of that contract makes every claim in its
docstring a test obligation, so the predicates' guarantees are pinned here independently of any one
router -- the audit-log regression that motivated the module lives in
``tests/review/routers/test_execution.py``.
"""

from fastapi import Request
import pytest

from phaze.routers.response_shape import (
    RENDERABLE_ALERT_STATUS,
    is_history_restore,
    is_htmx_request,
    wants_fragment,
)


def make_request(headers: dict[str, str] | None = None) -> Request:
    """Build a bare ASGI ``Request`` carrying ``headers`` and nothing else.

    The predicates read headers only, so a scope-level request avoids standing up an app or a
    database for what is a pure header decision.
    """
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/audit/", "headers": raw})


# The four shapes contract rule 2 and the wants_fragment docstring enumerate.
PLAIN = {}
HTMX_SWAP = {"HX-Request": "true"}
HISTORY_RESTORE = {"HX-Request": "true", "HX-History-Restore-Request": "true"}
RESTORE_ONLY = {"HX-History-Restore-Request": "true"}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (PLAIN, False),
        (HTMX_SWAP, True),
        (HISTORY_RESTORE, True),  # htmx sets HX-Request on restores too (historyRestoreAsHxRequest)
        (RESTORE_ONLY, False),
    ],
)
def test_is_htmx_request_answers_only_did_htmx_send_this(headers: dict[str, str], expected: bool) -> None:
    """The weak predicate tracks ``HX-Request`` alone -- including on a history restore (rule 1)."""
    assert is_htmx_request(make_request(headers)) is expected


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (PLAIN, False),
        (HTMX_SWAP, False),
        (HISTORY_RESTORE, True),
        (RESTORE_ONLY, True),  # independent of HX-Request: the restore header dominates (rule 2)
    ],
)
def test_is_history_restore_is_independent_of_hx_request(headers: dict[str, str], expected: bool) -> None:
    """A restore is a restore whether or not ``HX-Request`` accompanies it (rule 2)."""
    assert is_history_restore(make_request(headers)) is expected


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (PLAIN, False),  # plain browser navigation
        (HTMX_SWAP, True),  # the ONLY true case: a live in-page swap
        (HISTORY_RESTORE, False),  # phaze-qi9j: htmx swaps this into <body>
        (RESTORE_ONLY, False),
    ],
)
def test_wants_fragment_is_true_only_for_a_live_swap(headers: dict[str, str], expected: bool) -> None:
    """The canonical shape predicate, over every shape the docstring enumerates (rules 1 and 2)."""
    assert wants_fragment(make_request(headers)) is expected


def test_wants_fragment_never_disagrees_with_a_restore() -> None:
    """Rule 2's dominance claim, stated as an invariant rather than a case list.

    There is no header combination in which a restore request wants a partial.
    """
    for headers in (PLAIN, HTMX_SWAP, HISTORY_RESTORE, RESTORE_ONLY):
        request = make_request(headers)
        if is_history_restore(request):
            assert wants_fragment(request) is False


@pytest.mark.parametrize("value", ["", "false", "True", "TRUE", "1", "yes"])
def test_headers_are_matched_against_the_exact_literal_true(value: str) -> None:
    """Only htmx's own lowercase ``"true"`` counts; a stray value is not htmx (rule 1).

    Pins the documented value comparison so a handler cannot be tricked into shedding its chrome by
    a hand-rolled or proxy-mangled header.
    """
    assert is_htmx_request(make_request({"HX-Request": value})) is False
    assert is_history_restore(make_request({"HX-History-Restore-Request": value})) is False
    assert wants_fragment(make_request({"HX-Request": value})) is False


@pytest.mark.parametrize("name", ["hx-request", "HX-Request", "Hx-ReQuEsT"])
def test_header_name_lookup_is_case_insensitive(name: str) -> None:
    """Starlette normalises header NAMES, so any casing htmx or a proxy sends resolves (rule 1)."""
    assert is_htmx_request(make_request({name: "true"})) is True


def test_renderable_alert_status_is_200() -> None:
    """Contract rule 3: a body htmx must SWAP cannot carry a 4xx/5xx.

    htmx 2.x's stock ``responseHandling`` maps ``[45]..`` to ``{swap: false, error: true}``, so any
    other value here would silently discard the alert markup.
    """
    assert RENDERABLE_ALERT_STATUS == 200


def test_renderable_alert_status_is_distinct_from_the_malformed_payload_status() -> None:
    """Contract rule 4: the boundary against ``request_guards`` rule 1 is a real fork, not a synonym.

    A request phaze could not UNDERSTAND is 422; a request it understood and must report bad news
    about is a 200 with an alert body. Pinned together so neither constant can drift into the other.
    """
    from phaze.routers.request_guards import MALFORMED_PAYLOAD_STATUS

    assert RENDERABLE_ALERT_STATUS != MALFORMED_PAYLOAD_STATUS
    assert MALFORMED_PAYLOAD_STATUS == 422
