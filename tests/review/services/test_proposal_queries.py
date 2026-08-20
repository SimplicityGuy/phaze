"""Tests for proposal query service — pagination, filtering, stats, status updates."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from phaze.models.file import FileRecord
from phaze.models.proposal import APPROVE_REJECT_FROM, UNDO_FROM, ProposalStatus, RenameProposal
from phaze.routers.proposal_sort import PROPOSE_SORT
from phaze.services import proposal_queries
from phaze.services.proposal_queries import (
    Pagination,
    ProposalEditRefusedError,
    ProposalReviewToken,
    ProposalStaleWriteError,
    ProposalStats,
    ProposalTransitionError,
    bulk_approve_selected_above_confidence,
    bulk_update_status,
    count_pending_above_confidence,
    get_proposal_stats,
    get_proposal_with_file,
    get_proposals_page,
    proposal_review_digest,
    update_proposal_fields,
    update_proposal_status,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _create_proposal(
    session: AsyncSession,
    *,
    original_filename: str = "test_file.mp3",
    proposed_filename: str = "Artist - Track.mp3",
    confidence: float = 0.85,
    status: str = ProposalStatus.PENDING,
    reason: str = "Test reasoning",
) -> RenameProposal:
    """Create a FileRecord + RenameProposal for testing."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{original_filename}",
        original_filename=original_filename,
        current_path=f"/music/{original_filename}",
        file_type="music",
        file_size=1_000_000,
    )
    session.add(file_record)
    await session.flush()

    proposal = RenameProposal(
        id=uuid.uuid4(),
        file_id=file_id,
        proposed_filename=proposed_filename,
        confidence=confidence,
        status=status,
        context_used={"artist": "Test Artist"},
        reason=reason,
    )
    session.add(proposal)
    await session.commit()
    return proposal


# ---------------------------------------------------------------------------
# Pagination dataclass
# ---------------------------------------------------------------------------


class TestPagination:
    def test_total_pages_with_items(self):
        p = Pagination(page=1, page_size=25, total=60)
        assert p.total_pages == 3

    def test_total_pages_empty(self):
        p = Pagination(page=1, page_size=25, total=0)
        assert p.total_pages == 1

    def test_has_prev_first_page(self):
        p = Pagination(page=1, page_size=25, total=60)
        assert p.has_prev is False

    def test_has_prev_later_page(self):
        p = Pagination(page=2, page_size=25, total=60)
        assert p.has_prev is True

    def test_has_next_last_page(self):
        p = Pagination(page=3, page_size=25, total=60)
        assert p.has_next is False

    def test_has_next_not_last(self):
        p = Pagination(page=1, page_size=25, total=60)
        assert p.has_next is True

    def test_start_and_end(self):
        p = Pagination(page=2, page_size=25, total=60)
        assert p.start == 26
        assert p.end == 50

    def test_start_zero_when_empty(self):
        p = Pagination(page=1, page_size=25, total=0)
        assert p.start == 0

    def test_end_clamped_to_total(self):
        p = Pagination(page=3, page_size=25, total=60)
        assert p.end == 60


# ---------------------------------------------------------------------------
# ProposalStats
# ---------------------------------------------------------------------------


class TestProposalStats:
    def test_dataclass_fields(self):
        stats = ProposalStats(total=10, pending=5, approved=3, executed=0, rejected=2, failed=0, avg_confidence=0.75)
        assert stats.total == 10
        assert stats.avg_confidence == 0.75


# ---------------------------------------------------------------------------
# get_proposal_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_proposal_stats_empty(session: AsyncSession) -> None:
    stats = await get_proposal_stats(session)
    assert stats.total == 0
    assert stats.pending == 0
    assert stats.approved == 0
    assert stats.executed == 0
    assert stats.rejected == 0
    assert stats.failed == 0
    assert stats.avg_confidence is None


@pytest.mark.asyncio
async def test_get_proposal_stats_with_data(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="p1.mp3", status=ProposalStatus.PENDING, confidence=0.80)
    await _create_proposal(session, original_filename="p2.mp3", status=ProposalStatus.APPROVED, confidence=0.90)
    await _create_proposal(session, original_filename="p3.mp3", status=ProposalStatus.REJECTED, confidence=0.60)

    stats = await get_proposal_stats(session)
    assert stats.total == 3
    assert stats.pending == 1
    assert stats.approved == 1
    assert stats.executed == 0
    assert stats.rejected == 1
    assert stats.failed == 0
    assert stats.avg_confidence is not None
    assert abs(stats.avg_confidence - 0.7667) < 0.01


@pytest.mark.asyncio
async def test_get_proposal_stats_counts_executed_separately_from_approved(session: AsyncSession) -> None:
    """phaze-te2g3: `executed` is its own term, and `approved` does NOT absorb it.

    ADR-0008 maps the operator state Approved onto persisted `approved` OR `executed`, and the
    amendment this bead made to that ADR records why this query does not: the Execute stage needs
    "still to dispatch", so the union is a per-surface presentation choice, not a query one.
    Both halves are pinned here -- a future change that "conforms" this to the ADR by summing the
    two would break the Execute card's meaning silently.
    """
    await _create_proposal(session, original_filename="e1.mp3", status=ProposalStatus.APPROVED, confidence=0.90)
    await _create_proposal(session, original_filename="e2.mp3", status=ProposalStatus.EXECUTED, confidence=0.90)
    await _create_proposal(session, original_filename="e3.mp3", status=ProposalStatus.EXECUTED, confidence=0.90)

    stats = await get_proposal_stats(session)
    assert stats.approved == 1, "approved must stay persisted-`approved` only"
    assert stats.executed == 2
    assert stats.total == 3


@pytest.mark.asyncio
async def test_get_proposal_stats_terms_account_for_total(session: AsyncSession) -> None:
    """phaze-te2g3 / phaze-5uh4u: no proposal is counted by `total` and by no other term.

    This is the defect both beads exist to close, stated as arithmetic rather than as a field name:
    an executed (then a failed) proposal used to be in `total` and in nothing else. `failed` now
    carries its own term (phaze-5uh4u), so this corpus covers every persisted status and the full
    identity holds with no term missing.
    """
    for name, status in (
        ("t1.mp3", ProposalStatus.PENDING),
        ("t2.mp3", ProposalStatus.APPROVED),
        ("t3.mp3", ProposalStatus.EXECUTED),
        ("t4.mp3", ProposalStatus.REJECTED),
        ("t5.mp3", ProposalStatus.FAILED),
    ):
        await _create_proposal(session, original_filename=name, status=status, confidence=0.90)

    stats = await get_proposal_stats(session)
    assert stats.pending + stats.approved + stats.executed + stats.rejected + stats.failed == stats.total


@pytest.mark.asyncio
async def test_get_proposal_stats_counts_failed_separately(session: AsyncSession) -> None:
    """phaze-5uh4u: `failed` (operator vocabulary: Blocked) is its own aggregate term.

    Closes the gap phaze-te2g3 left open for `executed`: before this, a FAILED proposal inflated
    `total` while matching none of pending/approved/executed/rejected.
    """
    await _create_proposal(session, original_filename="f1.mp3", status=ProposalStatus.FAILED, confidence=0.90)
    await _create_proposal(session, original_filename="f2.mp3", status=ProposalStatus.FAILED, confidence=0.90)
    await _create_proposal(session, original_filename="f3.mp3", status=ProposalStatus.PENDING, confidence=0.90)

    stats = await get_proposal_stats(session)
    assert stats.failed == 2
    assert stats.pending == 1
    assert stats.total == 3


# ---------------------------------------------------------------------------
# get_proposals_page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_proposals_page_default(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="a.mp3")
    proposals, pagination = await get_proposals_page(session)
    assert len(proposals) == 1
    assert pagination.total == 1
    assert pagination.page == 1


@pytest.mark.asyncio
async def test_get_proposals_page_status_filter(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="p.mp3", status=ProposalStatus.PENDING)
    await _create_proposal(session, original_filename="a.mp3", status=ProposalStatus.APPROVED)

    proposals, pagination = await get_proposals_page(session, status="approved")
    assert len(proposals) == 1
    assert pagination.total == 1


@pytest.mark.asyncio
async def test_get_proposals_page_status_all(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="p.mp3", status=ProposalStatus.PENDING)
    await _create_proposal(session, original_filename="a.mp3", status=ProposalStatus.APPROVED)

    proposals, pagination = await get_proposals_page(session, status="all")
    assert len(proposals) == 2
    assert pagination.total == 2


@pytest.mark.asyncio
async def test_get_proposals_page_search(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="coachella_set.mp3", proposed_filename="DJ Shadow - Coachella.mp3")
    await _create_proposal(session, original_filename="other.mp3", proposed_filename="Other Artist.mp3")

    proposals, _pagination = await get_proposals_page(session, status="all", search="coachella")
    assert len(proposals) >= 1
    assert any("Coachella" in p.proposed_filename for p in proposals)


@pytest.mark.asyncio
async def test_get_proposals_page_search_underscore_is_literal(session: AsyncSession) -> None:
    """phaze-0dd2: `_` must match only itself, not "any single character" -- otherwise a search for
    `set_live_2024` also matches the unrelated `set-live-2024` / `setXlive 2024`."""
    await _create_proposal(session, original_filename="set_live_2024.mp3", proposed_filename="Set_Live_2024.mp3")
    await _create_proposal(session, original_filename="set-live-2024.mp3", proposed_filename="Set-Live-2024.mp3")
    await _create_proposal(session, original_filename="setXlive2024.mp3", proposed_filename="SetXLive2024.mp3")

    proposals, pagination = await get_proposals_page(session, status="all", search="set_live_2024")
    assert pagination.total == 1
    assert len(proposals) == 1
    assert proposals[0].file.original_filename == "set_live_2024.mp3"


@pytest.mark.asyncio
async def test_get_proposals_page_search_percent_is_literal(session: AsyncSession) -> None:
    """phaze-0dd2: `%` must match only itself, not "zero or more characters"."""
    await _create_proposal(session, original_filename="50pct.mp3", proposed_filename="50% Off Mix.mp3")
    await _create_proposal(session, original_filename="off.mp3", proposed_filename="Off Mix.mp3")

    proposals, pagination = await get_proposals_page(session, status="all", search="50% Off")
    assert pagination.total == 1
    assert proposals[0].proposed_filename == "50% Off Mix.mp3"


@pytest.mark.asyncio
async def test_get_proposals_page_search_backslash_round_trips(session: AsyncSession) -> None:
    """phaze-0dd2: a literal backslash in the search text must not be silently consumed as the LIKE
    escape character -- searching for `AC\\DC` must find the row whose filename literally is that."""
    await _create_proposal(session, original_filename="ac_dc.mp3", proposed_filename="AC\\DC - Thunder.mp3")
    await _create_proposal(session, original_filename="acdc.mp3", proposed_filename="ACDC - Thunder.mp3")

    proposals, pagination = await get_proposals_page(session, status="all", search="AC\\DC")
    assert pagination.total == 1
    assert proposals[0].proposed_filename == "AC\\DC - Thunder.mp3"


@pytest.mark.asyncio
async def test_get_proposals_page_sort_by_original_filename(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="zzz.mp3", proposed_filename="Z.mp3")
    await _create_proposal(session, original_filename="aaa.mp3", proposed_filename="A.mp3")

    proposals, _ = await get_proposals_page(session, status="all", sort=PROPOSE_SORT.resolve(sort="original_filename", order="asc"))
    assert len(proposals) == 2
    assert proposals[0].file.original_filename == "aaa.mp3"


@pytest.mark.asyncio
async def test_get_proposals_page_sort_by_proposed_filename(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="x.mp3", proposed_filename="Zebra.mp3")
    await _create_proposal(session, original_filename="y.mp3", proposed_filename="Alpha.mp3")

    proposals, _ = await get_proposals_page(session, status="all", sort=PROPOSE_SORT.resolve(sort="proposed_filename", order="asc"))
    assert len(proposals) == 2
    assert proposals[0].proposed_filename == "Alpha.mp3"


@pytest.mark.asyncio
async def test_get_proposals_page_sort_desc(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="low.mp3", confidence=0.3)
    await _create_proposal(session, original_filename="high.mp3", confidence=0.9)

    proposals, _ = await get_proposals_page(session, status="all", sort=PROPOSE_SORT.resolve(sort="confidence", order="desc"))
    assert proposals[0].confidence > proposals[1].confidence


@pytest.mark.asyncio
async def test_get_proposals_page_invalid_sort_falls_back(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="a.mp3")

    proposals, _ = await get_proposals_page(session, status="all", sort=PROPOSE_SORT.resolve(sort="nonexistent"))
    assert len(proposals) == 1  # falls back to confidence sort without error


@pytest.mark.asyncio
async def test_get_proposals_page_pagination(session: AsyncSession) -> None:
    for i in range(5):
        await _create_proposal(session, original_filename=f"f{i}.mp3", proposed_filename=f"P{i}.mp3")

    proposals, pagination = await get_proposals_page(session, status="all", page=2, page_size=2)
    assert len(proposals) == 2
    assert pagination.page == 2
    assert pagination.total == 5
    assert pagination.total_pages == 3


@pytest.mark.asyncio
async def test_get_proposals_page_clamps_out_of_range_page_to_the_last_page(session: AsyncSession) -> None:
    """phaze-33sx: a page past the end renders the LAST real page instead of an empty one.

    30 proposals at page_size=50 -> total_pages=1. Requesting page=5 (e.g. a bookmarked/forward
    navigated URL, or a bulk approve/reject on the last page shrinking the pending set out from under
    it) must clamp to page=1 and return its rows, not OFFSET past the end into an empty result with
    an inverted ``Pagination.start > Pagination.end`` range.
    """
    for i in range(30):
        await _create_proposal(session, original_filename=f"g{i}.mp3", proposed_filename=f"G{i}.mp3")

    proposals, pagination = await get_proposals_page(session, status="all", page=5, page_size=50)
    assert pagination.page == 1, "the clamped page must be reported, not the requested out-of-range one"
    assert len(proposals) == 30
    assert pagination.total == 30
    assert pagination.start <= pagination.end, "start must never exceed end"
    assert pagination.start == 1
    assert pagination.end == 30


@pytest.mark.asyncio
async def test_get_proposals_page_clamps_to_the_last_page_with_multiple_pages(session: AsyncSession) -> None:
    """The same clamp with more than one real page: page=99 must land on the true last page (3)."""
    for i in range(25):
        await _create_proposal(session, original_filename=f"h{i}.mp3", proposed_filename=f"H{i}.mp3")

    proposals, pagination = await get_proposals_page(session, status="all", page=99, page_size=10)
    assert pagination.page == 3
    assert len(proposals) == 5, "page 3 of 25 rows at page_size=10 holds the 5-row remainder"
    assert pagination.start <= pagination.end


@pytest.mark.asyncio
async def test_get_proposals_page_clamps_to_page_one_when_the_corpus_is_empty(session: AsyncSession) -> None:
    """total=0 -> total_pages is 1 (Pagination's own convention); page=7 clamps down to it."""
    proposals, pagination = await get_proposals_page(session, status="all", page=7, page_size=25)
    assert pagination.page == 1
    assert proposals == []
    assert pagination.start == 0
    assert pagination.end == 0


# ---------------------------------------------------------------------------
# update_proposal_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_proposal_status_approve(session: AsyncSession) -> None:
    proposal = await _create_proposal(session)
    result = await update_proposal_status(session, proposal.id, ProposalStatus.APPROVED)
    assert result is not None
    assert result.status == ProposalStatus.APPROVED
    assert result.file is not None


@pytest.mark.asyncio
async def test_update_proposal_status_reject(session: AsyncSession) -> None:
    """Rejecting a proposal writes proposals.status = REJECTED (no FileRecord.state cascade)."""
    proposal = await _create_proposal(session)
    result = await update_proposal_status(session, proposal.id, ProposalStatus.REJECTED)
    assert result is not None
    assert result.status == ProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_update_proposal_status_not_found(session: AsyncSession) -> None:
    result = await update_proposal_status(session, uuid.uuid4(), ProposalStatus.APPROVED)
    assert result is None


# ---------------------------------------------------------------------------
# update_proposal_status allowed_from guard (phaze-upnj)
#
# The guard is now folded INTO the UPDATE's WHERE clause (atomic conditional
# write), not a Python check on a prior unlocked SELECT. These assert the
# observable contract that closes the TOCTOU: a row outside the allowed set is
# refused and left untouched, rather than silently overwritten.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_proposal_status_guard_refuses_terminal_row(session: AsyncSession) -> None:
    """An Undo (-> PENDING) against a terminal EXECUTED row is refused and does NOT overwrite it."""
    proposal = await _create_proposal(session, status=ProposalStatus.EXECUTED)
    with pytest.raises(ProposalTransitionError):
        await update_proposal_status(session, proposal.id, ProposalStatus.PENDING, allowed_from=UNDO_FROM)
    # The terminal record survived — the write was conditional, not a blind ORM mutate.
    refetched = await session.get(RenameProposal, proposal.id)
    assert refetched is not None
    assert refetched.status == ProposalStatus.EXECUTED


@pytest.mark.asyncio
async def test_update_proposal_status_guard_allows_legal_from_state(session: AsyncSession) -> None:
    """Undo from an allowed from-state (APPROVED) succeeds under the guard."""
    proposal = await _create_proposal(session, status=ProposalStatus.APPROVED)
    result = await update_proposal_status(session, proposal.id, ProposalStatus.PENDING, allowed_from=UNDO_FROM)
    assert result is not None
    assert result.status == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_update_proposal_status_guard_not_found_returns_none(session: AsyncSession) -> None:
    """A guarded update on a missing id is 404 (None), not a spurious transition error."""
    result = await update_proposal_status(session, uuid.uuid4(), ProposalStatus.PENDING, allowed_from=UNDO_FROM)
    assert result is None


# ---------------------------------------------------------------------------
# update_proposal_status expected_updated_at guard (phaze-exivg)
#
# store_proposals is an idempotent upsert that overwrites a still-PENDING row's content in place
# (same id, same status, new proposed_filename/proposed_path/confidence) on a replayed
# generate_proposals. The status-only allowed_from guard does not see that: it matches this
# swapped-in row exactly the same as the one the operator actually reviewed. These assert the
# expected_updated_at token closes that gap by folding INTO the same conditional UPDATE the
# status guard already uses (phaze-upnj's one-statement pattern), mirroring how phaze-p35v closed
# the confidence half of the bulk-approve predicate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_proposal_status_stale_token_refused(session: AsyncSession) -> None:
    """A token minted before a concurrent content-swap is refused, and the swapped row survives
    untouched -- the approve never silently commits content nobody reviewed.
    """
    proposal = await _create_proposal(session, proposed_filename="Artist - Track A.mp3")
    stale_token = proposal.updated_at

    # Mirrors store_proposals' upsert: same id, same PENDING status, new proposed_filename, and a
    # bumped updated_at (store_proposals' on_conflict_do_update set_ writes updated_at=func.now()
    # explicitly; bumping it explicitly here too, rather than relying on the mixin's
    # onupdate=func.now(), because Postgres' now() is constant for the whole surrounding
    # transaction -- this test's own commit and the swap's commit would otherwise land in the SAME
    # transaction under the test session fixture and read back byte-identical timestamps).
    await session.execute(
        update(RenameProposal)
        .where(RenameProposal.id == proposal.id)
        .values(proposed_filename="Artist - Track B (unreviewed).mp3", updated_at=stale_token + timedelta(seconds=5))
    )
    await session.commit()
    await session.refresh(proposal)
    assert proposal.updated_at != stale_token, "the swap must actually advance updated_at for this test to mean anything"

    with pytest.raises(ProposalStaleWriteError):
        await update_proposal_status(
            session,
            proposal.id,
            ProposalStatus.APPROVED,
            allowed_from=APPROVE_REJECT_FROM,
            expected_updated_at=stale_token,
        )

    # The swapped-in, unreviewed content was never approved.
    refetched = await session.get(RenameProposal, proposal.id)
    assert refetched is not None
    assert refetched.status == ProposalStatus.PENDING
    assert refetched.proposed_filename == "Artist - Track B (unreviewed).mp3"


@pytest.mark.asyncio
async def test_update_proposal_status_current_token_succeeds(session: AsyncSession) -> None:
    """A token that matches the row's live updated_at (the non-race path) approves normally."""
    proposal = await _create_proposal(session)
    result = await update_proposal_status(
        session,
        proposal.id,
        ProposalStatus.APPROVED,
        allowed_from=APPROVE_REJECT_FROM,
        expected_updated_at=proposal.updated_at,
    )
    assert result is not None
    assert result.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_update_proposal_status_no_token_preserves_legacy_behavior(session: AsyncSession) -> None:
    """expected_updated_at=None (the default) skips the check entirely -- every pre-phaze-exivg
    caller keeps its exact prior behavior, including approving right after a concurrent swap.
    """
    proposal = await _create_proposal(session)
    proposal.proposed_filename = "Artist - Track B (unreviewed).mp3"
    await session.commit()

    result = await update_proposal_status(session, proposal.id, ProposalStatus.APPROVED, allowed_from=APPROVE_REJECT_FROM)
    assert result is not None
    assert result.status == ProposalStatus.APPROVED


# ---------------------------------------------------------------------------
# bulk_update_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_update_status(session: AsyncSession) -> None:
    p1 = await _create_proposal(session, original_filename="b1.mp3")
    p2 = await _create_proposal(session, original_filename="b2.mp3")

    count = await bulk_update_status(session, [p1.id, p2.id], ProposalStatus.REJECTED)
    assert count == 2

    updated1 = await session.get(RenameProposal, p1.id)
    assert updated1 is not None
    assert updated1.status == ProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_bulk_update_status_approve(session: AsyncSession) -> None:
    """Bulk approve writes proposals.status = APPROVED for all ids (no FileRecord.state cascade)."""
    p1 = await _create_proposal(session, original_filename="bulk1.mp3")
    p2 = await _create_proposal(session, original_filename="bulk2.mp3")

    count = await bulk_update_status(session, [p1.id, p2.id], ProposalStatus.APPROVED)
    assert count == 2

    for pid in (p1.id, p2.id):
        updated = await session.get(RenameProposal, pid)
        assert updated is not None
        assert updated.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_bulk_update_status_empty_list(session: AsyncSession) -> None:
    count = await bulk_update_status(session, [], ProposalStatus.APPROVED)
    assert count == 0


# ---------------------------------------------------------------------------
# bulk_update_status allowed_from guard (phaze-bg4w)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_update_status_allowed_from_skips_rejected(session: AsyncSession) -> None:
    """A stale id whose row was REJECTED between the SELECT and the UPDATE is NOT flipped to
    APPROVED (the phaze-bg4w TOCTOU shape).

    phaze-7tiqp: the guard was introduced for ``approve_pending_above_confidence``, retired with the
    routeless bulk-approve-high-confidence chain. It still holds the same line for the live
    selection-driven caller, ``bulk_action``'s reject branch, so the case is kept and re-attributed
    rather than deleted alongside it.
    """
    proposal = await _create_proposal(session, original_filename="bg4w.mp3", confidence=0.95)
    # Simulate the concurrent reject that landed after the id was snapshotted.
    await bulk_update_status(session, [proposal.id], ProposalStatus.REJECTED, allowed_from=APPROVE_REJECT_FROM)
    # Re-run the approve with the stale id list + the from-state guard the caller now uses.
    applied = await bulk_update_status(session, [proposal.id], ProposalStatus.APPROVED, allowed_from=APPROVE_REJECT_FROM)
    assert applied == 0
    refetched = await session.get(RenameProposal, proposal.id)
    assert refetched is not None
    assert refetched.status == ProposalStatus.REJECTED


# ---------------------------------------------------------------------------
# count_pending_above_confidence (phaze-rw14)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_pending_above_confidence_matches_approve_predicate(session: AsyncSession) -> None:
    """PENDING only, confidence >= threshold, NULL confidence excluded.

    phaze-7tiqp: this used to cross-check the count against what
    ``approve_pending_above_confidence`` actually approved for the same threshold. That function was
    retired with the routeless bulk-approve-high-confidence chain, so the predicate is now asserted
    directly instead of by agreement with a sibling.
    """
    await _create_proposal(session, original_filename="hc1.mp3", confidence=0.95)
    await _create_proposal(session, original_filename="hc2.mp3", confidence=0.91)
    await _create_proposal(session, original_filename="lc.mp3", confidence=0.5)
    await _create_proposal(session, original_filename="null_conf.mp3", confidence=None)
    await _create_proposal(session, original_filename="hc_approved.mp3", confidence=0.99, status=ProposalStatus.APPROVED)

    count = await count_pending_above_confidence(session, threshold=0.9)
    assert count == 2


@pytest.mark.asyncio
async def test_count_pending_above_confidence_zero_when_nothing_matches(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="lc.mp3", confidence=0.2)
    assert await count_pending_above_confidence(session, threshold=0.9) == 0


# ---------------------------------------------------------------------------
# get_proposal_with_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_proposal_with_file(session: AsyncSession) -> None:
    proposal = await _create_proposal(session)
    result = await get_proposal_with_file(session, proposal.id)
    assert result is not None
    assert result.file is not None
    assert result.file.original_filename == "test_file.mp3"


@pytest.mark.asyncio
async def test_get_proposal_with_file_not_found(session: AsyncSession) -> None:
    result = await get_proposal_with_file(session, uuid.uuid4())
    assert result is None


# ---------------------------------------------------------------------------
# update_proposal_fields allowed_from guard (phaze-3tj4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_proposal_fields_refuses_non_pending_row(session: AsyncSession) -> None:
    """An edit against an APPROVED row is refused and does NOT rewrite the reviewed proposed_path."""
    proposal = await _create_proposal(session, status=ProposalStatus.APPROVED)
    with pytest.raises(ProposalEditRefusedError) as exc_info:
        await update_proposal_fields(session, proposal.id, proposed_path="Some/Other/Dir", allowed_from=APPROVE_REJECT_FROM)
    # phaze-3mru: the message names the row's real (single) status, not a nonsensical
    # "approved -> approved" transition invented by echoing the same value into both slots.
    assert str(exc_info.value) == "edit refused: proposal is approved, only PENDING rows are editable"
    assert exc_info.value.current_status == ProposalStatus.APPROVED
    refetched = await session.get(RenameProposal, proposal.id)
    assert refetched is not None
    assert refetched.status == ProposalStatus.APPROVED
    # The approved row's persisted execution input is untouched.
    assert refetched.proposed_path != "Some/Other/Dir"


@pytest.mark.asyncio
async def test_update_proposal_fields_allows_pending_row(session: AsyncSession) -> None:
    """Editing a PENDING proposal persists the new value and keeps the row PENDING."""
    proposal = await _create_proposal(session, status=ProposalStatus.PENDING)
    result = await update_proposal_fields(session, proposal.id, proposed_filename="Edited.mp3", allowed_from=APPROVE_REJECT_FROM)
    assert result is not None
    assert result.status == ProposalStatus.PENDING
    assert result.proposed_filename == "Edited.mp3"


@pytest.mark.asyncio
async def test_update_proposal_fields_not_found_returns_none(session: AsyncSession) -> None:
    result = await update_proposal_fields(session, uuid.uuid4(), proposed_filename="X.mp3", allowed_from=APPROVE_REJECT_FROM)
    assert result is None


# ---------------------------------------------------------------------------
# bulk_approve_selected_above_confidence -- the batched per-row token predicate (phaze-r4am1)
#
# The per-proposal UPDATE loop collapsed into ONE `UPDATE ... FROM (VALUES ...)` round trip. The
# predicate it carries is genuinely per-row -- each id is matched against its OWN optimistic-
# concurrency token -- so the failure mode a batch invites is a naive `WHERE id = ANY(:ids)`, which
# looks identical from the outside until a token goes stale. These pin that down from both sides:
# a stale token must neither fail the batch nor ride along inside it.
# ---------------------------------------------------------------------------


async def _review_token_for(session: AsyncSession, proposal: RenameProposal) -> ProposalReviewToken:
    """Mint the exact token the rendered Changes Review row would carry for ``proposal``."""
    loaded = (
        await session.execute(select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal.id))
    ).scalar_one()
    return ProposalReviewToken(
        proposal_id=loaded.id,
        updated_at=loaded.updated_at,
        content_digest=proposal_review_digest(loaded),
    )


@pytest.mark.asyncio
async def test_bulk_approve_batch_transitions_every_valid_row(session: AsyncSession) -> None:
    """The happy path: a multi-row selection all still valid transitions in full, in one statement."""
    p1 = await _create_proposal(session, original_filename="ba1.mp3", proposed_filename="Artist - One.mp3", confidence=0.95)
    p2 = await _create_proposal(session, original_filename="ba2.mp3", proposed_filename="Artist - Two.mp3", confidence=0.95)
    tokens = [await _review_token_for(session, p1), await _review_token_for(session, p2)]

    assert await bulk_approve_selected_above_confidence(session, tokens) == 2

    for proposal in (p1, p2):
        refetched = await session.get(RenameProposal, proposal.id)
        assert refetched is not None
        assert refetched.status == ProposalStatus.APPROVED

    # A replay of the identical selection matches nothing -- the rows are no longer PENDING.
    assert await bulk_approve_selected_above_confidence(session, tokens) == 0


@pytest.mark.asyncio
async def test_bulk_approve_batch_drops_a_client_supplied_stale_token(session: AsyncSession) -> None:
    """A token minted before a content swap is rejected while its batch-mates still approve."""
    fresh = await _create_proposal(session, original_filename="ba3.mp3", proposed_filename="Artist - Three.mp3", confidence=0.95)
    swapped = await _create_proposal(session, original_filename="ba4.mp3", proposed_filename="Artist - Four.mp3", confidence=0.95)
    fresh_token = await _review_token_for(session, fresh)
    stale_token = await _review_token_for(session, swapped)

    # Mirrors store_proposals' in-place upsert of a still-PENDING row: same id, same status, new
    # content, bumped updated_at. The token the operator holds now describes a row that is gone.
    await session.execute(
        update(RenameProposal)
        .where(RenameProposal.id == swapped.id)
        .values(proposed_filename="Artist - Four (unreviewed).mp3", updated_at=stale_token.updated_at + timedelta(seconds=5))
    )
    await session.commit()

    assert await bulk_approve_selected_above_confidence(session, [fresh_token, stale_token]) == 1

    approved = await session.get(RenameProposal, fresh.id)
    assert approved is not None
    assert approved.status == ProposalStatus.APPROVED
    refused = await session.get(RenameProposal, swapped.id)
    assert refused is not None
    assert refused.status == ProposalStatus.PENDING
    assert refused.proposed_filename == "Artist - Four (unreviewed).mp3"


@pytest.mark.asyncio
async def test_bulk_approve_batch_keeps_the_token_predicate_inside_the_statement(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-row token check survives batching AT THE SQL LEVEL, not just in the Python pre-filter.

    The pre-filter compares the token against the values it read a moment earlier, so a token that
    is already stale when the function starts never reaches the UPDATE at all -- and a batch that
    had dropped the predicate (``WHERE id = ANY(:ids)``) would still pass a test built that way.
    What distinguishes the two is a write that lands INSIDE the read-to-write window, which is the
    TOCTOU case the in-statement predicate exists for.

    So the swap is injected at the one awaited call between the SELECT and the UPDATE. The ORM
    objects the pre-filter reads keep the values they were loaded with (raw ``text()`` does not
    synchronize the identity map), so the pre-filter admits BOTH rows and the batched statement is
    the only thing left standing between the stale token and an unreviewed approval.
    """
    fresh = await _create_proposal(session, original_filename="ba5.mp3", proposed_filename="Artist - Five.mp3", confidence=0.95)
    raced = await _create_proposal(session, original_filename="ba6.mp3", proposed_filename="Artist - Six.mp3", confidence=0.95)
    fresh_token = await _review_token_for(session, fresh)
    raced_token = await _review_token_for(session, raced)

    # The seam helper (defined with the phaze-1i0h6.4 block below) is this same injection point,
    # factored out once the status and confidence guards needed their own races through it.
    await _race_at_the_collision_seam(
        monkeypatch, "UPDATE proposals SET updated_at = updated_at + interval '5 seconds' WHERE id = :id", {"id": raced.id}
    )

    applied = await bulk_approve_selected_above_confidence(session, [fresh_token, raced_token])

    # One bad token neither fails the batch nor sneaks through it.
    assert applied == 1
    approved = await session.get(RenameProposal, fresh.id)
    assert approved is not None
    assert approved.status == ProposalStatus.APPROVED
    refused = await session.get(RenameProposal, raced.id)
    assert refused is not None
    assert refused.status == ProposalStatus.PENDING


# ---------------------------------------------------------------------------
# bulk_approve_selected_above_confidence -- the set-based transition (phaze-1i0h6.4)
#
# The batched shape is only correct if BOTH halves hold: the Python classifier admits exactly the
# eligible rows, and the single statement re-checks every race guard at write time. The tests below
# are split along that seam on purpose.
#
#   * Classifier tests seed a condition BEFORE the call (collision, stale digest, low confidence,
#     duplicate token) and assert the row is skipped.
#   * Arbiter tests inject the change INSIDE the read-to-write window, at the one awaited call
#     between the SELECT and the UPDATE (``get_review_collision_ids``). The ORM objects the
#     classifier reads keep the values they were loaded with -- raw ``text()`` does not synchronize
#     the identity map -- so the classifier admits the row and the statement's own WHERE clause is
#     the only thing left standing. Deleting a WHERE clause from ``_bulk_approve_statement`` makes
#     exactly one of these fail; that is what they are for. Do not "simplify" them into pre-seeded
#     variants of the classifier tests, which would pass against a predicate-free UPDATE.
# ---------------------------------------------------------------------------


async def _live_status(session: AsyncSession, proposal_id: uuid.UUID) -> str:
    """Read a proposal's status straight from the row, bypassing the identity map.

    ``session.get`` would answer from the ORM identity map, which is NOT synchronized by the raw
    ``text()`` writes the race tests below inject -- it reports the value the object was loaded
    with and every "was it refused?" assertion passes vacuously. Reading the column itself is the
    only way to see what the database actually holds.
    """
    return (await session.execute(select(RenameProposal.status).where(RenameProposal.id == proposal_id))).scalar_one()


async def _race_at_the_collision_seam(monkeypatch: pytest.MonkeyPatch, sql: str, params: dict[str, object]) -> None:
    """Run ``sql`` at the one awaited point between the eligibility SELECT and the guarded UPDATE."""
    real_get_review_collision_ids = proposal_queries.get_review_collision_ids

    async def _race_then_delegate(inner_session: AsyncSession) -> set[str]:
        await inner_session.execute(text(sql), params)
        return await real_get_review_collision_ids(inner_session)

    monkeypatch.setattr(proposal_queries, "get_review_collision_ids", _race_then_delegate)


@pytest.mark.asyncio
async def test_bulk_approve_issues_exactly_one_update_for_a_multi_row_batch(
    session: AsyncSession,
    async_engine: AsyncEngine,
) -> None:
    """The acceptance criterion of phaze-1i0h6.4, pinned as a ROUND-TRIP COUNT, not as contents.

    Every other test in this block passes identically whether the transition is one set-based
    statement or a per-row loop -- the outcomes are the same by construction, which is the whole
    point of the batching. This is the only assertion in the suite that would fail if the loop came
    back, so it asserts on the captured SQL rather than on the rows: FIVE eligible proposals, and
    exactly ONE ``UPDATE proposals`` statement reaches the cursor.
    """
    proposals = [
        await _create_proposal(session, original_filename=f"rt{index}.mp3", proposed_filename=f"Artist - RT{index}.mp3", confidence=0.95)
        for index in range(5)
    ]
    tokens = [await _review_token_for(session, proposal) for proposal in proposals]

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        captured.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        applied = await bulk_approve_selected_above_confidence(session, tokens)
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", _capture)

    assert applied == 5
    updates = [stmt for stmt in captured if stmt.lstrip().lower().startswith("update proposals")]
    assert len(updates) == 1, f"expected ONE batched UPDATE, saw {len(updates)}:\n" + "\n---\n".join(updates)
    # ...and it really did carry all five rows, so "one statement" is not "one row updated".
    for proposal in proposals:
        assert await _live_status(session, proposal.id) == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_bulk_approve_counts_only_the_rows_it_transitioned(session: AsyncSession) -> None:
    """The returned count is RETURNING's row set, never the selection size (phaze-uu17 toast contract).

    A three-row selection of which one is already APPROVED must report 2. The router quotes this
    number to the operator as "N approved", and reporting the selection size there would be a
    confident lie about an irreplaceable archive.
    """
    eligible_a = await _create_proposal(session, original_filename="ct1.mp3", proposed_filename="Artist - C1.mp3", confidence=0.95)
    eligible_b = await _create_proposal(session, original_filename="ct2.mp3", proposed_filename="Artist - C2.mp3", confidence=0.95)
    already = await _create_proposal(
        session, original_filename="ct3.mp3", proposed_filename="Artist - C3.mp3", confidence=0.95, status=ProposalStatus.APPROVED
    )
    tokens = [await _review_token_for(session, p) for p in (eligible_a, eligible_b, already)]

    assert await bulk_approve_selected_above_confidence(session, tokens) == 2


@pytest.mark.asyncio
async def test_bulk_approve_returns_zero_for_an_empty_selection(session: AsyncSession) -> None:
    """No tokens at all: nothing is read, nothing is written, the count is 0."""
    assert await bulk_approve_selected_above_confidence(session, []) == 0


@pytest.mark.asyncio
async def test_bulk_approve_returns_zero_when_no_selected_row_is_eligible(session: AsyncSession) -> None:
    """A non-empty selection none of which qualifies still commits and reports 0 -- no UPDATE at all.

    Distinct from the empty-selection case above: the reads happen, the classifier admits nothing,
    and the early return must still leave the transaction closed rather than dangling.
    """
    below = await _create_proposal(session, original_filename="ze1.mp3", proposed_filename="Artist - Z1.mp3", confidence=0.5)
    missing = ProposalReviewToken(proposal_id=uuid.uuid4(), updated_at=below.updated_at, content_digest="not-a-real-digest")

    assert await bulk_approve_selected_above_confidence(session, [await _review_token_for(session, below), missing]) == 0

    assert await _live_status(session, below.id) == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_bulk_approve_takes_the_last_token_submitted_for_a_repeated_id(session: AsyncSession) -> None:
    """A duplicated id collapses to ONE relation row, and the LAST token submitted is authoritative.

    Changes Review keeps selection in browser/history state, so a re-render or back-button replay
    can put one id on the wire twice with different snapshots. Two things must hold and each has its
    own half here: the row is counted ONCE (a duplicated relation row would let one proposal match
    the join twice and double the count the operator is shown), and the winner is the token minted
    LAST -- the snapshot the operator saw most recently.
    """
    proposal = await _create_proposal(session, original_filename="dup1.mp3", proposed_filename="Artist - Dup.mp3", confidence=0.95)
    fresh = await _review_token_for(session, proposal)
    stale = ProposalReviewToken(
        proposal_id=fresh.proposal_id, updated_at=fresh.updated_at - timedelta(seconds=5), content_digest=fresh.content_digest
    )

    # Stale first, fresh last -> the fresh token wins, the row transitions, and it counts ONCE.
    assert await bulk_approve_selected_above_confidence(session, [stale, fresh]) == 1
    assert await _live_status(session, proposal.id) == ProposalStatus.APPROVED

    # ...and the ordering genuinely decides it: fresh first, stale last refuses the same row.
    other = await _create_proposal(session, original_filename="dup2.mp3", proposed_filename="Artist - Dup2.mp3", confidence=0.95)
    other_fresh = await _review_token_for(session, other)
    other_stale = ProposalReviewToken(
        proposal_id=other_fresh.proposal_id,
        updated_at=other_fresh.updated_at - timedelta(seconds=5),
        content_digest=other_fresh.content_digest,
    )

    assert await bulk_approve_selected_above_confidence(session, [other_fresh, other_stale]) == 0
    assert await _live_status(session, other.id) == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_bulk_approve_excludes_a_row_that_collides_at_its_destination(session: AsyncSession) -> None:
    """Colliding rows are excluded, and the collision vetoes only the rows in it.

    Two proposals resolving to the same agent + owning root + destination path would land one
    physical file on top of another, so neither may be authorized. A third, unrelated row in the
    same selection must still approve -- the veto is per-destination, not per-batch.
    """
    left = await _create_proposal(session, original_filename="col1.mp3", proposed_filename="Same.mp3", confidence=0.95)
    right = await _create_proposal(session, original_filename="col2.mp3", proposed_filename="Same.mp3", confidence=0.95)
    clear = await _create_proposal(session, original_filename="col3.mp3", proposed_filename="Different.mp3", confidence=0.95)
    await session.execute(update(RenameProposal).where(RenameProposal.id.in_([left.id, right.id, clear.id])).values(proposed_path="Collide/Here"))
    await session.commit()
    tokens = [await _review_token_for(session, p) for p in (left, right, clear)]

    assert await bulk_approve_selected_above_confidence(session, tokens) == 1

    for proposal in (left, right):
        assert await _live_status(session, proposal.id) == ProposalStatus.PENDING
    assert await _live_status(session, clear.id) == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_bulk_approve_refuses_a_token_whose_content_digest_no_longer_matches(session: AsyncSession) -> None:
    """The digest is checked independently of ``updated_at`` -- a rewrite that kept the timestamp still fails.

    ``updated_at`` alone cannot carry this: the operator authorized a specific rendered filename and
    destination, and the digest is the only thing that binds the approval to those exact values. The
    write here deliberately restores the original ``updated_at`` so the timestamp axis passes and
    only the digest can refuse.
    """
    proposal = await _create_proposal(session, original_filename="dig1.mp3", proposed_filename="Artist - Digest.mp3", confidence=0.95)
    token = await _review_token_for(session, proposal)

    await session.execute(
        update(RenameProposal).where(RenameProposal.id == proposal.id).values(proposed_filename="Artist - Swapped.mp3", updated_at=token.updated_at)
    )
    await session.commit()

    assert await bulk_approve_selected_above_confidence(session, [token]) == 0

    assert await _live_status(session, proposal.id) == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_bulk_approve_refuses_a_row_below_the_confidence_threshold(session: AsyncSession) -> None:
    """A forged or rewritten low-confidence id never transitions, whatever token accompanies it."""
    low = await _create_proposal(session, original_filename="conf1.mp3", proposed_filename="Artist - Low.mp3", confidence=0.5)
    high = await _create_proposal(session, original_filename="conf2.mp3", proposed_filename="Artist - High.mp3", confidence=0.95)
    tokens = [await _review_token_for(session, low), await _review_token_for(session, high)]

    assert await bulk_approve_selected_above_confidence(session, tokens) == 1

    assert await _live_status(session, low.id) == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_bulk_approve_refuses_a_row_rejected_inside_the_read_to_write_window(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The STATUS guard lives in the statement: a concurrent reject landing mid-call is not overwritten.

    This is the arbiter half of the status check. The classifier read the row as PENDING and admits
    it; the reject commits after that read; only ``status = 'pending'`` inside the UPDATE's WHERE
    clause stops the batch from resurrecting a row the operator (or another tab) just rejected.
    Deleting that clause from ``_bulk_approve_statement`` makes this test, and only this test, fail.
    """
    fresh = await _create_proposal(session, original_filename="race1.mp3", proposed_filename="Artist - Race1.mp3", confidence=0.95)
    raced = await _create_proposal(session, original_filename="race2.mp3", proposed_filename="Artist - Race2.mp3", confidence=0.95)
    tokens = [await _review_token_for(session, fresh), await _review_token_for(session, raced)]

    await _race_at_the_collision_seam(monkeypatch, "UPDATE proposals SET status = 'rejected' WHERE id = :id", {"id": raced.id})

    assert await bulk_approve_selected_above_confidence(session, tokens) == 1

    assert await _live_status(session, fresh.id) == ProposalStatus.APPROVED
    assert await _live_status(session, raced.id) == ProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_bulk_approve_refuses_a_row_downgraded_inside_the_read_to_write_window(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CONFIDENCE guard lives in the statement too (phaze-p35v).

    A confidence rewrite that lands after the classifier read the row must not ride along inside the
    batch. Same seam as the reject race above, different predicate: this is what would fail if
    ``confidence >= threshold`` were dropped from the UPDATE as "already checked in Python".
    """
    fresh = await _create_proposal(session, original_filename="race3.mp3", proposed_filename="Artist - Race3.mp3", confidence=0.95)
    raced = await _create_proposal(session, original_filename="race4.mp3", proposed_filename="Artist - Race4.mp3", confidence=0.95)
    tokens = [await _review_token_for(session, fresh), await _review_token_for(session, raced)]

    await _race_at_the_collision_seam(monkeypatch, "UPDATE proposals SET confidence = 0.10 WHERE id = :id", {"id": raced.id})

    assert await bulk_approve_selected_above_confidence(session, tokens) == 1

    assert await _live_status(session, fresh.id) == ProposalStatus.APPROVED
    assert await _live_status(session, raced.id) == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_bulk_approve_commits_nothing_when_the_batched_write_fails(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-or-nothing: a failing statement leaves EVERY row untransitioned, not a partial batch.

    The single commit at the tail is what makes this true, and it is the property a chunked
    rewrite would be most likely to break -- chunk 1 committed, chunk 2 raised. The failure is
    INJECTED because no reachable input can make this particular write fail: PENDING -> APPROVED
    moves a row OUT of the one-pending-per-file partial unique index
    (``uq_proposals_file_id_pending``), so it cannot collide with it the way a revert TO pending
    can (that path is :class:`ProposalPendingConflictError`, on ``update_proposal_status``). What is
    under test is the transaction shape, not a specific constraint, so any DBAPI failure stands in.
    """
    left = await _create_proposal(session, original_filename="rb1.mp3", proposed_filename="Artist - RB1.mp3", confidence=0.95)
    right = await _create_proposal(session, original_filename="rb2.mp3", proposed_filename="Artist - RB2.mp3", confidence=0.95)
    tokens = [await _review_token_for(session, left), await _review_token_for(session, right)]
    # Hold the ids as plain values: the rollback below expires every ORM instance, and reading
    # ``left.id`` afterwards would emit a lazy refresh from sync attribute-access context.
    proposal_ids = [left.id, right.id]

    real_statement = proposal_queries._bulk_approve_statement

    def _sabotage(*args: object, **kwargs: object) -> object:
        real_statement(*args, **kwargs)  # type: ignore[arg-type]
        raise IntegrityError("simulated constraint violation on the batched UPDATE", None, Exception())

    monkeypatch.setattr(proposal_queries, "_bulk_approve_statement", _sabotage)

    with pytest.raises(IntegrityError):
        await bulk_approve_selected_above_confidence(session, tokens)
    await session.rollback()

    for proposal_id in proposal_ids:
        assert await _live_status(session, proposal_id) == ProposalStatus.PENDING
