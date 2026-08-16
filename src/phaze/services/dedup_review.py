"""Create and atomically commit durable duplicate review plans."""

from datetime import UTC, datetime
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.dedup_review_plan import DedupReviewPlan
from phaze.services.dedup import StaleDuplicateReviewError, resolve_group


class InvalidDedupReviewPlanError(Exception):
    """A plan is unknown, replayed, mismatched, or otherwise unsafe to commit."""


def create_review_plan(session: AsyncSession, group: dict[str, Any], canonical_id: uuid.UUID) -> DedupReviewPlan:
    """Stage one fully visible group without resolving it."""
    if group.get("truncated"):
        raise InvalidDedupReviewPlanError
    member_ids = [uuid.UUID(str(member["id"])) for member in group["files"]]
    if canonical_id not in member_ids or len(member_ids) <= 1:
        raise InvalidDedupReviewPlanError
    plan = DedupReviewPlan(
        group_hash=group["sha256_hash"],
        canonical_file_id=canonical_id,
        member_ids=[str(member_id) for member_id in sorted(member_ids)],
    )
    session.add(plan)
    return plan


async def commit_review_plans(
    session: AsyncSession,
    plan_ids: list[uuid.UUID],
    *,
    expected_group_hash: str | None = None,
) -> tuple[int, list[dict[str, Any]], list[str]]:
    """Lock, validate, and commit reviewed plans as one transaction."""
    if not plan_ids or len(set(plan_ids)) != len(plan_ids):
        raise InvalidDedupReviewPlanError
    result = await session.execute(
        select(DedupReviewPlan).where(DedupReviewPlan.id.in_(plan_ids)).order_by(DedupReviewPlan.group_hash, DedupReviewPlan.id).with_for_update()
    )
    plans = list(result.scalars().all())
    if len(plans) != len(plan_ids) or any(plan.committed_at is not None for plan in plans):
        raise InvalidDedupReviewPlanError
    if expected_group_hash is not None and (len(plans) != 1 or plans[0].group_hash != expected_group_hash):
        raise InvalidDedupReviewPlanError

    all_states: list[dict[str, Any]] = []
    group_hashes: list[str] = []
    for plan in sorted(plans, key=lambda item: item.group_hash):
        try:
            reviewed_ids = {uuid.UUID(member_id) for member_id in plan.member_ids}
        except (TypeError, ValueError) as exc:
            raise InvalidDedupReviewPlanError from exc
        try:
            count, states = await resolve_group(
                session,
                plan.group_hash,
                plan.canonical_file_id,
                reviewed_member_ids=reviewed_ids,
            )
        except StaleDuplicateReviewError as exc:
            raise InvalidDedupReviewPlanError from exc
        if count != len(reviewed_ids) - 1:
            raise InvalidDedupReviewPlanError
        plan.committed_at = datetime.now(UTC)
        all_states.extend(states)
        group_hashes.append(plan.group_hash)
    await session.flush()
    return len(plans), all_states, group_hashes
