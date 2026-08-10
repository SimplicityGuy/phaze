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

# phaze-v392: the row-state set the terminal guard below is keyed off, reused for the
# completed_at stamp at step 7 too so there is exactly one definition of "terminal".
_TERMINAL_SCAN_STATUSES: frozenset[ScanStatus] = frozenset({ScanStatus.COMPLETED, ScanStatus.FAILED})


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
    #
    # phaze-bnvx: load under a row-level write lock, mirroring the two sibling PATCH handlers
    # (agent_execution.patch_execution_log, phaze-6zxs; agent_proposals.patch_proposal_state,
    # phaze-jlu6). ScanBatch has no version_id_col and the engine runs at READ COMMITTED, so a
    # plain PK read here is a TOCTOU: two concurrent PATCHes (e.g. the control-side stall reaper
    # racing an in-flight agent PATCH, or the agent client's own tenacity retry landing after a
    # prior attempt already committed) can both read the same RUNNING snapshot, both pass the
    # step-2b terminal guard and the step-5 transition guard against that stale read, and the
    # last committer wins blindly -- overwriting a just-committed COMPLETED/FAILED outcome, or
    # re-stamping a fresh heartbeat onto an already-terminal row. FOR UPDATE serializes the two:
    # the second PATCH blocks on the row lock until the first commits, then re-evaluates every
    # guard below against the now-committed status.
    batch = await session.get(ScanBatch, batch_id, with_for_update=True)
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
    set_fields = body.model_dump(exclude_unset=True)

    # 2b. phaze-v392: terminal-state guard, gated on the ROW STATE rather than on whether the
    # PATCH body happens to carry `status`. `ScanBatchPatch` makes every field optional and the
    # agent legitimately sends status-less progress PATCHes (`ScanBatchPatch(processed_files=...)`
    # -- tasks/scan.py:236/302/309, no status field at all). Every guard below this point (the old
    # steps 3-5) was conditioned on `body.status is not None`, so a status-less PATCH fell straight
    # through into the unconditional setattr loop (step 6) with NO terminal check: a COMPLETED/
    # FAILED batch could have `processed_files`/`total_files`/`error_message` silently overwritten
    # and its heartbeat re-stamped -- e.g. a SAQ at-least-once retry re-running an already-completed
    # `scan_directory` task and re-issuing its status-less progress PATCHes against the terminal row.
    #
    # Mirrors the `allowed_from` CAS idiom used by `update_proposal_status` / `update_proposal_fields`
    # (services/proposal_queries.py, phaze-uu17/phaze-3tj4): mutation is gated on the row's CURRENT
    # status being in an allowed set, evaluated before any write, regardless of which fields the
    # caller happens to set. A genuinely no-op PATCH (nothing set at all, or only `status`
    # re-affirming the row's own terminal value with no other mutating field) still gets the
    # idempotent 200 echo -- everything else against a terminal row is refused with 409.
    #
    # phaze-01a3h: the echo test used to require the body carry NOTHING but `status` -- too
    # narrow for the agent's real terminal PATCHes, which always carry extra fields
    # (ScanBatchPatch(status="completed", total_files=N, processed_files=N) --
    # tasks/scan.py:317-320; ScanBatchPatch(status="failed", error_message=...) --
    # :296-299/:337-340). The client funnel retries on ANY httpx.TransportError, including a
    # read timeout on a response whose request already committed server-side; the resulting
    # retry resends that identical multi-field body, failed the old keys-only check, and hit an
    # unwarranted 409 that crashed the (already-successful) scan task. Compare every
    # EXPLICITLY-set field against the ORM attribute it would overwrite instead of the key set:
    # a value-identical replay (every set field already matches the row) is a true no-op and
    # gets the 200 echo; any field that actually differs from the current row still 409s below,
    # so a genuine conflicting write is unaffected.
    if cur in _TERMINAL_SCAN_STATUSES:
        is_pure_echo = not set_fields or all(getattr(batch, field) == value for field, value in set_fields.items())
        if is_pure_echo:
            return _row_to_response(batch)
        if body.status is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"illegal transition {cur.value} -> {ScanStatus(body.status).value}",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"scan batch status is terminal ({cur.value}); cannot apply further updates",
        )

    # 3. Idempotent same-state PATCH: if `body.status == batch.status` AND no
    # other mutating field was set, echo the current row WITHOUT a DB write
    # (Phase 26 D-08 invariant -- no updated_at bump).
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

    # 6b. PRIMARY heartbeat (PR4): every real (non-no-op) applied PATCH advances
    # the scan -- the agent's scan_directory task PATCHes processed_files each
    # chunk. Stamp last_progress_at here, AFTER the same-state no-op early-return
    # at step 3 (so an idempotent PATCH never bumps the heartbeat) and AFTER the
    # set_fields apply loop. This drives the UI activity indicator and feeds the
    # control-side stall reaper's freshness check.
    batch.last_progress_at = datetime.now(UTC)

    # 7. Stamp completed_at on the FIRST terminal transition so the admin UI's
    # elapsed timer freezes (incident 260608). The idempotent same-state no-op
    # returned at step 3 (so a same-state PATCH never stamps it); LIVE is
    # rejected at step 4; RUNNING is non-terminal. Guarding on `completed_at is
    # None` keeps it idempotent across repeated terminal PATCHes (first wins).
    if body.status is not None and ScanStatus(body.status) in _TERMINAL_SCAN_STATUSES and batch.completed_at is None:
        batch.completed_at = datetime.now(UTC)

    await session.commit()
    await session.refresh(batch)
    return _row_to_response(batch)
