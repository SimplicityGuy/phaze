"""Companion association and duplicate detection API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query

from phaze.database import get_session
from phaze.schemas.companion import AssociateResponse, DuplicateGroup, DuplicateGroupsResponse
from phaze.schemas.wire_bounds import INT32_MAX
from phaze.services.companion import associate_companions
from phaze.services.dedup import count_duplicate_groups, find_duplicate_groups


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1", tags=["companion"])


@router.post("/associate")
async def trigger_association(
    session: AsyncSession = Depends(get_session),
) -> AssociateResponse:
    """Trigger companion association: re-derive the links of every companion with current content features."""
    outcome = await associate_companions(session)
    message = f"Associated {outcome.links_created} companion link(s) with media files"
    if outcome.links_removed:
        message += f"; removed {outcome.links_removed} link(s) the linking chain no longer derives"
    if outcome.awaiting_features:
        message += f"; {outcome.awaiting_features} companion(s) await content features (phaze backfill companion-features)"
    return AssociateResponse(
        new_associations=outcome.links_created,
        removed_associations=outcome.links_removed,
        kept_associations=outcome.links_kept,
        awaiting_features=outcome.awaiting_features,
        message=message,
    )


@router.get("/duplicates")
async def list_duplicates(
    # limit=1000 is a DoS bound (wire_bounds rule 7), not a column width -- find_duplicate_groups
    # doesn't go through phaze.services.pagination (raw limit/offset, not page/page_size), so this
    # route needs its own ge=/le= guard per wire_bounds rule 8. Without it Postgres raises on a
    # negative LIMIT/OFFSET ("LIMIT/OFFSET must not be negative") and the request 500s (phaze-hpo9).
    limit: int = Query(100, ge=1, le=1000),
    # offset has no natural domain bound (it isn't stored, just fed to OFFSET), so it falls back to
    # the int32 column bound per wire_bounds rule 3.
    offset: int = Query(0, ge=0, le=INT32_MAX),
    session: AsyncSession = Depends(get_session),
) -> DuplicateGroupsResponse:
    """List groups of files sharing the same SHA256 hash."""
    raw_groups = await find_duplicate_groups(session, limit=limit, offset=offset)
    total = await count_duplicate_groups(session)
    groups = [DuplicateGroup(**g) for g in raw_groups]
    return DuplicateGroupsResponse(
        groups=groups,
        total_groups=total,
        limit=limit,
        offset=offset,
    )
