"""Propose is preparation-only, and the bulk endpoint's guarantees hold for every caller.

Propose once carried its own bulk bar, and ``PATCH /proposals/bulk`` answered it with a dedicated
response shape (``_propose_bulk_response.html``) chosen by ``HX-Target``. phaze-tzy6s.7 deleted the
bar; ADR-0008 made Changes Review the only surface that authorizes anything; phaze-7tiqp deleted the
branch, which by then was reachable only by a hand-built request. This file is what survived that:
the half of it asserting the deleted response's SHAPE went with the response, and the half asserting
the ENDPOINT's behaviour stayed, because that behaviour is unchanged and still worth pinning.

What remains, and why each one is here:

* **Propose advertises no decision controls.** The premise the rest of the file rests on -- if
  Propose ever regrows a bulk bar, these fail first.
* **Selection fidelity.** The request must act on exactly the ticked rows. Asserting on the rendered
  result alone is not enough: a bulk that acted on the whole page would look identical in a
  one-row-selected test if the page had one row. So the unselected row is asserted UNCHANGED in the
  database, not merely absent from the response.
* **Real counts, not selection size.** Selecting 3 rows of which 1 is pending must report 1.
* **Idempotency under replay.** The endpoint mutates many rows at once, and this repo has a recorded
  double-dispatch bug (phaze-fa2p) and a bulk TOCTOU double-write (phaze-u28m). The second identical
  submission must be a no-op that says so.
* **One response shape.** The route no longer forks on ``HX-Target`` at all -- the property that
  replaced the two shape-and-container tests deleted here.

What is deliberately NOT re-litigated: the from-state guard itself. That is
``tests/review/routers/test_proposals.py``'s ``test_bulk_approve_skips_terminal_rows``, which shares
this endpoint rather than forking it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import update

from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers.shell import CHANGES_LIST_CONTAINER_ID, PROPOSE_LIST_CONTAINER_ID
from phaze.services.proposal_queries import proposal_review_digest


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# The Propose container id, still sent as HX-Target by the tests below ON PURPOSE (phaze-7tiqp).
# It is now an id no live control targets at this endpoint, which is exactly what makes it a useful
# probe: the route must ignore it and answer with the Changes Review body regardless, rather than
# growing a third surface-specific branch the way it twice did before.
_BULK_TARGET = {"HX-Request": "true", "HX-Target": PROPOSE_LIST_CONTAINER_ID}


def _review_token(proposal: RenameProposal) -> str:
    return f"{proposal.id}|{proposal.updated_at.isoformat()}|{proposal_review_digest(proposal)}"


async def _status_of(session: AsyncSession, proposal: RenameProposal) -> ProposalStatus:
    """Read one proposal's status straight from the database, bypassing any render."""
    fresh = await session.get(RenameProposal, proposal.id, populate_existing=True)
    assert fresh is not None
    return fresh.status


# ---------------------------------------------------------------------------
# Endpoint compatibility -- the preparation workspace no longer exposes decision controls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_routes_decisions_to_review_instead_of_rendering_bulk_controls(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Candidates remain visible, but selection and approve/reject actions belong to Review."""
    await seed_pending_proposal(0.9, original_filename="inspect-me.mp3", proposed_filename="Inspect Me.mp3")
    body = (await client.get("/s/propose")).text

    assert "Inspect Me.mp3" in body
    assert 'name="proposal_ids"' not in body
    assert 'aria-label="Select all rows on this page"' not in body
    assert 'hx-patch="/proposals/bulk' not in body
    assert 'href="/s/rename"' in body and "Review changes" in body


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

    response = await client.patch(
        "/proposals/bulk", data={"action": "approve_eligible", "review_tokens": [_review_token(chosen)]}, headers=_BULK_TARGET
    )

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
    review_tokens = [_review_token(pending), *[_review_token(p) for p in done]]
    for proposal in done:
        await session.execute(update(RenameProposal).where(RenameProposal.id == proposal.id).values(status=ProposalStatus.EXECUTED.value))
    await session.commit()

    body = (
        await client.patch(
            "/proposals/bulk?status=all",
            data={"action": "approve_eligible", "review_tokens": review_tokens},
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
async def test_generate_all_remains_distinct_from_review_decisions(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Generate remains a corpus-wide enqueue while decisions route to Review."""
    await seed_pending_proposal(0.9, original_filename="distinct.mp3", proposed_filename="Distinct.mp3")
    body = (await client.get("/s/propose")).text

    assert "litellm jobs" in body, "GENERATE ALL's confirm must still name the enqueue it performs"
    assert "pending files?" in body, "GENERATE ALL's confirm must still quote the corpus-wide pending set"
    assert 'hx-post="/pipeline/proposals"' in body
    assert 'hx-patch="/proposals/bulk' not in body
    assert 'href="/s/rename"' in body


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
    payload = {"action": "approve_eligible", "review_tokens": [_review_token(p) for p in proposals]}

    first = (await client.patch("/proposals/bulk", data=payload, headers=_BULK_TARGET)).text
    second = (await client.patch("/proposals/bulk", data=payload, headers=_BULK_TARGET)).text

    assert "2 proposals approved" in first
    assert "Nothing approved" in second, f"a replay must report zero transitions, got: {second[:400]}"
    for proposal in proposals:
        assert await _status_of(session, proposal) == ProposalStatus.APPROVED, "a replay must not corrupt the first result"


# ---------------------------------------------------------------------------
# One route, one response shape -- whatever HX-Target arrives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-target"),
        pytest.param({"HX-Request": "true", "HX-Target": "proposal-list-container"}, id="legacy-target-phaze-y4s6"),
        pytest.param({"HX-Request": "true", "HX-Target": PROPOSE_LIST_CONTAINER_ID}, id="propose-target-phaze-7tiqp"),
        pytest.param({"HX-Request": "true", "HX-Target": "invented-container"}, id="invented-target"),
    ],
)
async def test_bulk_action_answers_with_the_changes_review_body_whatever_hx_target_arrives(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
    headers: dict[str, str],
) -> None:
    """``bulk_action`` has no ``HX-Target`` fork left, and this is the test that keeps it that way.

    It grew one twice, and both times the branch outlived its surface. phaze-y4s6 removed the legacy
    ``#proposal-list-container`` fork (``proposal_table.html`` / ``pagination.html`` /
    ``bulk_actions.html`` / ``proposal_list.html`` / ``bulk_response.html``), dead since the v7
    cutover. phaze-7tiqp removed the Propose fallthrough (``_propose_bulk_response.html``), dead
    since phaze-tzy6s.7 deleted the Propose bulk bar and ADR-0008 made Changes Review the only
    surface that authorizes anything.

    So every caller now gets the Changes Review list -- the surface that owns this action -- whether
    it names a retired container, an invented one, or nothing at all. The mutation itself is
    unconditional either way; what the parametrization pins is that the RESPONSE does not vary,
    because a shape only one hand-built request can reach is how both dead branches survived.
    """
    await seed_pending_proposal(0.9, original_filename="stays.mp3", proposed_filename="Stays.mp3")
    acted = await seed_pending_proposal(0.9, original_filename="acted.mp3", proposed_filename="Acted.mp3")

    body = (
        await client.patch(
            "/proposals/bulk",
            data={"action": "approve_eligible", "review_tokens": [_review_token(acted)]},
            headers=headers,
        )
    ).text

    assert f'id="{CHANGES_LIST_CONTAINER_ID}"' in body, "every caller gets the Changes Review list body"
    assert "1 proposal approved." in body, "the toast rides the Changes Review body, not a per-surface response"
    assert f'id="{PROPOSE_LIST_CONTAINER_ID}"' not in body, "the retired Propose response shape is back"
    assert 'id="proposals-table"' not in body, "the LEGACY table no longer exists to render"
    assert 'id="stats-bar"' not in body, "the legacy OOB stats fragment no longer exists to render"
