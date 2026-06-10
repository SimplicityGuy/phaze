"""Scan API endpoints for triggering and monitoring file discovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from phaze.config import settings
from phaze.database import async_session, get_session
from phaze.models.scan_batch import ScanBatch
from phaze.schemas.scan import ScanRequest, ScanResponse, ScanStatusResponse
from phaze.services.enqueue_router import NoActiveAgentError, resolve_queue_for_task
from phaze.services.ingestion import run_scan


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


router = APIRouter(prefix="/api/v1", tags=["scan"])

# Hold references to background scan tasks to prevent garbage collection (RUF006)
_background_tasks: set[asyncio.Task[None]] = set()


@router.post("/scan")
async def trigger_scan(
    request: ScanRequest,
    http_request: Request,
    session: AsyncSession = Depends(get_session),
) -> ScanResponse:
    """Trigger a file discovery scan.

    Accepts an optional path override; defaults to the configured SCAN_PATH.
    Validates the path is a real directory and contains no path traversal.
    The scan runs in the background; this endpoint returns immediately.

    The per-file ``extract_file_metadata`` jobs that discovery auto-enqueues are
    routed to an active agent's consumed ``phaze-agent-<id>`` queue via the shared
    enqueue router (never the removed consumer-less default queue — the v4.0.6
    incident). With no active agent there is nothing to consume the extraction
    jobs, so we fail loud with a 503 rather than silently stranding them.
    """
    scan_path = request.path or settings.scan_path

    # Reject path traversal attempts
    if ".." in scan_path:
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")

    # Validate scan path is an existing directory
    if not Path(scan_path).is_dir():
        raise HTTPException(status_code=400, detail=f"Scan path is not a valid directory: {scan_path}")

    # Resolve the per-agent queue that will consume the auto-enqueued metadata
    # extraction jobs. A 503 (not a 200) is correct when no agent is available:
    # discovery would otherwise persist files whose extraction step has no consumer.
    try:
        routed = await resolve_queue_for_task("extract_file_metadata", http_request.app.state, session)
    except NoActiveAgentError as exc:
        raise HTTPException(
            status_code=503,
            detail="No active agent available to process discovered files — start an agent worker and retry",
        ) from exc

    batch_id = uuid.uuid4()
    task = asyncio.create_task(run_scan(scan_path, batch_id, async_session, queue=routed.queue))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return ScanResponse(batch_id=batch_id, message="Scan started")


@router.get("/scan/{batch_id}")
async def get_scan_status(batch_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> ScanStatusResponse:
    """Get the status of a scan batch by its ID."""
    result = await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))
    batch = result.scalar_one_or_none()

    if batch is None:
        raise HTTPException(status_code=404, detail=f"Scan batch not found: {batch_id}")

    return ScanStatusResponse(
        batch_id=batch.id,
        status=batch.status,
        scan_path=batch.scan_path,
        total_files=batch.total_files,
        processed_files=batch.processed_files,
        error_message=batch.error_message,
        created_at=batch.created_at,
    )
