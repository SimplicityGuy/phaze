"""phaze-tzy6s.12: the Execute preflight manifest -- what a dispatch WOULD do, before it does it.

The defect this suite pins is not a crash. Before .12 the Execute workspace showed four aggregate
counters and guarded an irreversible batch with a native ``hx-confirm`` naming one number, so the
operator committed the product's only byte-moving operation knowing its cardinality and nothing
else: not which operations, not against which agents, not what was excluded, not what was
recoverable. Every assertion here is about that manifest being present, honest, and matching the
dispatch it precedes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.models.proposal import ProposalStatus


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.proposal import RenameProposal


async def _approve(session: AsyncSession, *proposals: RenameProposal) -> None:
    for proposal in proposals:
        proposal.status = ProposalStatus.APPROVED.value
    await session.commit()


@pytest.mark.asyncio
async def test_preflight_groups_approved_work_by_operation_type(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Moves and in-place renames are DIFFERENT operations and are counted separately.

    ADR-0008 puts the filename and destination decision on one RenameProposal, so the split is not a
    second table -- it is ``proposed_path``: empty means "rename where it sits", non-empty means
    "copy to a new directory, verify, delete the original". Those have different blast radii and
    different reversibility, so a manifest that reports a single undifferentiated "12 operations" has
    not told the operator what will happen.
    """
    move_one = await seed_pending_proposal(0.95, original_filename="a.mp3", proposed_filename="A.mp3", proposed_path="Artist/Event")
    move_two = await seed_pending_proposal(0.94, original_filename="b.mp3", proposed_filename="B.mp3", proposed_path="Artist/Other")
    in_place = await seed_pending_proposal(0.93, original_filename="c.mp3", proposed_filename="C.mp3", proposed_path=None)
    await _approve(session, move_one, move_two, in_place)

    body = (await client.get("/s/apply", headers={"HX-Request": "true"})).text

    assert "Move to new destination" in body
    assert "Rename in place" in body
    assert "Will run — 3 operations" in body

    # The two operation types carry DIFFERENT consequences and reversibility -- that difference is
    # the reason they are split, so assert it rather than merely asserting both labels appear.
    assert "Copies the file to its proposed directory, verifies the copy, then deletes the original." in body
    assert "Renames the file where it already sits. The directory is unchanged." in body
    assert "Reversibility:" in body


@pytest.mark.asyncio
async def test_preflight_names_excluded_work_with_a_reason_and_next_action(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Pending and rejected proposals are excluded EXPLICITLY, not by silent omission.

    An operation type absent from a manifest reads as "there is none of it". The operator needs to
    see that the 1 pending and 1 rejected row exist and are deliberately not part of this batch.
    """
    approved = await seed_pending_proposal(0.95, original_filename="ok.mp3", proposed_filename="Ok.mp3", proposed_path="Artist/Event")
    rejected = await seed_pending_proposal(0.40, original_filename="no.mp3", proposed_filename="No.mp3", proposed_path="Artist/Event2")
    await seed_pending_proposal(0.50, original_filename="wait.mp3", proposed_filename="Wait.mp3", proposed_path="Artist/Event3")

    approved.status = ProposalStatus.APPROVED.value
    rejected.status = ProposalStatus.REJECTED.value
    await session.commit()

    body = (await client.get("/s/apply", headers={"HX-Request": "true"})).text

    assert "Will not run" in body
    assert "Still needs review" in body
    assert "Not yet approved. Only approved proposals are executed." in body
    assert "Review them in Changes Review." in body
    assert "Rejected" in body
    assert "Explicitly rejected. These are never executed." in body


@pytest.mark.asyncio
async def test_preflight_states_that_tag_writes_are_not_dispatched_here(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """EXECUTE APPROVED does not flush tag writes, and the manifest says so rather than staying silent.

    ADR-0008 keeps tag authorization on its own append-only TagWriteLog, dispatched from Changes
    Review. An operator who assumes this control runs them is wrong in a way that costs a debugging
    session, so the exclusion is stated with the stage that does own it.

    The scope statement is asserted with an EMPTY tag-write queue on purpose. Counted exclusion rows
    drop at zero (a wall of zeroes buries the row that matters), so if the boundary were expressed
    only as a counted row it would vanish exactly when the operator has least reason to suspect the
    control is narrower than its name. "Which operations does this button run" is not a count.
    """
    approved = await seed_pending_proposal(0.95, original_filename="x.mp3", proposed_filename="X.mp3", proposed_path="Artist/Event")
    await _approve(session, approved)

    body = (await client.get("/s/apply", headers={"HX-Request": "true"})).text

    assert "Not dispatched by this control" in body
    assert "EXECUTE APPROVED runs approved filename and destination changes only." in body
    assert "Tag writes are authorized and dispatched separately (ADR-0008); this control does not run them." in body
    assert "Dispatch them from the Tag Changes section of Changes Review." in body
    assert "Duplicate resolution is its own decision, taken in Duplicates." in body
    assert "Cue sheets are generated artifacts, written on the Cue sheets stage." in body


@pytest.mark.asyncio
async def test_preflight_blocks_the_whole_batch_on_a_destination_conflict(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A collision short-circuits dispatch server-side, so the manifest must present it as BLOCKING.

    ``start_execution`` checks collisions before it groups anything and returns collision_block
    instead of dispatching -- the veto is batch-wide, not per-row. A workspace that offered an
    enabled EXECUTE button here would be promising something the server will refuse.
    """
    first = await seed_pending_proposal(0.95, original_filename="one.mp3", proposed_filename="Same.mp3", proposed_path="Artist/Event")
    second = await seed_pending_proposal(0.95, original_filename="two.mp3", proposed_filename="Same.mp3", proposed_path="Artist/Event")
    await _approve(session, first, second)

    body = (await client.get("/s/apply", headers={"HX-Request": "true"})).text

    assert "Destination conflicts" in body
    assert "blocking" in body
    assert "Execution is blocked for the whole batch, not just the conflicting rows." in body
    assert "Resolve the conflicting destinations in Changes Review, then return here." in body

    # Blocked means genuinely inert: no dialog to open and nothing that can post.
    assert 'id="apply-confirm"' not in body, "a blocked batch must not offer a confirmation dialog"
    assert 'hx-post="/execution/start"' not in body, "a blocked batch must not be dispatchable"


@pytest.mark.asyncio
async def test_preflight_confirmation_carries_the_manifest_not_a_bare_count(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The confirm step is the shared dialog and repeats the consequences and the excluded total.

    This is the criterion that rules out the native prompt: ``window.confirm`` can render one
    unstyled string, so the manifest had nowhere to live. The dialog states what happens to the
    bytes, that originals survive verification, that everything is audited, and how much work it is
    deliberately NOT touching.
    """
    approved = await seed_pending_proposal(0.95, original_filename="y.mp3", proposed_filename="Y.mp3", proposed_path="Artist/Event")
    await seed_pending_proposal(0.50, original_filename="pend.mp3", proposed_filename="Pend.mp3", proposed_path="Artist/Event2")
    await _approve(session, approved)

    body = (await client.get("/s/apply", headers={"HX-Request": "true"})).text

    assert "hx-confirm" not in body
    assert 'id="apply-confirm"' in body
    assert "Execute 1 approved operation?" in body
    assert "Originals survive until verification succeeds" in body
    assert "written to the audit log" in body
    assert "are excluded and will not be touched." in body
