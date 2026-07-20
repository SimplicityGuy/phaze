"""Unit tests for the htmx response-shape contract helpers (phaze-qi9j).

``src/phaze/routers/response_shape.py`` is THE contract every handler composes when it chooses a
document shape or the status of a renderable error. Rule 5 of that contract makes every claim in its
docstring a test obligation, so the predicates' guarantees are pinned here independently of any one
router -- the audit-log regression that motivated the module lives in
``tests/review/routers/test_execution.py``.
"""

from pathlib import Path
import re

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


# ---------------------------------------------------------------------------
# Corpus-level guard (phaze-64uy)
#
# Contract rule 1 is phrased as a BAN on the raw ``HX-Request`` header rather than as advice,
# precisely because the raw check reads correct at every call site that has one. Nothing local to
# a handler looks wrong; the defect lives in the gap between what the header says and what the
# handler assumes. A per-handler test therefore cannot enforce rule 1 -- it only pins the handlers
# someone already thought to test.
#
# phaze-qi9j fixed ONE instance (audit_log) and landed the contract. phaze-64uy then found SEVEN
# more still branching on the raw header, including the /s/* shell rail -- the app's primary
# navigation. That is the shape of the failure this guard exists to prevent: the rule was written
# down and the corpus quietly disagreed with it.
# ---------------------------------------------------------------------------


_ROUTERS_DIR = Path(__file__).parents[3] / "src" / "phaze" / "routers"

# Matches any direct read of the header off a request, in either casing htmx/Starlette accept.
# Deliberately NOT anchored to ``request.`` -- a helper taking ``req`` or ``r`` re-derives the same
# banned decision, which is exactly what admin_agents.py's ``_is_htmx`` used to do.
_RAW_HEADER_READ = re.compile(r"""headers\s*\.\s*get\s*\(\s*["']hx-request["']\s*\)""", re.IGNORECASE)


def test_no_router_branches_on_the_raw_hx_request_header() -> None:
    """No router may read ``HX-Request`` directly; ``wants_fragment`` is the only way to ask.

    Contract rule 1. ``response_shape.py`` itself is the sole exemption -- it is where the header
    is allowed to be spelled, because it is the module that turns the header into the decision.

    This is a corpus guard, not a per-handler assertion: it fails for a handler nobody has written
    a shape test for yet, which is the case the per-handler regressions structurally cannot cover.
    """
    offenders: list[str] = []
    for path in sorted(_ROUTERS_DIR.rglob("*.py")):
        if path.name == "response_shape.py":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            # Skip comment lines: the converted handlers NAME the banned header in order to explain
            # why they no longer branch on it, and prose is not a branch.
            if line.lstrip().startswith("#"):
                continue
            if _RAW_HEADER_READ.search(line):
                offenders.append(f"{path.relative_to(_ROUTERS_DIR.parents[2])}:{lineno}: {line.strip()}")

    assert not offenders, (
        "These routers branch on the raw HX-Request header, which contract rule 1 of "
        "src/phaze/routers/response_shape.py BANS.\n\n"
        "The header means 'htmx issued this request', NOT 'send a fragment' -- htmx sets it on\n"
        "history restores too (historyRestoreAsHxRequest defaults true), and on a restore it\n"
        "IGNORES hx-target and swaps the response into <body>. A handler answering a restore with\n"
        "a chrome-less fragment therefore REPLACES THE WHOLE PAGE with that fragment.\n\n"
        "FIX: compose phaze.routers.response_shape.wants_fragment(request) instead. It is exactly\n"
        "'htmx asked AND it is not restoring', and it is the ONLY sanctioned way to ask.\n\n"
        "Offending lines:\n  " + "\n  ".join(offenders)
    )


def test_no_template_branches_on_the_raw_hx_request_header() -> None:
    """No Jinja template may derive its own shape flag from the raw header either (rule 1).

    The routers are only half the corpus. ``tracklists/partials/tracklist_list.html`` and
    ``tracklists/partials/scan_tab.html`` each computed ``is_hx`` from
    ``request.headers.get('HX-Request')`` inside the template, and both got the restore case
    wrong in the nastier direction: they SUPPRESSED their wrapper ``<div>``, so the restored
    full document carried ZERO copies of the id every later swap targets, stranding the page
    even after the handler above it was fixed (phaze-xc84, phaze-64uy).

    The flag belongs to the HANDLER, passed in as ``wants_fragment(request)``. An undefined
    ``is_hx`` stays falsy in Jinja, so a render that forgets it emits the wrapper -- the
    recoverable direction.
    """
    templates_dir = Path(__file__).parents[3] / "src" / "phaze" / "templates"
    offenders: list[str] = []
    for path in sorted(templates_dir.rglob("*.html")):
        source = path.read_text()
        # Strip Jinja comments: the fixed templates necessarily NAME the header to explain the fix.
        stripped = re.sub(r"\{#.*?#\}", "", source, flags=re.DOTALL)
        for lineno, line in enumerate(stripped.splitlines(), start=1):
            if _RAW_HEADER_READ.search(line):
                offenders.append(f"{path.relative_to(templates_dir.parents[2])}:{lineno}: {line.strip()}")

    assert not offenders, (
        "These templates derive a shape flag from the raw HX-Request header, banned by contract\n"
        "rule 1 of src/phaze/routers/response_shape.py for the same reason it is banned in the\n"
        "routers -- a history restore sets that header too.\n\n"
        "FIX: have the HANDLER pass the flag in as response_shape.wants_fragment(request).\n\n"
        "Offending lines:\n  " + "\n  ".join(offenders)
    )
