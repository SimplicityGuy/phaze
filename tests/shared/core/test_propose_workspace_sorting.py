"""Sortable columns in the v7 Propose workspace (phaze-a6hm.10).

The bead is a JOIN, not a feature: ``column_sort`` (phaze-a6hm.1) already owned "how a table sorts"
and ``view_state`` (phaze-a6hm.2 / .9) already carried ``sort``/``order`` through every control. So
the thing most worth testing here is not that sorting works -- it is that it works THROUGH those two
contracts and not beside them. The acceptance criterion says "with no bespoke second implementation",
and a test suite that only checked row order would pass just as happily against a private ``if``
ladder in the query layer.

Hence three groups of assertions that are really about STRUCTURE:

* **One whitelist.** ``get_proposals_page`` used to hold its own ``valid_sort_columns`` set and its
  own name-to-column ladder. It now holds none, and the tests below reach for
  ``PROPOSAL_SORT_COLUMNS`` as the single enumeration -- if a second one reappears, the label/key
  agreement tests are what fail.
* **One URL spelling.** ``SortState.url_for`` spells header URLs; ``ListViewState.query`` spells
  every other control's. The seam between them (``sort_view_state``) is asserted directly, because a
  drift there is invisible on screen until an operator loses their filter mid-sort.
* **Server-side, whole-corpus ordering.** The defect ``column_sort`` exists to prevent is a sort that
  reorders ONE PAGE and presents it as the ordering of the set. That is only detectable with more
  rows than fit on a page, so the ordering tests below deliberately seed across a page boundary --
  a single-page fixture would pass against client-side sorting.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest

import phaze
from phaze.routers.column_sort import ASCENDING, DESCENDING
from phaze.routers.proposal_sort import LEGACY_PROPOSAL_SORT, PROPOSAL_SORT_COLUMNS, PROPOSE_SORT
from phaze.routers.shell import PROPOSE_LIST_CONTAINER_ID
from phaze.routers.view_state import ListViewState


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.proposal import RenameProposal


_CONTAINER = PROPOSE_LIST_CONTAINER_ID
_LIST_TARGET = {"HX-Request": "true", "HX-Target": _CONTAINER}

_PROPOSE_LIST_TEMPLATE = Path(phaze.__file__).parent / "templates" / "pipeline" / "partials" / "_propose_list.html"


def _template_columns() -> list[str]:
    """Return the header labels ``_propose_list.html`` actually passes to ``_file_table.html``.

    Parsed out of the template rather than restated here. A hardcoded copy would be a THIRD place
    the labels live, and the failure it is supposed to catch -- someone renames a header and the
    column silently stops sorting, because _file_table.html matches by label STRING -- is precisely
    the failure a stale copy would hide.
    """
    match = re.search(r"\{%\s*set\s+columns\s*=\s*(\[[^\]]*\])\s*%\}", _PROPOSE_LIST_TEMPLATE.read_text())
    assert match is not None, f"could not find the `columns` list in {_PROPOSE_LIST_TEMPLATE}"
    return list(ast.literal_eval(match.group(1)))


async def _seed_across_a_page_boundary(
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
    *,
    count: int = 30,
) -> None:
    """Seed ``count`` proposals (> the 25-row default page) with strictly ordered confidences.

    The size is the whole point: with everything on one page, a client-side sort and a server-side
    sort are indistinguishable, so a fixture that fits would silently stop testing the defect.
    Confidence ascends with the index while the original filename DESCENDS, so a test can tell the
    two sort keys apart rather than accidentally asserting insertion order twice.
    """
    for index in range(count):
        await seed_pending_proposal(
            round(0.10 + index * 0.02, 4),
            original_filename=f"file-{count - index:03d}.mp3",
            proposed_filename=f"Proposed {index:03d}.mp3",
        )
    await session.commit()


# ---------------------------------------------------------------------------
# The contract object itself -- wiring that must fail at import, not on a click
# ---------------------------------------------------------------------------


def test_propose_contract_targets_the_real_list_container() -> None:
    """The sort target is the EXISTING list container, introducing no new id (gzrd / op6f / 7j50).

    ``proposal_sort.py`` cannot import ``PROPOSE_LIST_CONTAINER_ID`` -- ``shell.py`` imports IT, so
    the dependency only runs one way -- which means the selector is written out as a literal there.
    This is the assertion that keeps the literal honest. It also pins the swap SHAPE: ``/s/propose``
    routes a container-targeted request to the list fragment, so a header aiming anywhere else would
    silently start swapping the whole workspace and destroying search focus mid-keystroke.
    """
    assert PROPOSE_SORT.target == f"#{PROPOSE_LIST_CONTAINER_ID}"
    assert PROPOSE_SORT.endpoint == "/s/propose", "a sort must re-request the surface it was clicked on, not a bespoke fragment endpoint"


def test_the_two_proposal_surfaces_share_one_whitelist() -> None:
    """Both contracts enumerate THE SAME columns; only endpoint and target differ.

    This is the "no bespoke second implementation" criterion expressed as an object identity. The v7
    workspace and the legacy list are two mount points on one table, so they may disagree about
    where a click goes -- never about which columns exist or what they order by.
    """
    assert PROPOSE_SORT.columns is PROPOSAL_SORT_COLUMNS
    assert LEGACY_PROPOSAL_SORT.columns is PROPOSAL_SORT_COLUMNS
    assert PROPOSE_SORT.endpoint != LEGACY_PROPOSAL_SORT.endpoint
    assert PROPOSE_SORT.target != LEGACY_PROPOSAL_SORT.target


def test_contract_labels_match_the_template_headers() -> None:
    """Every whitelisted label is a header the template actually renders, and 'Model' is not sortable.

    ``_file_table.html`` matches headers by LABEL STRING, so a rename on either side degrades that
    header to plain text with no error -- the single most likely silent regression in this bead.
    'Model' renders one configured ``settings.llm_model`` for every row, so it is excluded
    deliberately: sorting a constant is an affordance that promises an ordering it cannot deliver.
    """
    rendered = set(_template_columns())
    labels = {column.label for column in PROPOSAL_SORT_COLUMNS}

    assert labels <= rendered, f"whitelisted labels the template no longer renders (these silently stopped sorting): {labels - rendered}"
    assert "Model" not in labels, "Model is one configured value for the whole page; it is not a column and must not offer sorting"
    assert rendered - labels == {"Model"}, f"a header exists with no sort affordance and no stated reason: {rendered - labels - {'Model'}}"


def test_unknown_sort_degrades_and_never_reaches_a_column() -> None:
    """An unwhitelisted key resolves to the default and cannot name a column (column_sort rule 2/3).

    Asserting the resolved KEY rather than a status code is the whole value of this test: a 200 would
    also be returned by an implementation that cheerfully ``getattr``-ed its way to whatever the
    string named. What must hold is that the hostile string is DISCARDED, so the expression that
    reaches SQL is one a developer enumerated at import time.
    """
    keys = {column.key for column in PROPOSAL_SORT_COLUMNS}
    for hostile in ("nonexistent", "id", "file_id", "status", "1; DROP TABLE proposals", "__class__"):
        state = PROPOSE_SORT.resolve(sort=hostile, order="asc")
        assert state.key == PROPOSE_SORT.default_key
        assert state.key in keys
        assert state.order_by(), "the resolved state must still produce a usable ORDER BY"


# ---------------------------------------------------------------------------
# The seam: one URL spelling, and view state that survives a sort
# ---------------------------------------------------------------------------


def test_sort_view_state_carries_filters_but_not_page_sort_or_order() -> None:
    """``sort_view_state`` feeds ``url_for`` exactly the parameters a header must preserve.

    ``page`` is withheld because a re-sort returns to page 1; ``sort``/``order`` are withheld because
    ``url_for`` appends the NEW key and the toggled direction itself, and emitting them twice would
    leave Starlette reading the stale first occurrence.
    """
    view = ListViewState(status="approved", q="coachella", page=4, page_size=50, sort="confidence", order="desc")
    carried = view.sort_view_state()

    assert carried == {"status": "approved", "q": "coachella", "page_size": 50}
    assert "page" not in carried and "sort" not in carried and "order" not in carried


def test_sort_view_state_and_query_never_enumerate_different_parameters() -> None:
    """The seam is DERIVED from the same parameter map the pager's URLs use, so the two cannot drift.

    This is the anti-rot assertion the bead asked for. If a seventh display parameter is added to
    ``ListViewState`` and only ``query`` learns about it, a sort click would start dropping it while
    every other control preserved it -- a bug that shows up as "sorting cleared my filter" long after
    the change that caused it.
    """
    view = ListViewState(status="rejected", q="live set", page=2, page_size=100, sort="proposed_path", order="asc")
    assert set(view.sort_view_state()) == set(view.params()) - {"page", "sort", "order"}
    assert set(view.params()) == set(parse_qs(view.query(), keep_blank_values=True))


def test_header_url_preserves_filter_and_search_and_resets_the_page() -> None:
    """A header click changes the order and NOTHING else (column_sort rule 4).

    The acceptance criterion "sorting preserves filter, search and page state" is asserted on the
    URL the header actually emits, not on a round trip, because that is where the parameters are
    either carried or lost.
    """
    view = ListViewState(status="approved", q="a&b coachella", page=7, page_size=50, sort="confidence", order="asc")
    state = PROPOSE_SORT.resolve(sort=view.sort, order=view.order, view_state=view.sort_view_state())

    parsed = urlparse(state.url_for("File"))
    params = parse_qs(parsed.query, keep_blank_values=True)

    assert parsed.path == "/s/propose"
    assert params["status"] == ["approved"]
    assert params["q"] == ["a&b coachella"], "a search containing & must survive encoding, not truncate the URL"
    assert params["page_size"] == ["50"]
    assert params["sort"] == ["original_filename"]
    assert "page" not in params, "a re-sort must return to page 1 rather than hold an offset into the old ordering"


def test_clicking_the_active_column_toggles_and_another_column_starts_ascending() -> None:
    """Toggle semantics come from the shared contract, so the propose workspace inherits them."""
    ascending = PROPOSE_SORT.resolve(sort="confidence", order=ASCENDING)
    assert parse_qs(urlparse(ascending.url_for("Conf")).query)["order"] == [DESCENDING]

    descending = PROPOSE_SORT.resolve(sort="confidence", order=DESCENDING)
    assert parse_qs(urlparse(descending.url_for("Conf")).query)["order"] == [ASCENDING]

    # A DIFFERENT column starts ascending rather than inheriting 'desc' -- inheriting reads as the
    # table having silently re-sorted itself in a direction the operator never chose.
    assert parse_qs(urlparse(descending.url_for("File")).query)["order"] == [ASCENDING]


# ---------------------------------------------------------------------------
# The rendered workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_renders_sortable_headers_with_aria_sort(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Sortable headers render as buttons announcing their state; the non-sortable one does not.

    ``aria-sort`` is the accessible signal (column_sort rule 5) -- a caret alone conveys the active
    column to sighted users only. The active column announces its direction and the other sortable
    columns announce ``none``, which means "sortable, not currently sorted"; the ABSENCE of the
    attribute on Model means "not sortable at all", and the two must not be confused.
    """
    await seed_pending_proposal(0.9, original_filename="a.mp3", proposed_filename="A.mp3")
    body = (await client.get("/s/propose?sort=confidence&order=asc")).text

    assert 'aria-sort="ascending"' in body, "the active column must announce its direction"
    assert 'aria-sort="none"' in body, "other sortable columns must announce that they are sortable"
    assert "sort=original_filename" in body and "sort=proposed_filename" in body and "sort=proposed_path" in body

    # Model must render as a plain header: no sort link anywhere claims it.
    assert "sort=llm_model" not in body and "sort=model" not in body


@pytest.mark.asyncio
async def test_sort_click_swaps_only_the_list_and_reemits_no_container_id(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A container-targeted sort returns the list's INNER content -- no duplicate id, no lost focus.

    Two defects in one assertion. Re-emitting the container would put its id in the DOM twice (the
    gzrd / op6f / 7j50 shape, four on record). Returning the whole workspace would re-render the
    search input mid-keystroke and destroy focus and the caret -- which is why the header aims at
    the list container and not at the workspace.
    """
    await seed_pending_proposal(0.9, original_filename="a.mp3", proposed_filename="A.mp3")
    fragment = (await client.get("/s/propose?sort=confidence&order=desc", headers=_LIST_TARGET)).text

    assert f'id="{_CONTAINER}"' not in fragment, "the narrow swap must return the container's contents, never the container itself"
    assert 'name="q"' not in fragment, "a sort must not re-emit the search input; that swap would destroy focus mid-word"
    assert 'aria-label="Status filter tabs"' not in fragment, "a sort swaps the list, not the whole workspace"
    assert "A.mp3" in fragment, "the narrow swap must still contain the rows"


@pytest.mark.asyncio
async def test_sorting_orders_the_whole_corpus_not_just_the_visible_page(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Ascending confidence puts the GLOBAL minimum on page 1 (column_sort rule 1).

    THE test for the defect the contract exists to prevent. Thirty rows over a 25-row page means the
    lowest-confidence row and the highest are on different pages, so a browser-side or
    after-the-read sort -- which can only reorder the rows it was handed -- puts the wrong row first
    and this fails. On a single page it would pass against exactly the bug.
    """
    await _seed_across_a_page_boundary(session, seed_pending_proposal)

    ascending = (await client.get("/s/propose?status=all&sort=confidence&order=asc&page_size=25", headers=_LIST_TARGET)).text
    assert "Proposed 000.mp3" in ascending, "the globally lowest-confidence proposal must lead the ascending page"
    assert "Proposed 029.mp3" not in ascending, "the globally highest-confidence proposal belongs on the LAST ascending page"

    descending = (await client.get("/s/propose?status=all&sort=confidence&order=desc&page_size=25", headers=_LIST_TARGET)).text
    assert "Proposed 029.mp3" in descending, "the globally highest-confidence proposal must lead the descending page"
    assert "Proposed 000.mp3" not in descending


@pytest.mark.asyncio
async def test_sorting_by_original_filename_orders_across_the_joined_file_table(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The File column orders by FileRecord.original_filename, which lives across a join.

    Worth its own test because the join used to be applied CONDITIONALLY, keyed off the sort string.
    Now that the ORDER BY is an opaque whitelisted expression, the query can no longer know whether
    it needs the join, so it always joins -- and this is the assertion that the resulting ordering is
    real. The fixture makes filename order the INVERSE of confidence order, so a query that silently
    fell back to the confidence default would fail here rather than coincidentally passing.
    """
    await _seed_across_a_page_boundary(session, seed_pending_proposal)

    body = (await client.get("/s/propose?status=all&sort=original_filename&order=asc&page_size=25", headers=_LIST_TARGET)).text
    assert "file-001.mp3" in body, "the alphabetically first original filename must lead"
    assert "file-030.mp3" not in body, "the alphabetically last original filename belongs on a later page"


@pytest.mark.asyncio
async def test_an_unknown_sort_renders_the_default_order_rather_than_422ing(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A stale bookmark carrying a retired sort key renders the workspace, not an error.

    ``column_sort`` rule 3: this is a display preference on a hot GET render path, re-sent on every
    pager click, and a 422 would blank the whole workspace to punish a request whose worst outcome is
    "you got the default order". Degrading is safe here only BECAUSE the unknown value never reached
    a column -- that half is asserted directly in the resolver test above.
    """
    await seed_pending_proposal(0.9, original_filename="a.mp3", proposed_filename="A.mp3")

    response = await client.get("/s/propose?sort=retired_column&order=sideways")
    assert response.status_code == 200
    assert "A.mp3" in response.text, "the workspace must still render its rows under the default order"
    assert 'aria-sort="ascending"' in response.text, "the default (confidence asc) must be announced as the active order"


@pytest.mark.asyncio
async def test_paging_stays_inside_the_chosen_sort(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A pager click preserves sort/order -- the easy half of rule 4 to forget.

    Without this, Prev/Next drops the operator back into the default order mid-scan. It works
    because the pager builds its URLs from ``ListViewState.query()``, which re-emits the WHOLE state
    including the two sort parameters; no pager-side change was needed for this bead, and this test
    is what keeps that true.
    """
    await _seed_across_a_page_boundary(session, seed_pending_proposal)

    body = (await client.get("/s/propose?status=all&sort=original_filename&order=desc&page_size=25", headers=_LIST_TARGET)).text
    assert "sort=original_filename" in body and "order=desc" in body

    # Page 2 under the same ordering continues where page 1 stopped, rather than restarting in the
    # default confidence order.
    page_two = (await client.get("/s/propose?status=all&sort=original_filename&order=desc&page_size=25&page=2", headers=_LIST_TARGET)).text
    assert "file-005.mp3" in page_two, "page 2 of a descending filename sort must hold the tail of that ordering"
    assert "file-030.mp3" not in page_two, "the first row of the ordering belongs on page 1"


@pytest.mark.asyncio
async def test_sorting_preserves_the_status_filter_and_search_through_a_round_trip(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The acceptance criterion end to end: sort inside a filtered, searched view keeps both.

    The URL-level assertion above proves the header EMITS the parameters; this proves the server
    still HONOURS them on the way back, so the operator who sorts a searched view does not silently
    get the whole corpus reordered under them.
    """
    await seed_pending_proposal(0.9, original_filename="coachella-set.mp3", proposed_filename="Coachella Set.mp3")
    await seed_pending_proposal(0.5, original_filename="other.mp3", proposed_filename="Other.mp3")

    body = (await client.get("/s/propose?status=pending&q=coachella&sort=confidence&order=desc", headers=_LIST_TARGET)).text
    assert "Coachella Set.mp3" in body
    assert "Other.mp3" not in body, "sorting must not widen the search back to the whole corpus"

    # Every header link emitted from this render must carry the search and the filter onward.
    assert "q=coachella" in body and "status=pending" in body
