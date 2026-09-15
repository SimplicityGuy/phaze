"""Persist bounded metadata-only orphan COMPANION diagnostics from an owning agent."""

from __future__ import annotations

import posixpath
from typing import Annotated
import unicodedata
import uuid  # noqa: TC003  # FastAPI resolves route annotations at runtime.

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002  # FastAPI dependency annotation.

from phaze.database import get_session
from phaze.models.agent import Agent  # noqa: TC001  # FastAPI dependency annotation.
from phaze.models.orphan_companion_diagnostic import OrphanCompanionDiagnostic
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_orphan_companions import OrphanCompanionChunk, OrphanCompanionChunkResponse


router = APIRouter(prefix="/api/internal/agent/scan-batches", tags=["agent-internal"])


def _normalize_absolute_path(value: str) -> str:
    """Return an NFC, lexically normalized POSIX path or reject a relative path."""
    normalized = posixpath.normpath(unicodedata.normalize("NFC", value))
    if not posixpath.isabs(normalized):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="diagnostic path must be absolute")
    return normalized


def _path_within_root(path: str, root: str) -> bool:
    """Use path-component containment, never a string-prefix approximation."""
    try:
        return posixpath.commonpath((path, root)) == root
    except ValueError:
        return False


@router.post("/{batch_id}/orphan-companions", status_code=status.HTTP_200_OK, response_model=OrphanCompanionChunkResponse)
async def post_orphan_companions(
    batch_id: uuid.UUID,
    body: OrphanCompanionChunk,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OrphanCompanionChunkResponse:
    """Insert a retry-safe diagnostic chunk for the caller's running scan batch."""
    batch = await session.get(ScanBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scan batch not found")
    if batch.agent_id != agent.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="scan batch does not belong to authenticated agent")
    if batch.status != ScanStatus.RUNNING.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="scan batch does not accept orphan diagnostics in its current state")

    configured_root = _normalize_absolute_path(batch.configured_root)
    scan_path = _normalize_absolute_path(batch.scan_path)
    records: dict[str, dict[str, object]] = {}
    for diagnostic in body.diagnostics:
        normalized_path = _normalize_absolute_path(diagnostic.normalized_path)
        if not _path_within_root(normalized_path, scan_path):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="diagnostic path is outside the batch scan path")
        records[normalized_path] = {
            "batch_id": batch.id,
            "configured_root": configured_root,
            "normalized_path": normalized_path,
            "companion_extension": diagnostic.companion_extension,
        }

    statement = (
        pg_insert(OrphanCompanionDiagnostic)
        .values(list(records.values()))
        .on_conflict_do_nothing(index_elements=["batch_id", "normalized_path"])
        .returning(OrphanCompanionDiagnostic.normalized_path)
    )
    # The len-all-count rule has misread this. It rewrites `len(QUERY.all())` into
    # `QUERY.count()` to keep the count server-side, which assumes a SELECT. This is an
    # INSERT ... ON CONFLICT DO NOTHING ... RETURNING, bounded by settings.agent_file_chunk_max:
    # the returned rows are the inserted set, so there is no query object to call .count() on
    # and no second round trip to save -- RETURNING is already how the inserted count comes
    # back. Rewriting it to satisfy the rule would mean dropping RETURNING for
    # `result.rowcount`, which is a behavioural change, not a cleanup.
    #
    # The marker below must stay on the line IMMEDIATELY above the statement: semgrep reads
    # nosemgrep from the preceding line only, so folding it into the paragraph above silently
    # stops suppressing.
    # nosemgrep: python.sqlalchemy.performance.performance-improvements.len-all-count
    inserted = len((await session.execute(statement)).scalars().all())
    await session.commit()
    return OrphanCompanionChunkResponse(batch_id=batch.id, inserted=inserted, existing=len(records) - inserted)
