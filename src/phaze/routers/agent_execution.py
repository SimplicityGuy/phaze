"""POST + PATCH /api/internal/agent/execution-log -- write-ahead audit trail (phase-25 D-13, D-15).

This router holds the most behaviorally-rich endpoint in Phase 25:

- **POST** creates a row whose primary key the AGENT supplies (D-13). Server
  uses `INSERT ... ON CONFLICT (id) DO NOTHING` so that replays after a flaky
  network retry are silent no-ops, never PK-violation 500s.

- **PATCH** updates a row's status, applying an application-level monotonic
  invariant (D-15). The lifecycle ladder is
  `PENDING < IN_PROGRESS < COMPLETED < FAILED`; a regression returns 409 with
  detail `"execution-log status would regress"`; a PATCH against a terminal
  row (COMPLETED or FAILED) returns 409 with detail `"execution-log status is
  terminal"`. Same-status PATCH is allowed (idempotent retry).

Auth: every handler gates on `Depends(get_authenticated_agent)`. The agent_id
returned in responses comes from the auth dep (AUTH-01); the ExecutionLog row
itself has no agent_id column -- attribution is via the proposal_id FK chain.

This module deliberately omits `from __future__ import annotations` so FastAPI
can resolve `Annotated[AsyncSession, Depends(get_session)]` at app-build time
(matches the duplicates.py / tags.py / agent_auth.py convention).
"""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_execution import (
    ExecutionLogCreate,
    ExecutionLogCreateResponse,
    ExecutionLogPatch,
    ExecutionLogPatchResponse,
)


router = APIRouter(prefix="/api/internal/agent/execution-log", tags=["agent-internal"])


# D-15 monotonic ladder. Higher value = "further along" in the lifecycle.
# Comparator is strict `<` (not `<=`) so same-status PATCH is allowed for
# idempotent retry; this is the D-15 footnote enforced by
# `test_same_status_patch_allowed`.
_STATUS_ORDER: dict[ExecutionStatus, int] = {
    ExecutionStatus.PENDING: 0,
    ExecutionStatus.IN_PROGRESS: 1,
    ExecutionStatus.COMPLETED: 2,
    ExecutionStatus.FAILED: 3,
}
_TERMINAL: frozenset[ExecutionStatus] = frozenset({ExecutionStatus.COMPLETED, ExecutionStatus.FAILED})


@router.post("", status_code=status.HTTP_200_OK, response_model=ExecutionLogCreateResponse)
async def create_execution_log(
    body: ExecutionLogCreate,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ExecutionLogCreateResponse:
    """Create an ExecutionLog row. Agent supplies `id` (D-13); replay POST is a no-op.

    Per D-13: `INSERT ... ON CONFLICT (id) DO NOTHING` -- first-create wins;
    identical replays are silent no-ops. The agent persists `id` in SAQ job
    state so retries point at the same row.

    `agent_id` is NOT in the request body and NOT a column on ExecutionLog;
    the response echoes the auth-dep's `agent.id` so the caller can correlate
    the row with the authenticated identity.
    """
    payload = body.model_dump()
    stmt = pg_insert(ExecutionLog).values([payload]).on_conflict_do_nothing(index_elements=["id"])
    await session.execute(stmt)
    await session.commit()
    return ExecutionLogCreateResponse(agent_id=agent.id, execution_log_id=body.id)


@router.patch("/{execution_log_id}", status_code=status.HTTP_200_OK, response_model=ExecutionLogPatchResponse)
async def patch_execution_log(
    execution_log_id: uuid.UUID,
    body: ExecutionLogPatch,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ExecutionLogPatchResponse:
    """Update an ExecutionLog row. Status transitions are monotonic (D-15).

    - 404 if `execution_log_id` does not exist
    - 409 if current status is terminal (COMPLETED or FAILED) -- detail
      `"execution-log status is terminal"` (this guard runs BEFORE the regress
      check so operator triage can distinguish "agent retrying past terminal"
      from "stale retry walks status backward").
    - 409 if proposed status regresses (e.g., IN_PROGRESS -> PENDING) -- detail
      `"execution-log status would regress"`.
    - 200 otherwise (same-status PATCH allowed for idempotent retry; comparator
      is strict `<`, NOT `<=`).
    """
    existing = await session.get(ExecutionLog, execution_log_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="execution-log not found")

    # `existing.status` is stored as String(20); cast back to enum for ordered comparison.
    cur = ExecutionStatus(existing.status)
    new = body.status  # already an ExecutionStatus instance (Pydantic-validated)

    # D-15: terminal-state guard runs FIRST (early exit before regress check).
    if cur in _TERMINAL:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="execution-log status is terminal")

    # D-15: monotonic guard -- `<` (not `<=`) so same-status retry is allowed.
    if _STATUS_ORDER[new] < _STATUS_ORDER[cur]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="execution-log status would regress")

    # Apply explicit-set mutations only (Pydantic `exclude_unset=True` -- default-None
    # values do NOT clobber existing data).
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(existing, field, value)
    await session.commit()
    return ExecutionLogPatchResponse(
        agent_id=agent.id,
        execution_log_id=execution_log_id,
        status=ExecutionStatus(existing.status),
    )
