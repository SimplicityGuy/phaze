"""phaze-tzy6s.12: the Execute preflight manifest -- what a dispatch WOULD do, before it does it.

The defect this suite pins is not a crash. Before .12 the Execute workspace showed four aggregate
counters and guarded an irreversible batch with a native ``hx-confirm`` naming one number, so the
operator committed the product's only byte-moving operation knowing its cardinality and nothing
else: not which operations, not against which agents, not what was excluded, not what was
recoverable. Every assertion here is about that manifest being present, honest, and matching the
dispatch it precedes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup
import pytest

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
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

    ADR-0008 (changes review approval boundary) puts the filename and destination decision on one RenameProposal, so the split is not a
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
    """Pending and rejected proposals remain contextual guidance, outside batch exclusions.

    An operation type absent from a workspace reads as "there is none of it". The operator needs to
    see that the 1 pending and 1 rejected row exist and are deliberately not part of this batch.
    """
    approved = await seed_pending_proposal(0.95, original_filename="ok.mp3", proposed_filename="Ok.mp3", proposed_path="Artist/Event")
    rejected = await seed_pending_proposal(0.40, original_filename="no.mp3", proposed_filename="No.mp3", proposed_path="Artist/Event2")
    await seed_pending_proposal(0.50, original_filename="wait.mp3", proposed_filename="Wait.mp3", proposed_path="Artist/Event3")

    approved.status = ProposalStatus.APPROVED.value
    rejected.status = ProposalStatus.REJECTED.value
    await session.commit()

    body = (await client.get("/s/apply", headers={"HX-Request": "true"})).text

    assert "Will not run" not in body
    assert "Other work outside this approved batch" in body
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

    ADR-0008 (changes review approval boundary) keeps tag authorization on its own append-only TagWriteLog, dispatched from Changes
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
    assert "Tag writes are authorized and dispatched separately; this control does not run them." in body
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
    assert "Batch exceptions — execution blocked" in body
    assert "1 destination" in body
    assert "Will not run — 1 approved operation" not in body

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


# phaze-tzy6s.17 (CR-12-1 / CR-12-2): the preflight's three adjacent counts must not be
# saturated page lengths.
#
# The manifest's own module docstring sets the standard these pin: the *_pending defaults
# exist so a caller "degrades to omitting the row rather than reporting a confident zero it
# did not measure". Reporting a capped read's length as a total is the same error pointed the
# other way -- and it landed on the confirmation dialog, the last thing an operator reads
# before the product's only irreversible operation. Two of the three counts are exactly
# knowable in SQL and are now COUNT queries; the third cannot be (tag-write eligibility is a
# Python predicate) and is therefore labelled a floor instead of being dressed up as a total.


@pytest.mark.asyncio
async def test_duplicate_group_count_is_a_corpus_count_not_the_pager_page_length(
    session: AsyncSession,
    seed_duplicate_group: Callable[..., Awaitable[list[object]]],
) -> None:
    """``count_duplicate_groups`` counts every group; the paged reader is bounded by its ``limit``.

    Asserted as a DIVERGENCE from a deliberately tiny page rather than by seeding 101 groups to
    trip the real ``limit=100`` default. Same defect, three orders of magnitude cheaper: the bug
    was that a page length was being read as a total, so what has to be pinned is that the two
    disagree once the page bounds and the count does not.
    """
    from phaze.services.dedup import count_duplicate_groups, find_duplicate_groups_with_metadata

    for _ in range(3):
        await seed_duplicate_group(count=2)

    assert await count_duplicate_groups(session) == 3

    capped_page = await find_duplicate_groups_with_metadata(session, limit=2)
    assert len(capped_page) == 2, "the paged reader is bounded, which is correct for a page"
    assert await count_duplicate_groups(session) == 3, "...and is exactly why len(page) must not be used as the count"


@pytest.mark.asyncio
async def test_cue_candidate_count_covers_both_halves_without_building_any_cue_text(
    session: AsyncSession,
    seed_cue_set: Callable[..., Awaitable[tuple[object, object, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``count_cue_review_candidates`` counts eligible + gated, and calls no cue generator.

    The generator assertion is the CR-12-2 half and is not incidental: producing this number used
    to run ``generate_cue_content`` for up to ``_MAX_REVIEW_ROWS`` sets and throw every string away.
    Patching it to explode proves the count path never reaches it, which a count-only assertion
    would not.
    """
    import phaze.services.review as review_module
    from phaze.services.review import count_cue_review_candidates

    def _explode(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("counting cue candidates must not generate cue text")

    await seed_cue_set(eligible=True, original_filename="set-a.mp3")
    await seed_cue_set(eligible=True, original_filename="set-b.mp3")
    await seed_cue_set(eligible=False, original_filename="set-c.mp3")

    monkeypatch.setattr(review_module, "generate_cue_content", _explode)

    assert await count_cue_review_candidates(session) == 3


@pytest.mark.asyncio
async def test_a_capped_tagwrite_count_renders_as_a_floor_not_a_total(
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A saturated tag-write scan marks contextual guidance ``at_least``, outside batch totals.

    Tag-write eligibility cannot be counted in SQL, so honesty here means labelling the number
    rather than fixing it. The approved execution batch must not inherit an unrelated queue's flag.
    """
    from phaze.services.execution_preflight import get_execution_preflight

    approved = await seed_pending_proposal(0.95, original_filename="ok.mp3", proposed_filename="Ok.mp3", proposed_path="Artist/Event")
    await _approve(session, approved)

    exact = await get_execution_preflight(session, pending=0, rejected=0, tagwrite_pending=12)
    floored = await get_execution_preflight(session, pending=0, rejected=0, tagwrite_pending=12, tagwrite_pending_at_least=True)

    (exact_row,) = [e for e in exact.related_work if e.key == "tagwrite"]
    (floored_row,) = [e for e in floored.related_work if e.key == "tagwrite"]

    assert exact_row.at_least is False
    assert exact.excluded_total_at_least is False

    assert floored_row.at_least is True, "a capped scan's length is a lower bound, and the row must say so"
    assert floored_row.count == 12, "the flag annotates the number; it does not replace or inflate it"
    assert "capped" in floored_row.reason, "the reason text must explain the +, not just render a symbol"
    assert floored.excluded_total == 0
    assert floored.excluded_total_at_least is False, "adjacent queue floors do not belong to the execution batch"


@pytest.mark.asyncio
async def test_the_execute_workspace_marks_a_floored_exclusion_total_in_the_rendered_page(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """End-to-end: pending proposals appear as contextual guidance rather than batch exclusions.

    A pending row is outside the approved population, so it must not inflate its excluded total.
    """
    approved = await seed_pending_proposal(0.95, original_filename="e.mp3", proposed_filename="E.mp3", proposed_path="Artist/Event")
    await seed_pending_proposal(0.50, original_filename="p.mp3", proposed_filename="P.mp3", proposed_path="Artist/Other")
    await _approve(session, approved)

    response = await client.get("/s/apply")
    assert response.status_code == 200
    body = response.text

    assert "Will not run" not in body
    assert "Other work outside this approved batch" in body
    assert "Will not run — 1+ excluded" not in body, "nothing here is capped, so nothing may be marked as a floor"


@pytest.mark.asyncio
async def test_rendering_execute_never_calls_the_saturating_list_readers(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /s/apply`` must reach neither ``get_dedupe_groups`` nor ``get_cue_review_cards``.

    This is the test that would have caught CR-12-1/CR-12-2 in the first place, and it is phrased
    against the CALL rather than the number on purpose. Asserting a count is correct requires
    seeding past whichever cap you are worried about -- 101 duplicate groups for the ``limit=100``
    pager, ``_MAX_REVIEW_ROWS`` cue sets for the other -- so such a test is expensive, and it silently
    stops testing anything the day a cap is raised. The invariant that actually holds is structural:
    the Execute manifest is built from aggregates, so a saturating list reader has no business on
    this path at all. If one comes back, this fails immediately and at any corpus size.

    Both readers stay correct and in use for their OWN workspaces (``/s/dedupe``, ``/s/cue``); it is
    only Execute, which needs a scalar rather than the rows, that must not call them.
    """
    # phaze-bk9el.16: `_apply_stage_context` moved into the routers/shell package's
    # `stage_context` submodule and resolves these two readers from there.
    import phaze.routers.shell.stage_context as shell_module

    async def _explode_dedupe(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("GET /s/apply must count duplicate groups, not page them")

    async def _explode_cue(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("GET /s/apply must count cue candidates, not build their cards")

    approved = await seed_pending_proposal(0.95, original_filename="s.mp3", proposed_filename="S.mp3", proposed_path="Artist/Event")
    await _approve(session, approved)

    monkeypatch.setattr(shell_module, "get_dedupe_groups", _explode_dedupe)
    monkeypatch.setattr(shell_module, "get_cue_review_cards", _explode_cue)

    response = await client.get("/s/apply")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_empty_execute_is_neutral_and_links_review_despite_duplicate_backlog(
    client: AsyncClient,
    seed_duplicate_group: Callable[..., Awaitable[list[object]]],
) -> None:
    """Unrelated duplicate work cannot turn an empty approved batch into an execution alarm."""
    await seed_duplicate_group(count=2)
    response = await client.get("/s/apply")
    page = BeautifulSoup(response.text, "html.parser")
    empty = page.select_one("[data-execute-empty]")
    assert empty is not None
    assert "No approved operations ready to execute" in empty.get_text()
    assert "text-danger" not in str(empty) and "text-warn" not in str(empty)
    review = empty.select_one("a")
    assert review is not None and review["href"] == "/s/rename?status=needs_review"
    assert review["hx-target"] == "#stage-workspace"
    assert "Execution unavailable" not in response.text
    assert "Will not run" not in response.text
    assert page.select_one("#apply-confirm") is None
    assert page.select_one('[hx-post="/execution/start"]') is None
    guidance = page.select_one("[data-execute-related-work]")
    assert guidance is not None and guidance.select_one('a[href="/s/dedupe"]') is not None
    for label in ("Blocked", "Rejected by review"):
        term = page.find("dt", string=label)
        assert term is not None
        value = term.find_next_sibling("dd")
        assert value is not None and value.get_text(strip=True) == "0"
        assert "text-danger" not in str(value) and "text-warn" not in str(value)


@pytest.mark.asyncio
async def test_partially_excluded_batch_counts_only_approved_revoked_operations(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A live approved operation stays actionable while an approved revoked operation is explicit."""
    from phaze.services.execution_preflight import get_execution_preflight

    live = await seed_pending_proposal(0.95, original_filename="live.mp3", proposed_filename="Live.mp3", proposed_path="Artist/Live")
    revoked = await seed_pending_proposal(0.95, original_filename="revoked.mp3", proposed_filename="Revoked.mp3", proposed_path="Artist/Revoked")
    session.add(Agent(id="revoked-seat", name="Revoked seat", revoked_at=datetime.now(UTC)))
    await session.flush()
    file = await session.get(FileRecord, revoked.file_id)
    assert file is not None
    file.agent_id = "revoked-seat"
    await _approve(session, live, revoked)

    preflight = await get_execution_preflight(session, pending=99, rejected=25, dedupe_pending=1919)
    assert preflight.can_execute is True and preflight.total == 1
    assert [(row.key, row.count) for row in preflight.exclusions] == [("revoked", 1)]
    assert preflight.excluded_total == 1
    assert [(row.key, row.count) for row in preflight.related_work] == [("pending", 99), ("rejected", 25), ("dedupe", 1919)]

    body = (await client.get("/s/apply")).text
    assert "Will run — 1 operation" in body
    assert "Will not run — 1 approved operation excluded" in body
    assert "Owned by a revoked agent" in body
    assert "Re-register the agent" in body
    assert 'id="apply-confirm"' in body
    assert 'hx-post="/execution/start"' in body


@pytest.mark.asyncio
async def test_all_approved_revoked_is_a_real_block_with_agent_recovery(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Zero runnable operations is not an empty approval queue when revocation excludes every row."""
    from phaze.services.execution_preflight import get_execution_preflight

    proposal = await seed_pending_proposal(0.95)
    file = await session.get(FileRecord, proposal.file_id)
    assert file is not None
    agent = await session.get(Agent, file.agent_id)
    assert agent is not None
    agent.revoked_at = datetime.now(UTC)
    await _approve(session, proposal)
    preflight = await get_execution_preflight(session, pending=0, rejected=0)
    assert preflight.total == 0 and preflight.can_execute is False
    assert preflight.excluded_total == 1
    assert preflight.blocked_reason == "All approved operations belong to revoked agents."
    response = await client.get("/s/apply")
    page = BeautifulSoup(response.text, "html.parser")
    assert page.select_one("[data-execute-empty]") is None
    assert "All approved operations belong to revoked agents" in response.text
    assert page.select_one('a[href="/s/agents"]') is not None
    assert page.select_one("#apply-confirm") is None
    assert page.select_one('[hx-post="/execution/start"]') is None
