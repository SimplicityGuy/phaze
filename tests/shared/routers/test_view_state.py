"""Unit tests for ``routers/view_state.py`` -- the list-view state carrier.

``ListViewState`` makes two promises its callers rely on without checking, so both are asserted
here rather than left to the integration tests that would only notice them indirectly:

* **Parsing is total.** ``from_request`` reads a user-editable, bookmarkable URL and must never
  raise, whatever is in it.
* **``query()`` re-emits everything.** The entire point is that a control states only what it
  changes; if a parameter could silently vanish, every "preserves the filter" assertion elsewhere
  would be testing luck.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import pytest
from starlette.datastructures import QueryParams

from phaze.routers.view_state import DEFAULT_PAGE_SIZE, MAX_PAGE, PAGE_SIZE_CHOICES, ListViewState


class _FakeRequest:
    """Minimal stand-in exposing the one attribute ``from_request`` reads."""

    def __init__(self, query: str) -> None:
        self.query_params = QueryParams(query)


def _parse(state: ListViewState, **kwargs: object) -> dict[str, list[str]]:
    return parse_qs(state.query(**kwargs), keep_blank_values=True)  # type: ignore[arg-type]


def test_defaults_land_on_the_review_queue() -> None:
    """An empty query string yields the pending queue, page 1, smallest page size."""
    state = ListViewState.from_request(_FakeRequest(""))
    assert state.status == "pending"
    assert state.q == ""
    assert state.page == 1
    assert state.page_size == DEFAULT_PAGE_SIZE


def test_every_parameter_round_trips() -> None:
    """A fully-specified URL parses into exactly the state it names."""
    state = ListViewState.from_request(_FakeRequest("status=approved&q=coachella&page=3&page_size=50&sort=proposed_filename&order=desc"))
    assert (state.status, state.q, state.page, state.page_size, state.sort, state.order) == (
        "approved",
        "coachella",
        3,
        50,
        "proposed_filename",
        "desc",
    )


@pytest.mark.parametrize(
    "query",
    [
        "page=banana",
        "page=",
        "page=-4",
        "page=0",
        "page=1e9999",
        "page=99999999999999999999",
        "page_size=banana",
        "page_size=100000",
        "page_size=0",
        "page_size=-1",
        "order=sideways",
        "status=&q=&page=&page_size=&sort=&order=",
    ],
)
def test_unparseable_values_degrade_instead_of_raising(query: str) -> None:
    """Parsing is TOTAL -- junk in the URL renders a sane view, never an exception.

    These are not hypothetical: a truncated share link, a stale bookmark and a hand-edited address
    bar all produce them, and the operator's reasonable expectation is the default view rather than
    a stack trace. ``page_size`` is additionally required to stay inside the closed choice set --
    ``?page_size=100000`` is the unbounded-read defect re-entered through the URL, so honouring it
    would undo the pagination bead.
    """
    state = ListViewState.from_request(_FakeRequest(query))
    assert 1 <= state.page <= MAX_PAGE
    assert state.page_size in PAGE_SIZE_CHOICES
    assert state.order in {"asc", "desc"}


def test_page_is_capped_above_at_max_page() -> None:
    """phaze-h9oz: ``page`` was only lower-bounded -- a huge value must now clamp to MAX_PAGE.

    A hand-edited ``?page=99999999999999999999`` (the failure scenario's exact example) has an
    OFFSET so large asyncpg fails to encode it as a Postgres ``bigint`` bind parameter, and the
    caller's error handling degrades the whole workspace to a false-empty view with zeroed stats.
    Capping here means that offset can never be computed in the first place.
    """
    state = ListViewState.from_request(_FakeRequest("page=99999999999999999999"))
    assert state.page == MAX_PAGE


def test_max_page_keeps_every_possible_offset_far_below_bigint_overflow() -> None:
    """The clamp is effective for every page size the app will ever honour, not just the default."""
    bigint_max = 2**63 - 1
    for page_size in PAGE_SIZE_CHOICES:
        offset = (MAX_PAGE - 1) * page_size
        assert offset < bigint_max


def test_query_emits_every_parameter_even_when_overriding_one() -> None:
    """The anti-drop guarantee: changing ``page`` preserves filter, search, size, sort and order.

    This is the assertion the legacy hand-built pager URLs could not make. Each of its six controls
    re-stated the whole parameter list independently, so "preserves the filter" was a property of
    six correct copies rather than of the mechanism.
    """
    state = ListViewState(status="approved", q="live set", page=2, page_size=50, sort="confidence", order="desc")
    emitted = _parse(state, page=5)
    assert emitted["page"] == ["5"]
    assert emitted["status"] == ["approved"]
    assert emitted["q"] == ["live set"]
    assert emitted["page_size"] == ["50"]
    assert emitted["sort"] == ["confidence"]
    assert emitted["order"] == ["desc"]


def test_query_encodes_values_that_would_break_a_hand_built_url() -> None:
    """A search containing URL metacharacters survives the round trip.

    The legacy ``q={{ search_query }}`` interpolation did not encode, so a query containing ``&``
    silently truncated at that character and searched for something the operator never typed.
    """
    hostile = "AC/DC & Friends #1 ?live"
    state = ListViewState(q=hostile)
    assert _parse(state)["q"] == [hostile]
    reparsed = ListViewState.from_request(_FakeRequest(state.query()))
    assert reparsed.q == hostile


def test_omit_drops_exactly_one_parameter() -> None:
    """``omit`` removes the named key entirely -- it does not blank it.

    The search box relies on this: it supplies ``q`` from its own input via ``hx-include``, so the
    URL must not also carry a ``q``. A blanked ``q=`` would still be the FIRST occurrence in
    ``?q=&q=typed`` and Starlette would read the empty one, leaving the box a keystroke behind.
    """
    emitted = _parse(ListViewState(status="approved", q="old"), omit=("q",))
    assert "q" not in emitted
    assert emitted["status"] == ["approved"]


def test_state_is_immutable_and_with_returns_a_copy() -> None:
    """Frozen: a control emitting a URL can never mutate what later controls emit."""
    state = ListViewState()
    derived = state.with_(page=9)
    assert state.page == 1 and derived.page == 9
    with pytest.raises(AttributeError):
        state.page = 4  # type: ignore[misc]


def test_url_joins_path_and_query() -> None:
    assert ListViewState(status="all").url("/s/propose", page=2).startswith("/s/propose?")
