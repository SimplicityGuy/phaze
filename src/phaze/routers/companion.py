"""Companion association and duplicate detection API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

from phaze.database import get_session
from phaze.schemas.companion import AssociateResponse, DuplicateGroup, DuplicateGroupsResponse
from phaze.services.companion import associate_companions
from phaze.services.dedup import count_duplicate_groups, find_duplicate_groups


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1", tags=["companion"])


@router.post("/associate")
async def trigger_association(
    session: AsyncSession = Depends(get_session),
) -> AssociateResponse:
    """Trigger companion file association. Links unlinked companions to media in same directory."""
    count = await associate_companions(session)
    return AssociateResponse(
        new_associations=count,
        message=f"Associated {count} companion file(s) with media files",
    )


@router.get("/duplicates")
async def list_duplicates(
    limit: int = 100,
    offset: int = 0,
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
