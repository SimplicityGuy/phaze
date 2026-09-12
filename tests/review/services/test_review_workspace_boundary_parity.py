"""Differential ruler for the review workspace boundary extraction.

The baseline module is the exact ``review.py`` blob at this bead's starting revision.  Each
capability reads the same seeded database state through the baseline and live modules, so moving
queries or row construction behind a facade cannot change row shapes, warning copy, ordering,
pagination, bounded-page honesty, or degrade behavior.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from phaze.models.proposal import ProposalStatus
from phaze.services import review
from tests.review.capabilities.tag_write.test_degrade import _seed_applied_tagwrite_file
from tests.review.services.test_review_refactor_parity import _load_old_review_module


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord
    from phaze.models.proposal import RenameProposal


_BOUNDARY_BASE_SHA = "74a647b5e161a6f70c8d66e2b713d1d988196574"


@pytest.fixture(scope="module")
def baseline_review() -> Any:
    return _load_old_review_module(_BOUNDARY_BASE_SHA)


@pytest.mark.asyncio
async def test_changes_workspace_preserves_rows_warning_copy_order_and_pagination(
    session: AsyncSession,
    baseline_review: Any,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    absent = await seed_pending_proposal(None, original_filename="boundary-absent.mp3")
    absent.proposed_path = None
    absent.reason = "Operator-visible reason."
    low = await seed_pending_proposal(0.4, original_filename="boundary-low.mp3")
    approved = await seed_pending_proposal(0.99, original_filename="boundary-approved.mp3")
    approved.status = ProposalStatus.APPROVED.value
    await session.commit()

    expected = await baseline_review.get_changes_review_page(session, status="not-a-status", page=99, page_size=1)
    actual = await review.get_changes_review_page(session, status="not-a-status", page=99, page_size=1)

    assert actual == expected
    assert actual.pagination.page == 2
    assert actual.rows[0]["id"] == low.id
    absent_page = await review.get_changes_review_page(session, status="needs_review", page=1, page_size=1)
    assert absent_page.rows[0]["id"] == absent.id
    assert absent_page.rows[0]["warnings"] == [
        "Confidence unavailable; individual review required.",
        "No destination change; the file will be renamed in its current directory.",
        "Operator-visible reason.",
    ]


@pytest.mark.asyncio
async def test_tagwrite_workspace_preserves_complete_page_shape(session: AsyncSession, baseline_review: Any) -> None:
    await _seed_applied_tagwrite_file(session)

    expected = await baseline_review.get_tagwrite_review_page(session)
    actual = await review.get_tagwrite_review_page(session)

    assert actual == expected
    assert actual.partial is False


@pytest.mark.asyncio
async def test_dedupe_workspace_preserves_pagination_and_order(
    session: AsyncSession,
    baseline_review: Any,
    seed_duplicate_group: Callable[..., Awaitable[list[FileRecord]]],
) -> None:
    for _ in range(3):
        await seed_duplicate_group(count=2)

    expected = await baseline_review.get_dedupe_groups(session, limit=1, offset=1)
    actual = await review.get_dedupe_groups(session, limit=1, offset=1)

    assert actual == expected
    assert len(actual) == 1


@pytest.mark.asyncio
async def test_cue_workspace_preserves_eligible_then_gated_order(
    session: AsyncSession,
    baseline_review: Any,
    seed_cue_set: Callable[..., Awaitable[tuple[FileRecord, object, object]]],
) -> None:
    await seed_cue_set(eligible=False, original_filename="boundary-gated.mp3")
    await seed_cue_set(eligible=True, original_filename="boundary-eligible.mp3")

    expected = await baseline_review.get_cue_review_cards(session)
    actual = await review.get_cue_review_cards(session)

    assert actual == expected
    assert [card["eligible"] for card in actual] == [True, False]


class _FailingNested:
    async def __aenter__(self) -> None:
        raise RuntimeError("synthetic read failure")

    async def __aexit__(self, *_args: object) -> None:
        return None


class _TransactionProbe:
    def __init__(self) -> None:
        self.begin_nested_calls = 0

    def begin_nested(self) -> _FailingNested:
        self.begin_nested_calls += 1
        return _FailingNested()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader", "kwargs"),
    [
        ("get_changes_review_page", {"status": "all", "page": 1, "page_size": 25}),
        ("get_pending_proposal_rows", {}),
        ("get_proposal_workspace_page", {"status": "all", "search": "", "page": 1, "page_size": 25}),
        ("get_tagwrite_review_page", {}),
        ("get_dedupe_groups", {}),
        ("count_cue_review_candidates", {}),
        ("get_cue_review_cards", {}),
    ],
)
async def test_each_workspace_preserves_nested_transaction_ownership_and_degrade_result(
    baseline_review: Any,
    reader: str,
    kwargs: dict[str, object],
) -> None:
    expected_probe = _TransactionProbe()
    actual_probe = _TransactionProbe()

    expected = await getattr(baseline_review, reader)(expected_probe, **kwargs)
    actual = await getattr(review, reader)(actual_probe, **kwargs)

    assert actual == expected
    assert expected_probe.begin_nested_calls == actual_probe.begin_nested_calls == 1
