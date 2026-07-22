"""PATCH /api/internal/agent/proposals/{proposal_id}/state -- proposal-status transition (Phase 26 D-28; Phase 86 SIDECAR-03 cutover).

Allowed transitions (single source of truth):
  ProposalStatus.APPROVED -> {EXECUTED, FAILED}

Side effect on the FileRecord (when body.file_state is provided):
  EXECUTED + file_state="moved"     -> FileRecord.current_path=body.current_path (the real move destination)
The proposal->FileRecord.state cascade was removed in Phase 86 (SIDECAR-03): the handler no
longer mirrors the proposal outcome into FileRecord.state. The response `file_state` is now a
byte-for-byte echo of the request's `body.file_state` (the only caller discards it), so the wire
contract is unchanged.

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
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_proposals import ProposalStatePatch, ProposalStateResponse


router = APIRouter(prefix="/api/internal/agent/proposals", tags=["agent-internal"])


# D-28 allowed transitions. Single source of truth -- exhaustive over from-states.
_PROPOSAL_TRANSITIONS: dict[ProposalStatus, frozenset[ProposalStatus]] = {
    ProposalStatus.APPROVED: frozenset({ProposalStatus.EXECUTED, ProposalStatus.FAILED}),
}


@router.patch("/{proposal_id}/state", status_code=status.HTTP_200_OK, response_model=ProposalStateResponse)
async def patch_proposal_state(
    proposal_id: uuid.UUID,
    body: ProposalStatePatch,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProposalStateResponse:
    """Proposal status transition, plus an optional FileRecord.current_path update, in one transaction (D-28; Phase 86 SIDECAR-03)."""
    # 404 if proposal_id does not exist.
    #
    # phaze-jlu6: take a row lock (SELECT ... FOR UPDATE) so the read-check-write transition guard
    # is atomic. Without it two concurrent PATCHes (the phaze-fa2p double-dispatch: one job reports
    # EXECUTED, the other FAILED) both read cur=APPROVED, both pass the APPROVED->{EXECUTED,FAILED}
    # guard, and the last committer silently overwrites the winner -- a FAILED row whose file has
    # actually moved. Locking the proposal row serializes the two: the loser blocks until the winner
    # commits, then re-reads status=EXECUTED/FAILED, and its now-terminal from-state fails the guard
    # (409) instead of clobbering the authoritative record.
    proposal = await session.get(RenameProposal, proposal_id, with_for_update=True)
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
        # Pure replay: no outcome was requested, so echo file_state=None (SIDECAR-03 cutover --
        # the handler no longer reads the FileRecord row's state). current_path is a real path, not a
        # cascade, so it MAY still be echoed from the row.
        current_path_str: str | None = None
        if file_record is not None:
            current_path_str = file_record.current_path
        return ProposalStateResponse(
            proposal_id=proposal_id,
            proposal_state=cur.value,
            file_state=None,
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
        # SIDECAR-03 cutover: the proposal outcome is NO LONGER mirrored into the FileRecord row's state.
        # The response echoes the request's file_state (byte-identical wire contract, D-02);
        # current_path is the real move destination and IS still persisted (Pitfall 3).
        response_file_state = body.file_state
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
