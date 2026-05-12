"""PATCH /api/internal/agent/proposals/{proposal_id}/state -- joint Proposal+FileRecord state transition (Phase 26 D-28).

Allowed transitions (single source of truth):
  ProposalStatus.APPROVED -> {EXECUTED, FAILED}

Side effects on the FileRecord (when body.file_state is provided):
  EXECUTED + file_state="moved"     -> FileRecord.state=MOVED, current_path=body.current_path
  FAILED + file_state="unchanged"   -> FileRecord.state=UNCHANGED, current_path preserved

Same-state PATCH (e.g., EXECUTED -> EXECUTED): 200 idempotent no-op (per D-28).
Other transitions: 409 with detail `"illegal transition {cur} -> {new}"`.
proposal_id not found: 404.

Joint update is atomic: ONE session.commit() at the end of the handler.
RESEARCH Pitfall 6 (joint partial-commit): violated only by inserting an extra
commit between row mutations.

This module deliberately omits `from __future__ import annotations` so FastAPI
can resolve `Annotated[AsyncSession, Depends(get_session)]` at app-build time
(matches the agent_execution.py / agent_auth.py convention).
"""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_proposals import ProposalStatePatch, ProposalStateResponse


router = APIRouter(prefix="/api/internal/agent/proposals", tags=["agent-internal"])


# D-28 allowed transitions. Single source of truth -- exhaustive over from-states.
_PROPOSAL_TRANSITIONS: dict[ProposalStatus, frozenset[ProposalStatus]] = {
    ProposalStatus.APPROVED: frozenset({ProposalStatus.EXECUTED, ProposalStatus.FAILED}),
}

# Maps proposal_state -> expected file_state (informational; the caller MUST supply
# file_state in the body, and the validator on the schema enforces moved+path coupling).
_FILE_FOLLOW: dict[ProposalStatus, FileState] = {
    ProposalStatus.EXECUTED: FileState.MOVED,
    ProposalStatus.FAILED: FileState.UNCHANGED,
}


@router.patch("/{proposal_id}/state", status_code=status.HTTP_200_OK, response_model=ProposalStateResponse)
async def patch_proposal_state(
    proposal_id: uuid.UUID,
    body: ProposalStatePatch,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProposalStateResponse:
    """Joint Proposal + FileRecord state transition in one transaction (D-28)."""
    # 404 if proposal_id does not exist
    proposal = await session.get(RenameProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")

    # W1 / T-26-08-S2: cross-tenant guard. Load FileRecord.agent_id and reject if
    # the proposal's file belongs to a different agent than the authenticated one.
    # Single-operator deployment makes this low-impact today, but the structural
    # check matters for future multi-tenant. Returns 403 BEFORE state-machine logic
    # so a leaked proposal_id cannot be probed via 409 timing.
    file_record = await session.get(FileRecord, proposal.file_id)
    if file_record is not None and file_record.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="proposal does not belong to authenticated agent",
        )

    cur = ProposalStatus(proposal.status)
    new = ProposalStatus(body.proposal_state)

    # Same-state PATCH is idempotent 200 no-op (D-28 invariant). Echo current row
    # state without DB writes -- the SAQ retry's previous successful PATCH already
    # persisted the canonical state, so we just report it back.
    if cur == new:
        file_state_str: str | None = None
        current_path_str: str | None = None
        if file_record is not None:
            file_state_str = file_record.state
            current_path_str = file_record.current_path
        return ProposalStateResponse(
            proposal_id=proposal_id,
            proposal_state=cur.value,
            file_state=file_state_str,
            current_path=current_path_str,
        )

    # Disallowed transition: 409 with explicit detail.
    allowed = _PROPOSAL_TRANSITIONS.get(cur, frozenset())
    if new not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"illegal transition {cur.value} -> {new.value}",
        )

    # Apply joint mutation in one transaction (Pitfall 6: ONE commit only).
    proposal.status = new.value
    if body.error_message is not None:
        proposal.reason = body.error_message

    response_file_state: str | None = None
    response_current_path: str | None = None

    if body.file_state is not None and file_record is not None:
        new_file_state = FileState(body.file_state)
        file_record.state = new_file_state.value
        response_file_state = new_file_state.value
        if body.current_path is not None:
            file_record.current_path = body.current_path
            response_current_path = body.current_path
        else:
            response_current_path = file_record.current_path
    # If file_record is None (FK orphan), skip the file update but still update the proposal.
    # This is a data-integrity warning case; the FK constraint should prevent it.

    await session.commit()
    return ProposalStateResponse(
        proposal_id=proposal_id,
        proposal_state=new.value,
        file_state=response_file_state,
        current_path=response_current_path,
    )
