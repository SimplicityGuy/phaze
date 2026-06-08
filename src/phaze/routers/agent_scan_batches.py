"""PATCH /api/internal/agent/scan-batches/{batch_id} -- scan-batch state-machine + cross-tenant guard (Phase 27 D-10, D-21).

Allowed transitions (single source of truth):
  ScanStatus.RUNNING -> {COMPLETED, FAILED}

The LIVE sentinel state is the watcher's terminal own-state -- operators NEVER
PATCH a sentinel batch. The Pydantic Literal on `ScanBatchPatch.status`
rejects `"live"` on the wire at validation time (422); the handler also
documents the invariant with a defensive belt-and-suspenders check.

Handler ordering (the ORDER is part of the contract, per T-27-01):
  1. 404 if batch_id is unknown.
  2. 403 if `batch.agent_id != caller.id` -- cross-tenant guard BEFORE the
     state-machine so a leaked batch_id cannot be probed via 409 vs 200 timing
     (mirrors agent_proposals.py:62-76 byte-for-byte).
  3. 200 idempotent echo if `body.status == batch.status` and no other
     mutating fields are set (zero DB writes; matches Phase 26 D-08 invariant).
  4. 409 if `body.status` is a transition not in `_SCAN_TRANSITIONS[cur]`.
  5. Apply partial fields via `model_dump(exclude_unset=True)` and commit.

This module deliberately omits `from __future__ import annotations` so FastAPI
can resolve `Annotated[AsyncSession, Depends(get_session)]` at app-build time
(matches the agent_execution.py / agent_proposals.py convention).
"""

from datetime import UTC, datetime
from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_scan_batches import ScanBatchPatch, ScanBatchPatchResponse


router = APIRouter(prefix="/api/internal/agent/scan-batches", tags=["agent-internal"])


# D-10 allowed transitions. Single source of truth -- exhaustive over from-states
# that an agent can mutate. LIVE is intentionally absent (sentinel-terminal),
# COMPLETED/FAILED are terminal post-mutation states (any PATCH attempting
# to leave them returns 409 -- see _SCAN_TRANSITIONS.get(cur, frozenset())).
_SCAN_TRANSITIONS: dict[ScanStatus, frozenset[ScanStatus]] = {
    ScanStatus.RUNNING: frozenset({ScanStatus.COMPLETED, ScanStatus.FAILED}),
}


def _row_to_response(batch: ScanBatch) -> ScanBatchPatchResponse:
    """Echo the current row state as a ScanBatchPatchResponse (D-Discretion §4)."""
    return ScanBatchPatchResponse(
        batch_id=batch.id,
        agent_id=batch.agent_id,
        scan_path=batch.scan_path,
        status=batch.status,
        total_files=batch.total_files,
        processed_files=batch.processed_files,
        error_message=batch.error_message,
    )


@router.patch("/{batch_id}", status_code=status.HTTP_200_OK, response_model=ScanBatchPatchResponse)
async def patch_scan_batch(
    batch_id: uuid.UUID,
    body: ScanBatchPatch,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ScanBatchPatchResponse:
    """Update a ScanBatch row. Cross-tenant guard runs BEFORE state-machine evaluation (T-27-01)."""
    # 1. 404 if batch_id is unknown.
    batch = await session.get(ScanBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scan batch not found")

    # 2. T-27-01 cross-tenant guard. Returns 403 BEFORE state-machine logic so
    # a leaked batch_id cannot be probed via 409 vs 200 timing. Mirrors
    # agent_proposals.py:62-76 byte-for-byte.
    if batch.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="scan batch does not belong to authenticated agent",
        )

    cur = ScanStatus(batch.status)

    # 3. Idempotent same-state PATCH: if `body.status == batch.status` AND no
    # other mutating field was set, echo the current row WITHOUT a DB write
    # (Phase 26 D-08 invariant -- no updated_at bump).
    set_fields = body.model_dump(exclude_unset=True)
    if body.status is not None and ScanStatus(body.status) == cur and set(set_fields.keys()) == {"status"}:
        # Same-state PATCH with no other fields: no-op echo (zero DB writes).
        return _row_to_response(batch)

    # 4. Defensive: LIVE is rejected at the Literal layer (422) -- this branch
    # documents the invariant for any future schema widening.
    if body.status is not None and ScanStatus(body.status) == ScanStatus.LIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cannot transition to LIVE",
        )

    # 5. State-machine transition guard.
    if body.status is not None:
        new = ScanStatus(body.status)
        if new != cur and new not in _SCAN_TRANSITIONS.get(cur, frozenset()):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"illegal transition {cur.value} -> {new.value}",
            )

    # 6. Apply explicit-set mutations only (default-None values do NOT clobber).
    for field, value in set_fields.items():
        setattr(batch, field, value)

    # 7. Stamp completed_at on the FIRST terminal transition so the admin UI's
    # elapsed timer freezes (incident 260608). The idempotent same-state no-op
    # returned at step 3 (so a same-state PATCH never stamps it); LIVE is
    # rejected at step 4; RUNNING is non-terminal. Guarding on `completed_at is
    # None` keeps it idempotent across repeated terminal PATCHes (first wins).
    if body.status is not None and ScanStatus(body.status) in {ScanStatus.COMPLETED, ScanStatus.FAILED} and batch.completed_at is None:
        batch.completed_at = datetime.now(UTC)

    await session.commit()
    await session.refresh(batch)
    return _row_to_response(batch)
