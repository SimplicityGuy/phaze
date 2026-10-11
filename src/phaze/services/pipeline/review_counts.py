"""Off-request review queue counts, sharing the stage pages' authoritative queries."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from phaze.services.dedup import count_duplicate_groups
from phaze.services.junk_review_page import count_open_groups
from phaze.services.proposal_queries import get_proposal_stats
from phaze.services.review import count_cue_review_candidates


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_REVIEW_TTL_SECONDS = 4.0
# Three missed refresh intervals make a stalled/hung refresher observable, while retaining last-good values.
_REVIEW_MAX_AGE_SECONDS = 3 * _REVIEW_TTL_SECONDS
_review_cache: dict[str, int] = {"reviewPending": 0, "reviewDedupe": 0, "reviewJunk": 0, "reviewCue": 0}
_review_cache_measured_at = 0.0
_review_cache_available = False


def get_cached_review_counts() -> dict[str, int]:
    """Copy last-good values; unknown before measurement, after failure, or after three missed intervals."""
    known = _review_cache_available and _review_cache_measured_at > 0 and time.monotonic() - _review_cache_measured_at <= _REVIEW_MAX_AGE_SECONDS
    return {**_review_cache, "reviewCountsKnown": int(known)}


async def compute_review_counts(session: AsyncSession) -> dict[str, int]:
    """Raising derivation: no degraded page read may masquerade as a measured zero."""
    async with session.begin_nested():
        pending = (await get_proposal_stats(session)).pending
        dedupe = await count_duplicate_groups(session)
        junk = await count_open_groups(session)
        cue = await count_cue_review_candidates(session, strict=True)
    return {"reviewPending": int(pending), "reviewDedupe": int(dedupe), "reviewJunk": int(junk), "reviewCue": int(cue)}


async def refresh_review_counts() -> dict[str, int]:
    """Refresh atomically off-request, retaining last-good counts while marking failures unknown."""
    global _review_cache, _review_cache_measured_at, _review_cache_available
    from phaze.database import async_session  # noqa: PLC0415 -- call-time factory keeps the cache testable and independent of request sessions

    try:
        async with async_session() as session:
            computed = await compute_review_counts(session)
    except Exception:
        _review_cache_available = False
        raise
    _review_cache = computed
    _review_cache_measured_at = time.monotonic()
    _review_cache_available = True
    return dict(computed)
