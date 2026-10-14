"""Periodic, read-only junk-review observations (phaze-co6ad), independent of an open admin tab.

Age starts at ``updated_at``: dispatch stamps it when claiming ``approved -> executing``, while
``decided_at`` is the earlier approval and ``created_at`` may precede approval by weeks. This task
never changes a status, enqueues quarantine, or recovers a ledger row. Recovery remains explicit.

A failed read publishes nothing, including no fresh observation timestamp. General form: an
alert must distinguish a measured empty population from a failed or stale observation, rather
than treating absent evidence as zero. The alert checks freshness on the same producer identity.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from phaze.enums.junk_review import JunkReviewStatus
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.telemetry.instruments import set_gauge


async def observe_junk_review_execution(ctx: dict[str, Any]) -> dict[str, int | float]:
    """Publish executing count, oldest age in seconds, and successful observation time. SELECT only.

    One aggregate returns one row regardless of queue depth, using the existing status index.
    Database time and the oldest transition timestamp share one clock and one statement snapshot.
    Publishing zeros on a successful empty read clears the previous sample after completion/reset.
    """
    async with ctx["async_session"]() as session:
        count, oldest, observed_at = (
            await session.execute(
                select(func.count(), func.min(CompanionJunkReview.updated_at), func.now()).where(
                    CompanionJunkReview.status == JunkReviewStatus.EXECUTING
                )
            )
        ).one()
    age = max(0.0, (observed_at - oldest).total_seconds()) if oldest is not None else 0.0
    set_gauge("phaze.junk_review.executing.count", float(count))
    set_gauge("phaze.junk_review.executing.oldest_age", age)
    set_gauge("phaze.junk_review.executing.observed_timestamp", observed_at.timestamp())
    return {"executing": int(count), "oldest_age_seconds": age}
