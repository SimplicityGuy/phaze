"""Bulk approve/reject in the v7 Propose workspace (phaze-a6hm.11).

The human-in-the-loop approval gate is this application's stated core value -- "nothing moves
without review" -- and the v7 shell cutover left it available per-row only, against an archive of
many thousands of files. This is the bead that makes it usable at scale, and every assertion here
is one of the five acceptance criteria or one of the defect classes this repo has already shipped.

What is deliberately NOT re-litigated here: the from-state guard itself and the legacy surface's
view-state round trip. Those are ``tests/review/routers/test_proposals.py``'s
``test_bulk_action_skips_terminal_rows`` and the two phaze-gc5d tests, which must stay green
unchanged -- this bead SHARES their endpoint rather than forking it, so weakening them would
weaken this surface too. What IS tested here is that the propose workspace genuinely inherits
those guarantees, which is a different claim from "the legacy view has them".

Four things get more than the usual scrutiny:

* **Selection fidelity.** The request must carry exactly the ticked rows. Asserting on the rendered
  result alone is not enough: a bulk that acted on the whole page would look identical in a
  one-row-selected test if the page had one row. So the unselected row is asserted UNCHANGED in the
  database, not merely absent from the response.
* **Real counts, not selection size.** Selecting 3 rows of which 1 is pending must report 1.
* **Idempotency under replay.** The endpoint mutates many rows at once, and this repo has a recorded
  double-dispatch bug (phaze-fa2p) and a bulk TOCTOU double-write (phaze-u28m). The second identical
  submission must be a no-op that says so.
* **Duplicate ids** (four on record: gzrd, op6f, 7j50, and the one 5p43 avoided). The bulk response
  is a THIRD producer of the container's contents and must not re-emit the container itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import update

from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers.shell import PROPOSE_LIST_CONTAINER_ID


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_CONTAINER = PROPOSE_LIST_CONTAINER_ID
_BULK_TARGET = {"HX-Request": "true", "HX-Target": _CONTAINER}


async def _status_of(session: AsyncSession, proposal: RenameProposal) -> ProposalStatus:
    """Read one proposal's status straight from the database, bypassing any render."""
    fresh = await session.get(RenameProposal, proposal.id, populate_existing=True)
    assert fresh is not None
    return fresh.status


def _checkbox_for(body: str, proposal: RenameProposal) -> str:
    """Return the rendered checkbox tag for one row.

    Split on the marker attribute rather than substring-searching the whole tag: the class list
    contains ``disabled:opacity-40`` (a Tailwind variant, not a state), so a naive
    ``"disabled" in tag`` reports every checkbox as disabled and the test passes against a UI that
    offers nothing at all.
    """
    return body.split(f'value="{proposal.id}"')[1].split(">")[0]


def _is_locked(checkbox: str) -> bool:
    """True when the checkbox carries the real ``disabled`` attribute (not the Tailwind variant)."""
    return "disabled title=" in checkbox


# ---------------------------------------------------------------------------
# Acceptance 1 -- rows are selectable, and the action hits EXACTLY the selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rows_render_a_selection_checkbox_carrying_the_proposal_id(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Each row carries a ``proposal_ids`` checkbox valued with its own id, plus a select-all."""
    proposal = await seed_pending_proposal(0.9, original_filename="pick-me.mp3", proposed_filename="Pick Me.mp3")
    body = (await client.get("/s/propose")).text

    assert 'name="proposal_ids"' in body, "rows must be selectable"
    assert f'value="{proposal.id}"' in body, "the checkbox must carry the row's own proposal id"
    assert 'aria-label="Select all rows on this page"' in body, "the header select-all must render"


@pytest.mark.asyncio
async def test_bulk_approve_acts_on_exactly_the_selection(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Only the submitted ids transition; an unselected pending row is left strictly untouched.

    The unselected row is checked in the DATABASE rather than by its absence from the response,
    because a bulk that over-reached onto the whole page would still render a plausible-looking
    list. Absence from a rendered pending tab is what an over-reaching bulk looks like too.
    """
    chosen = await seed_pending_proposal(0.9, original_filename="chosen.mp3", proposed_filename="Chosen.mp3")
    spared = await seed_pending_proposal(0.9, original_filename="spared.mp3", proposed_filename="Spared.mp3")

    response = await client.patch("/proposals/bulk", data={"action": "approve", "proposal_ids": [str(chosen.id)]}, headers=_BULK_TARGET)

    assert response.status_code == 200
    assert await _status_of(session, chosen) == ProposalStatus.APPROVED
    assert await _status_of(session, spared) == ProposalStatus.PENDING, "an unselected row must not be touched"


@pytest.mark.asyncio
async def test_bulk_reject_acts_on_exactly_the_selection(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Reject is the mirror of approve -- same selection semantics, opposite terminal status."""
    chosen = await seed_pending_proposal(0.9, original_filename="nope.mp3", proposed_filename="Nope.mp3")
    spared = await seed_pending_proposal(0.9, original_filename="keep.mp3", proposed_filename="Keep.mp3")

    await client.patch("/proposals/bulk", data={"action": "reject", "proposal_ids": [str(chosen.id)]}, headers=_BULK_TARGET)

    assert await _status_of(session, chosen) == ProposalStatus.REJECTED
    assert await _status_of(session, spared) == ProposalStatus.PENDING


# ---------------------------------------------------------------------------
# Acceptance 2 -- the response re-renders the list, on the SAME view it came from
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_response_is_the_propose_list_not_an_empty_or_legacy_body(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The body is #propose-workspace-list's OWN inner content -- rows + bulk bar + pager.

    Two distinct failures are excluded. The phaze-gc5d shape: a response whose body is gated on a
    single ``proposal`` and is therefore EMPTY for a bulk, which swaps emptiness into the container
    and wipes the list. And the wrong-container shape: answering with the LEGACY
    ``proposal_list.html`` body, whose contract is a different set of children -- putting either
    container's contents inside the other is precisely the phaze-7j50 defect the two distinct ids
    exist to prevent.
    """
    await seed_pending_proposal(0.9, original_filename="stays.mp3", proposed_filename="Stays.mp3")
    other = await seed_pending_proposal(0.9, original_filename="acted.mp3", proposed_filename="Acted.mp3")

    body = (await client.patch("/proposals/bulk", data={"action": "approve", "proposal_ids": [str(other.id)]}, headers=_BULK_TARGET)).text

    assert "Stays.mp3" in body, "the surviving row must be re-rendered, not swapped away"
    assert "Proposed name" in body, "the propose table header must be present (this is the propose container's shape)"
    assert "Showing" in body, "the pager lives inside this container and must come back with it"
    assert 'id="proposals-table"' not in body, "the LEGACY table must never be rendered into the propose container"


@pytest.mark.asyncio
async def test_bulk_response_does_not_re_emit_its_own_container(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The third producer obeys the same split as the other two: inner content, never the wrapper.

    This is the recurring shape of all four duplicate-id bugs on record -- a fragment that re-emits
    its own wrapper and nests a copy of itself inside itself, after which later swaps resolve to the
    outer element while a stale inner copy persists.
    """
    proposal = await seed_pending_proposal(0.9, original_filename="dupe.mp3", proposed_filename="Dupe.mp3")

    body = (await client.patch("/proposals/bulk", data={"action": "approve", "proposal_ids": [str(proposal.id)]}, headers=_BULK_TARGET)).text

    assert f'id="{_CONTAINER}"' not in body, "the bulk response must not nest a second list container"
    assert "<html" not in body.lower(), "a fragment must never carry document chrome"
    # The ONE OOB target is the shared toast container, appended to and never redeclared (phaze-gzrd).
    assert 'hx-swap-oob="beforeend:#toast-container"' in body
    assert 'id="toast-container"' not in body


@pytest.mark.asyncio
async def test_bulk_returns_on_the_same_filter_search_and_page_it_was_issued_from(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The phaze-gc5d guarantee, inherited by this surface: no silent reset to page 1 / pending.

    The propose controls carry the view in the URL via ``ListViewState.query()`` rather than as six
    hidden inputs, and the endpoint re-parses it with the same ``from_request`` the GET uses -- so
    this asserts the round trip end to end, on a non-default page AND a non-default filter AND a
    search, which is the combination a per-parameter implementation drops one of.
    """
    # DISTINCT confidences, because the view is sorted by confidence: twelve rows at 0.9 make the
    # ordering a tie and "page 2" a different pair of rows on each query, which would make this test
    # flap for a reason that has nothing to do with what it asserts.
    approved = []
    for i in range(30):
        proposal = await seed_pending_proposal(0.50 + i / 1000, original_filename=f"a6hmbulk-{i:02d}.mp3", proposed_filename=f"A6HM Bulk {i:02d}.mp3")
        approved.append(proposal)
    for proposal in approved:
        await session.execute(update(RenameProposal).where(RenameProposal.id == proposal.id).values(status=ProposalStatus.APPROVED.value))
    await session.commit()

    # Page 2 of the APPROVED tab, searched, 25 per page, ascending confidence -> rows 26-30, i.e.
    # the five highest-confidence seeds. `page_size` must be a member of PAGE_SIZE_CHOICES: an
    # out-of-set value silently falls back to the default (view_state.py), which would make this
    # test assert against a page the URL never actually requested.
    query = "status=approved&q=a6hmbulk&page=2&page_size=25&sort=confidence&order=asc"
    before = (await client.get(f"/s/propose?{query}", headers=_BULK_TARGET)).text
    assert "A6HM Bulk 27.mp3" in before, "the fixture must put row 27 on page 2, or this asserts nothing"

    # Act on a row, from that view. Nothing transitions (approved is not a legal from-state), which
    # is exactly what isolates the VIEW-STATE round trip from the mutation.
    body = (
        await client.patch(
            f"/proposals/bulk?{query}",
            data={"action": "approve", "proposal_ids": [str(approved[0].id)]},
            headers=_BULK_TARGET,
        )
    ).text

    assert "A6HM Bulk 27.mp3" in body, "the response must come back on page 2 of the approved tab, not page 1 of pending"
    assert "A6HM Bulk 00.mp3" not in body, "a reset to page 1 would surface the first page's rows"
    # The controls in the returned list must keep re-emitting the same state, or the NEXT click resets it.
    assert "status=approved" in body and "q=a6hmbulk" in body


# ---------------------------------------------------------------------------
# Acceptance 3 -- the pager reflects POST-action totals (the phaze-7j50 guarantee)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pager_reports_post_action_totals(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """After approving 3 of 6 pending rows, the pending pager says 3 -- not the pre-action 6.

    The pager is re-rendered by the same swap as the rows because it lives INSIDE the container.
    A pager outside it would still read "6" here, which is the stale-count defect 7j50 fixed on the
    legacy view and which this container must not reintroduce.
    """
    proposals = [await seed_pending_proposal(0.9, original_filename=f"pagecount-{i}.mp3", proposed_filename=f"Page Count {i}.mp3") for i in range(6)]

    query = "status=pending&q=pagecount&page=1&page_size=25"
    before = (await client.get(f"/s/propose?{query}", headers=_BULK_TARGET)).text
    assert "of 6" in before

    body = (
        await client.patch(
            f"/proposals/bulk?{query}",
            data={"action": "approve", "proposal_ids": [str(p.id) for p in proposals[:3]]},
            headers=_BULK_TARGET,
        )
    ).text

    assert "of 3" in body, "the pager must report the total AFTER the action"
    assert "of 6" not in body, "a pre-action total here is the phaze-7j50 stale-pager defect"


# ---------------------------------------------------------------------------
# Acceptance 4 -- legal from-states only; the COUNT is real transitions (phaze-uu17)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_rows_in_the_selection_are_skipped_and_the_count_is_honest(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Selecting 1 pending + 2 executed rows approves 1 and SAYS 1 -- never 3.

    The count, not the selection size, is what the operator is told. An EXECUTED row is the
    authoritative record that a rename was applied to an irreplaceable archive; reporting it as
    freshly approved would be a confident lie about work that already happened.
    """
    pending = await seed_pending_proposal(0.9, original_filename="live.mp3", proposed_filename="Live.mp3")
    done = [await seed_pending_proposal(0.9, original_filename=f"done-{i}.mp3", proposed_filename=f"Done {i}.mp3") for i in range(2)]
    for proposal in done:
        await session.execute(update(RenameProposal).where(RenameProposal.id == proposal.id).values(status=ProposalStatus.EXECUTED.value))
    await session.commit()

    body = (
        await client.patch(
            "/proposals/bulk?status=all",
            data={"action": "approve", "proposal_ids": [str(pending.id), *[str(p.id) for p in done]]},
            headers=_BULK_TARGET,
        )
    ).text

    assert await _status_of(session, pending) == ProposalStatus.APPROVED
    for proposal in done:
        assert await _status_of(session, proposal) == ProposalStatus.EXECUTED, "a terminal row must never be rewritten"

    assert "1 proposal approved" in body, f"the toast must report REAL transitions, got: {body[:400]}"
    assert "2 skipped" in body, "the skipped remainder must be stated, not silently dropped"
    assert "3 proposals approved" not in body, "reporting the selection size is the defect this asserts against"


@pytest.mark.asyncio
async def test_terminal_rows_render_a_disabled_checkbox(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The affordance agrees with the guard: a row that cannot transition cannot be ticked.

    Both sides derive from the SAME ``APPROVE_REJECT_FROM``, which is why they cannot drift. This is
    a UI courtesy on top of the server guard, never a replacement for it -- the previous test proves
    the server still skips a terminal row that reaches it anyway.
    """
    executed = await seed_pending_proposal(0.9, original_filename="locked.mp3", proposed_filename="Locked.mp3")
    await session.execute(update(RenameProposal).where(RenameProposal.id == executed.id).values(status=ProposalStatus.EXECUTED.value))
    await session.commit()

    body = (await client.get("/s/propose?status=all", headers=_BULK_TARGET)).text

    assert _is_locked(_checkbox_for(body, executed)), "an already-actioned row must not offer a selectable checkbox"


@pytest.mark.asyncio
async def test_pending_rows_render_an_enabled_checkbox(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The converse of the above -- the guard must not disable everything and call it safe."""
    proposal = await seed_pending_proposal(0.9, original_filename="open.mp3", proposed_filename="Open.mp3")

    body = (await client.get("/s/propose", headers=_BULK_TARGET)).text

    assert not _is_locked(_checkbox_for(body, proposal)), "a pending row must be selectable"


# ---------------------------------------------------------------------------
# Acceptance 5 -- hx-confirm, and the GENERATE-ALL / APPROVE distinction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_controls_carry_hx_confirm_naming_the_operation(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Both state-changing bulk controls confirm, and each confirm says WHICH operation it is."""
    await seed_pending_proposal(0.9, original_filename="confirm.mp3", proposed_filename="Confirm.mp3")
    body = (await client.get("/s/propose")).text

    assert 'hx-confirm="Approve the selected proposals?' in body
    assert 'hx-confirm="Reject the selected proposals?' in body


@pytest.mark.asyncio
async def test_approve_and_generate_all_remain_distinguishable(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The two bulk triggers on this page must not be readable as each other.

    GENERATE ALL creates proposals over the whole pending CORPUS and enqueues litellm jobs;
    approve/reject decides on proposals that already exist, over exactly the selection. The v7
    cutover blurred this distinction and beads .2/.9 already fixed GENERATE ALL's confirm to quote
    the corpus rather than the filtered page -- so this asserts BOTH that the generate confirm still
    names its enqueue scope and that the approve confirm does not borrow its "all" phrasing.
    """
    await seed_pending_proposal(0.9, original_filename="distinct.mp3", proposed_filename="Distinct.mp3")
    body = (await client.get("/s/propose")).text

    assert "litellm jobs" in body, "GENERATE ALL's confirm must still name the enqueue it performs"
    assert "pending files?" in body, "GENERATE ALL's confirm must still quote the corpus-wide pending set"
    approve_confirm = body.split('hx-confirm="Approve the selected proposals?')[1].split('"')[0]
    assert "all" not in approve_confirm.lower(), "the approve confirm must not claim a corpus-wide scope"
    assert "litellm" not in approve_confirm, "approving proposals enqueues nothing"


# ---------------------------------------------------------------------------
# Concurrency: double-click / replay (phaze-fa2p double-dispatch, phaze-u28m TOCTOU)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replaying_the_same_bulk_submission_is_an_honest_no_op(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A double-click (or a concurrent submit that lost the race) transitions nothing the second time.

    Idempotency here is STRUCTURAL, not a lock and not a client-side guard: ``allowed_from`` is
    evaluated inside the UPDATE's own WHERE clause, so after the first submission the rows are no
    longer PENDING and the replay matches zero. The client-side ``hx-disabled-elt`` narrows the
    window but is explicitly not what this test depends on -- it bypasses the browser entirely.
    """
    proposals = [await seed_pending_proposal(0.9, original_filename=f"twice-{i}.mp3", proposed_filename=f"Twice {i}.mp3") for i in range(2)]
    payload = {"action": "approve", "proposal_ids": [str(p.id) for p in proposals]}

    first = (await client.patch("/proposals/bulk", data=payload, headers=_BULK_TARGET)).text
    second = (await client.patch("/proposals/bulk", data=payload, headers=_BULK_TARGET)).text

    assert "2 proposals approved" in first
    assert "Nothing approved" in second, f"a replay must report zero transitions, got: {second[:400]}"
    for proposal in proposals:
        assert await _status_of(session, proposal) == ProposalStatus.APPROVED, "a replay must not corrupt the first result"


# ---------------------------------------------------------------------------
# The legacy surface is unaffected by sharing the endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_action_ignores_hx_target_and_always_returns_the_propose_body(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """phaze-y4s6: the legacy ``#proposal-list-container`` fork is gone; every caller gets the propose body.

    ``bulk_action`` used to fork the response shape on ``HX-Target`` -- the legacy
    ``#proposal-list-container`` surface (``proposal_table.html``/``pagination.html``/
    ``bulk_actions.html``/``proposal_list.html``/``bulk_response.html``) got one shape, the v7
    propose workspace another. That legacy surface had no live caller left post-v7-cutover and was
    deleted outright, so the endpoint now serves the v7 propose body UNCONDITIONALLY -- even for a
    request that still carries the old legacy ``HX-Target`` (e.g. a stale client) or none at all.
    """
    await seed_pending_proposal(0.9, original_filename="stays.mp3", proposed_filename="Stays.mp3")
    acted = await seed_pending_proposal(0.9, original_filename="legacy.mp3", proposed_filename="Legacy.mp3")

    body = (
        await client.patch(
            "/proposals/bulk",
            data={"action": "approve", "proposal_ids": [str(acted.id)]},
            headers={"HX-Request": "true", "HX-Target": "proposal-list-container"},
        )
    ).text

    assert "Stays.mp3" in body, "the surviving pending row must still be re-rendered"
    assert "Proposed name" in body, "the propose table header must be present (this is the propose container's shape)"
    assert 'id="proposals-table"' not in body, "the LEGACY table no longer exists to render"
    assert 'id="stats-bar"' not in body, "the legacy OOB stats fragment no longer exists to render"
