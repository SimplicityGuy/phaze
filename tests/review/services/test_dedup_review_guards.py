"""Guard branches of ``commit_review_plans`` (phaze-tzy6s.19).

Every assertion here is on a REFUSAL. ``commit_review_plans`` is the entry point to a destructive,
irreversible path -- committing a reviewed duplicate plan deletes files -- so each guard below is
the thing standing between a malformed or stale request and data loss. They were the only
uncovered lines in ``services/dedup_review.py`` (42, 50, 57-58, 69), which held the module at
88.64% and failed the 90% per-module floor in ``scripts/coverage_floor.py``, keeping CI red on the
whole phaze-tzy6s epic.

Each test names the condition the guard defends rather than the line number, so a refactor that
moves the guards keeps the tests meaningful.
"""

from typing import Any
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.dedup_review_plan import DedupReviewPlan
from phaze.services import dedup_review
from phaze.services.dedup_review import InvalidDedupReviewPlanError, commit_review_plans


async def _persist_plan(session: AsyncSession, *, member_ids: list[str], group_hash: str = "hash-a") -> DedupReviewPlan:
    """Store one uncommitted plan and return it with its id populated."""
    plan = DedupReviewPlan(
        group_hash=group_hash,
        canonical_file_id=uuid.UUID(member_ids[0]) if _is_uuid(member_ids[0]) else uuid.uuid4(),
        member_ids=member_ids,
    )
    session.add(plan)
    await session.flush()
    return plan


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (TypeError, ValueError):
        return False
    return True


@pytest.mark.asyncio
async def test_commit_refuses_an_empty_plan_id_list(session: AsyncSession) -> None:
    """No plans means nothing was reviewed; committing would be a no-op that reports success."""
    with pytest.raises(InvalidDedupReviewPlanError):
        await commit_review_plans(session, [])


@pytest.mark.asyncio
async def test_commit_refuses_a_repeated_plan_id(session: AsyncSession) -> None:
    """A duplicated id would resolve the same group twice; the second pass acts on already-deleted members."""
    plan = await _persist_plan(session, member_ids=[str(uuid.uuid4()), str(uuid.uuid4())])

    with pytest.raises(InvalidDedupReviewPlanError):
        await commit_review_plans(session, [plan.id, plan.id])


@pytest.mark.asyncio
async def test_commit_refuses_a_plan_whose_group_hash_is_not_the_expected_one(session: AsyncSession) -> None:
    """Single-group callers pin the hash; a mismatch means the plan under the cursor is not the reviewed one."""
    plan = await _persist_plan(session, member_ids=[str(uuid.uuid4()), str(uuid.uuid4())], group_hash="hash-a")

    with pytest.raises(InvalidDedupReviewPlanError):
        await commit_review_plans(session, [plan.id], expected_group_hash="hash-b")


@pytest.mark.asyncio
async def test_commit_refuses_a_plan_whose_stored_member_ids_are_not_uuids(session: AsyncSession) -> None:
    """member_ids is a JSONB string list, so nothing at the column level stops a non-UUID being stored."""
    plan = await _persist_plan(session, member_ids=[str(uuid.uuid4()), "not-a-uuid"])

    with pytest.raises(InvalidDedupReviewPlanError):
        await commit_review_plans(session, [plan.id])


@pytest.mark.asyncio
async def test_commit_refuses_a_partial_resolution(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count guard is what catches resolve_group deleting a different number of files than reviewed.

    Every member except the canonical one should be resolved, so the expected count is
    ``len(reviewed_ids) - 1``. A smaller number means the group moved under the review and only part
    of it was acted on -- exactly the shape that silently leaves duplicates behind, or removes files
    the reviewer never saw. resolve_group is monkeypatched because provoking a genuine partial
    resolution would require racing the very lock this function takes.
    """
    members = [str(uuid.uuid4()) for _ in range(3)]
    plan = await _persist_plan(session, member_ids=members)

    async def _under_resolve(*_args: Any, **_kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
        return 1, []  # reviewed 3 members, so the guard expects 2

    monkeypatch.setattr(dedup_review, "resolve_group", _under_resolve)

    with pytest.raises(InvalidDedupReviewPlanError):
        await commit_review_plans(session, [plan.id])


@pytest.mark.asyncio
async def test_commit_accepts_a_well_formed_plan(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guards must not be so tight that the happy path cannot pass -- a refusal-only suite would not show that."""
    members = [str(uuid.uuid4()) for _ in range(3)]
    plan = await _persist_plan(session, member_ids=members)

    async def _full_resolve(*_args: Any, **_kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
        return 2, [{"id": members[1]}, {"id": members[2]}]

    monkeypatch.setattr(dedup_review, "resolve_group", _full_resolve)

    committed, states, hashes = await commit_review_plans(session, [plan.id])

    assert committed == 1
    assert hashes == ["hash-a"]
    assert len(states) == 2
    assert plan.committed_at is not None, "a committed plan must be stamped so it cannot be replayed"


@pytest.mark.asyncio
async def test_commit_refuses_to_replay_an_already_committed_plan(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The committed_at stamp is the replay guard; without it a resubmitted form deletes twice."""
    members = [str(uuid.uuid4()) for _ in range(2)]
    plan = await _persist_plan(session, member_ids=members)

    async def _full_resolve(*_args: Any, **_kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
        return 1, [{"id": members[1]}]

    monkeypatch.setattr(dedup_review, "resolve_group", _full_resolve)
    await commit_review_plans(session, [plan.id])

    with pytest.raises(InvalidDedupReviewPlanError):
        await commit_review_plans(session, [plan.id])


@pytest.mark.asyncio
async def test_commit_refuses_a_plan_id_that_does_not_exist(session: AsyncSession) -> None:
    """A missing row must refuse rather than silently commit the subset that did load."""
    plan = await _persist_plan(session, member_ids=[str(uuid.uuid4()), str(uuid.uuid4())])

    with pytest.raises(InvalidDedupReviewPlanError):
        await commit_review_plans(session, [plan.id, uuid.uuid4()])
