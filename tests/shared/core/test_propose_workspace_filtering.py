"""Filter tabs, search and pagination in the v7 Propose workspace (phaze-a6hm.2 / phaze-a6hm.9).

The v7 shell cutover replaced the proposals table with ``pipeline/partials/propose_workspace.html``
and dropped status filtering, search, sorting, pagination and bulk approve/reject in the process.
These tests cover the filter/search and pagination halves being restored INTO that workspace (the
sorting half is phaze-a6hm.10; bulk actions are phaze-a6hm.11).

Three things here are worth more than the usual scrutiny, because each is a defect this repo has
already shipped once:

* **The history restore** (``response_shape.py`` rule 2). The filter controls push URLs, so Back
  with the htmx snapshot evicted re-fetches them carrying BOTH ``HX-Request`` and
  ``HX-History-Restore-Request``. htmx ignores ``hx-target`` on a restore and swaps into ``<body>``,
  so answering with a fragment REPLACES THE PAGE with chrome-less markup. The assertions below
  check for the CHROME, not merely a 200 -- a status assertion alone passes against the bug.
* **Duplicate ids** (four on record: gzrd, op6f, 7j50, and the one 5p43 avoided). The narrow swap
  must return the container's inner content and never re-emit the container itself.
* **Post-action totals** (the 7j50 shape). The pager must live inside the swapped container so it
  can never report counts from before the action that re-rendered the rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers.shell import PROPOSE_LIST_CONTAINER_ID
from phaze.services.review import get_proposal_workspace_page


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_CONTAINER = PROPOSE_LIST_CONTAINER_ID
_LIST_TARGET = {"HX-Request": "true", "HX-Target": _CONTAINER}
_RESTORE = {"HX-Request": "true", "HX-History-Restore-Request": "true"}


async def _seed_mixed(session: AsyncSession, seed_pending_proposal: Callable[..., Awaitable[RenameProposal]]) -> None:
    """Seed 3 pending + 2 approved + 1 rejected proposal with distinguishable filenames."""
    pending = [await seed_pending_proposal(0.95, original_filename=f"pending-{i}.mp3", proposed_filename=f"Pending {i}.mp3") for i in range(3)]
    approved = [await seed_pending_proposal(0.8, original_filename=f"approved-{i}.mp3", proposed_filename=f"Approved {i}.mp3") for i in range(2)]
    rejected = await seed_pending_proposal(0.5, original_filename="rejected-0.mp3", proposed_filename="Rejected 0.mp3")
    assert pending  # seeded for the pending tab; referenced so the intent is explicit
    for proposal in approved:
        await session.execute(update(RenameProposal).where(RenameProposal.id == proposal.id).values(status=ProposalStatus.APPROVED.value))
    await session.execute(update(RenameProposal).where(RenameProposal.id == rejected.id).values(status=ProposalStatus.REJECTED.value))
    await session.commit()


# ---------------------------------------------------------------------------
# phaze-a6hm.2 -- filter tabs + search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_renders_filter_tabs_and_search(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The adopted tabs + search box render in the workspace with live counts."""
    await _seed_mixed(session, seed_pending_proposal)
    body = (await client.get("/s/propose")).text

    assert 'aria-label="Status filter tabs"' in body

    # Assert on each tab's DESTINATION rather than its visible word: "All"/"Pending" etc. occur in
    # unrelated chrome all over the shell, so a label-substring check would pass against a page with
    # no tabs at all.
    for value in ("all", "pending", "approved", "rejected"):
        assert f"status={value}" in body, f"the {value} tab must render with its own filter URL"

    assert 'name="q"' in body, "the adopted search box must render"
    assert 'placeholder="Search by filename..."' in body

    # The count badges are corpus-wide, not page-wide: 6 total / 3 pending / 2 approved / 1 rejected.
    badges = [chunk.split("<")[0].strip() for chunk in body.split('rounded-full px-2 py-0.5 ml-1">')[1:]]
    assert badges == ["6", "3", "2", "1"], f"tab counts must be total/pending/approved/rejected, got {badges}"


@pytest.mark.asyncio
async def test_status_filter_selects_the_matching_proposals(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Each tab renders its own status and excludes the others -- the filter actually filters."""
    await _seed_mixed(session, seed_pending_proposal)

    pending = (await client.get("/s/propose?status=pending", headers=_LIST_TARGET)).text
    assert "Pending 0.mp3" in pending
    assert "Approved 0.mp3" not in pending and "Rejected 0.mp3" not in pending

    approved = (await client.get("/s/propose?status=approved", headers=_LIST_TARGET)).text
    assert "Approved 0.mp3" in approved
    assert "Pending 0.mp3" not in approved

    everything = (await client.get("/s/propose?status=all", headers=_LIST_TARGET)).text
    assert "Pending 0.mp3" in everything and "Approved 0.mp3" in everything and "Rejected 0.mp3" in everything


@pytest.mark.asyncio
async def test_search_narrows_within_the_active_filter(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Search matches filenames and composes with -- rather than replaces -- the status filter."""
    await _seed_mixed(session, seed_pending_proposal)

    hits = (await client.get("/s/propose?status=all&q=Pending 1", headers=_LIST_TARGET)).text
    assert "Pending 1.mp3" in hits
    assert "Pending 0.mp3" not in hits and "Approved 0.mp3" not in hits

    # A search that matches only approved rows returns nothing while the pending tab is active:
    # the two predicates are ANDed, not one overriding the other.
    crossed = (await client.get("/s/propose?status=pending&q=Approved", headers=_LIST_TARGET)).text
    assert "Approved 0.mp3" not in crossed


@pytest.mark.asyncio
async def test_filter_and_search_state_survives_a_swap_and_reaches_the_url(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Every control in the swapped fragment re-emits the ACTIVE state, and pushes it to the URL.

    This is the acceptance criterion "filter and search state survives an HTMX swap and is
    reflected in the URL", and it is asserted on the FRAGMENT rather than the full page on purpose:
    the fragment is what replaces the container, so if its controls forgot the current state, the
    very next click would silently reset the operator's view.
    """
    await _seed_mixed(session, seed_pending_proposal)
    fragment = (await client.get("/s/propose?status=approved&q=Approved&page_size=50", headers=_LIST_TARGET)).text

    assert 'hx-push-url="true"' in fragment, "state must reach the address bar"
    assert "status=approved" in fragment, "the pager must carry the active filter"
    assert "q=Approved" in fragment, "the pager must carry the active search"
    assert "page_size=50" in fragment, "the pager must carry the active page size"
    # The pushed URLs are the full-page shell route, never a fragment-only endpoint -- that is what
    # makes the restore below answerable with a whole document.
    assert "/s/propose?" in fragment


@pytest.mark.asyncio
async def test_history_restore_of_a_filtered_url_returns_a_full_document(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """response_shape.py rule 2 -- a restore gets the CHROME, not the fragment it swapped last time.

    The restore carries BOTH ``HX-Request`` and ``HX-History-Restore-Request``, and htmx will swap
    whatever comes back into ``<body>`` while ignoring ``hx-target``. Answering with the list
    fragment would leave an orphaned table with no nav, no header and no way out but a reload --
    the phaze-64uy defect.

    Deliberately asserts on the CHROME. A status-code assertion passes against the bug.
    """
    await _seed_mixed(session, seed_pending_proposal)
    restored = await client.get("/s/propose?status=approved&q=Approved&page=1", headers=_RESTORE)

    assert restored.status_code == 200
    body = restored.text
    assert "<html" in body, "a restore must be a FULL document"
    assert "<head" in body
    assert 'id="stage-workspace"' in body, "the shell chrome must be present"
    # ...and it must still be the FILTERED view, not a reset to the default tab.
    assert "Approved 0.mp3" in body
    assert "Pending 0.mp3" not in body


@pytest.mark.asyncio
async def test_history_restore_wins_even_with_a_list_hx_target(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The restore header dominates HX-Target, not the other way round.

    A restore of a URL originally pushed by a pager click arrives carrying that control's
    ``HX-Target``. The narrow-swap branch must not fire: htmx ignores ``hx-target`` on a restore, so
    honouring it here is exactly how a fragment ends up replacing the document.
    """
    await _seed_mixed(session, seed_pending_proposal)
    body = (await client.get("/s/propose?status=all", headers={**_RESTORE, "HX-Target": _CONTAINER})).text
    assert "<html" in body and 'id="stage-workspace"' in body


# ---------------------------------------------------------------------------
# Shape / id-uniqueness discipline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_container_id_appears_exactly_once_and_never_in_the_fragment(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The narrow swap returns the container's INNER content -- it never re-emits the container.

    This is the four-duplicate-id defect class (gzrd / op6f / 7j50 / the one 5p43 avoided) in its
    most common shape: a fragment that carries its own wrapper nests a second copy of that id inside
    the first on every swap, after which subsequent swaps resolve to the outer element while the
    stale inner copy lingers on screen.
    """
    await _seed_mixed(session, seed_pending_proposal)

    full = (await client.get("/s/propose")).text
    assert full.count(f'id="{_CONTAINER}"') == 1, "the container is declared exactly once"

    fragment = (await client.get("/s/propose", headers=_LIST_TARGET)).text
    assert f'id="{_CONTAINER}"' not in fragment, "the fragment must NOT re-emit its own container"


@pytest.mark.asyncio
async def test_narrow_swap_is_the_list_only_not_the_whole_workspace(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A container-targeted swap omits the workspace chrome; a rail swap keeps it.

    The search box must not be re-rendered by its own keystroke -- doing so destroys focus and caret
    position mid-word on every debounce tick, which is why the two swap shapes exist at all.
    """
    await _seed_mixed(session, seed_pending_proposal)

    narrow = (await client.get("/s/propose", headers=_LIST_TARGET)).text
    assert 'name="q"' not in narrow, "the search input must survive its own swap"
    assert 'aria-label="Status filter tabs"' not in narrow
    assert "GENERATE ALL" not in narrow

    rail = (await client.get("/s/propose", headers={"HX-Request": "true"})).text
    assert 'name="q"' in rail and "GENERATE ALL" in rail
    assert "<html" not in rail, "a rail swap is still a bare fragment (R-5)"


# ---------------------------------------------------------------------------
# phaze-a6hm.9 -- pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_paginates_and_pages_are_disjoint(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The flat list is gone: rows are split across pages and every row stays reachable."""
    for i in range(30):
        await seed_pending_proposal(0.9, original_filename=f"f{i:03d}.mp3", proposed_filename=f"Track {i:03d}.mp3")

    page1 = (await client.get("/s/propose?page_size=25&page=1", headers=_LIST_TARGET)).text
    page2 = (await client.get("/s/propose?page_size=25&page=2", headers=_LIST_TARGET)).text

    assert page1.count("Track ") == 25, "page 1 holds exactly one page of rows"
    assert page2.count("Track ") == 5, "page 2 holds the remainder"
    assert "Showing 1-25 of 30" in page1
    assert "Showing 26-30 of 30" in page2
    assert 'aria-label="Pagination"' in page1


@pytest.mark.asyncio
async def test_page_links_preserve_filter_search_and_sort(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A page change carries the whole view state -- including the sort phaze-a6hm.10 will own.

    ``sort``/``order`` are asserted here even though nothing interprets them yet: the pager is
    responsible for CARRYING them, and .10 layering meaning on top must not require re-testing that
    the pager preserves them.
    """
    for i in range(30):
        await seed_pending_proposal(0.9, original_filename=f"live{i:03d}.mp3", proposed_filename=f"Live {i:03d}.mp3")

    fragment = (await client.get("/s/propose?status=pending&q=Live&page_size=25&sort=confidence&order=desc", headers=_LIST_TARGET)).text

    assert "status=pending" in fragment
    assert "q=Live" in fragment
    assert "sort=confidence" in fragment
    assert "order=desc" in fragment
    assert "page_size=25" in fragment


@pytest.mark.asyncio
async def test_pager_reports_post_action_totals(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The pager sits INSIDE the swapped container, so its counts reflect the current state.

    The 7j50 shape: when the pager rendered outside the container, an action re-rendered the rows
    but left "Showing 1-50 of 312" reporting pre-action totals. Here, mutating the corpus and
    re-requesting the same view yields a pager that has moved with it -- which is the property
    phaze-a6hm.11's bulk approve/reject will depend on.
    """
    proposals = [await seed_pending_proposal(0.9, original_filename=f"p{i:03d}.mp3", proposed_filename=f"P {i:03d}.mp3") for i in range(30)]

    before = (await client.get("/s/propose?status=pending&page_size=25", headers=_LIST_TARGET)).text
    assert "Showing 1-25 of 30" in before

    # Approve 10 -- they leave the pending filter entirely.
    for proposal in proposals[:10]:
        await session.execute(update(RenameProposal).where(RenameProposal.id == proposal.id).values(status=ProposalStatus.APPROVED.value))
    await session.commit()

    after = (await client.get("/s/propose?status=pending&page_size=25", headers=_LIST_TARGET)).text
    assert "Showing 1-20 of 20" in after, "the pager must report the post-action total"
    assert "of 30" not in after, "no stale pre-action count may survive"


@pytest.mark.asyncio
async def test_header_subcount_reports_the_filtered_total_not_the_page(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The workspace header counts the whole filtered set, not the 25 rows on screen.

    ``propose_proposals | length`` was correct only while the list was unpaginated; once paged it
    would report the page size for every backlog at or above it -- a header that stops counting
    exactly when the number starts mattering.
    """
    for i in range(30):
        await seed_pending_proposal(0.9, original_filename=f"s{i:03d}.mp3", proposed_filename=f"S {i:03d}.mp3")

    body = (await client.get("/s/propose?page_size=25")).text
    assert "30 proposals ready" in body
    assert "25 proposals ready" not in body


@pytest.mark.asyncio
async def test_generate_all_confirm_quotes_the_corpus_not_the_filtered_page(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A bulk-enqueue confirm must name the scope the ACTION has, not the rows on screen.

    GENERATE ALL enqueues over the whole pending set regardless of tab, search or page. Quoting the
    visible rows would understate the blast radius -- filtered to Approved, the confirm would have
    promised zero jobs before enqueuing every pending one (paging contract rule 7: enqueue sets are
    never paged).
    """
    await _seed_mixed(session, seed_pending_proposal)
    body = (await client.get("/s/propose?status=approved")).text
    assert "all 3 pending files" in body, "the confirm quotes the pending corpus (3), not the approved page (2)"


@pytest.mark.asyncio
async def test_junk_pagination_params_render_a_sane_view(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A hand-edited or truncated URL renders the default view rather than 500ing.

    ``page_size`` is the one that matters beyond tidiness: honouring ``?page_size=100000`` would
    reintroduce the unbounded read this bead exists to remove.
    """
    await _seed_mixed(session, seed_pending_proposal)
    for query in ("page=banana", "page=-1", "page_size=100000", "page_size=abc", "order=sideways"):
        response = await client.get(f"/s/propose?{query}", headers=_LIST_TARGET)
        assert response.status_code == 200, f"{query} must not error"


@pytest.mark.asyncio
async def test_read_degrades_to_an_empty_page_instead_of_500ing(session: AsyncSession) -> None:
    """A DB failure renders an empty first page, never an exception (the documented contract).

    ``get_proposal_workspace_page`` promises the render path can never 500, which is what lets
    ``_render_stage`` call it with no try/except -- the same degrade-safe discipline as its
    siblings in this module. The zeroed stats matter as much as the empty rows: real tab counts
    above an empty table would read as "23 pending" over "no proposals", a contradiction the
    operator has no way to diagnose.
    """
    with patch("phaze.services.review.get_proposals_page", side_effect=OperationalError("boom", {}, Exception())):
        page = await get_proposal_workspace_page(session, status="pending", search="", page=3, page_size=50, sort="confidence", order="asc")

    assert page.rows == []
    assert page.pagination.total == 0
    assert page.pagination.page_size == 50, "the requested page size is preserved in the degraded pager"
    assert (page.stats.total, page.stats.pending) == (0, 0), "counts must not survive a failed read"


@pytest.mark.asyncio
async def test_workspace_still_renders_when_the_underlying_query_fails(client: AsyncClient) -> None:
    """The degrade reaches the UI: a failing query yields a 200 workspace, not a traceback.

    Patches the DB read BENEATH the degrade-safe wrapper (``get_proposals_page``), not the wrapper
    itself -- patching the wrapper would only prove that a function raising makes its caller raise,
    which is true of every function and tests nothing. Going one level down exercises the real
    ``except`` path and proves the router needs no try/except of its own.
    """
    with patch("phaze.services.review.get_proposals_page", side_effect=OperationalError("boom", {}, Exception())):
        response = await client.get("/s/propose")

    assert response.status_code == 200
    assert 'id="stage-workspace"' in response.text, "the shell chrome still renders"
    assert "0 proposals ready" in response.text


@pytest.mark.asyncio
async def test_empty_search_result_does_not_claim_the_archive_is_done(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """An empty list under a search says so, instead of asserting something false.

    The flat "every discovered file already has a rename proposal" copy was true only while the list
    was unconditionally the pending set. Under a search it tells an operator whose query simply
    missed that their archive is fully proposed.
    """
    await _seed_mixed(session, seed_pending_proposal)
    body = (await client.get("/s/propose?status=all&q=zzz-no-such-file", headers=_LIST_TARGET)).text
    assert "No matches" in body
    assert "already has a rename proposal" not in body
