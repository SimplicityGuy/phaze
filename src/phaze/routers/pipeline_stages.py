"""Per-stage control-plane router -- operator pause / resume / priority endpoints (Phase 37).

Three POST endpoints let an operator steer the three agent pipeline stages
(``metadata`` / ``analyze`` / ``fingerprint``) at runtime:

- ``POST /pipeline/stages/{stage}/priority`` -- apply a clamped delta to the stage priority
  and reorder the queued backlog (LOWER dequeues sooner);
- ``POST /pipeline/stages/{stage}/pause`` -- drain-pause: active jobs finish, the queued
  backlog is parked (``scheduled = SENTINEL``);
- ``POST /pipeline/stages/{stage}/resume`` -- un-park ONLY the pause-parked backlog rows.

Each endpoint mutates the durable :class:`PipelineStageControl` intent row AND the live
``saq_jobs`` backlog (via the Plan-02 service helpers) in a SINGLE transaction, then returns
``{stage, priority, paused}`` so the Phase 38 UI can re-render. The response priority/paused
come from the CONTROL ROW (the durable intent), never from a serialized job's priority: a raw
``saq_jobs`` priority UPDATE changes the dequeue ORDER-BY column but does NOT rewrite the
serialized ``job`` BYTEA, so a later-dequeued ``Job.priority`` reflects the stale enqueue-time
stamp (Plan 37-03 finding).

Security (threat model):
- T-37-01 (Tampering): ``stage`` is validated against the :data:`STAGE_TO_FUNCTION` allowlist
  BEFORE any backlog filter is built -- an unknown stage returns 422.
- T-37-02 (DoS / pipeline stall): the priority delta is clamped to ``[0, 100]`` before the
  control row and backlog are updated (the DB CHECK backstops it).
- T-37-04 (access control): no app-layer auth is added -- these sit behind the same
  reverse-proxy internal-realm auth as the rest of ``/pipeline/*`` and the ``/saq`` UI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException

from phaze.database import get_session
from phaze.models import PipelineStageControl
from phaze.schemas.pipeline_stages import StagePriorityDelta  # noqa: TC001 (FastAPI body resolved at runtime)
from phaze.services.stage_control import pause_stage, resume_stage, set_stage_priority
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


router = APIRouter(tags=["pipeline"])

# Clamp bounds for a stage priority. Mirrors the DB CHECK (priority BETWEEN 0 AND 100) on
# pipeline_stage_control, keeping every stage inside SAQ's 0-32767 dequeue window (T-37-02).
_PRIORITY_MIN = 0
_PRIORITY_MAX = 100


def _validate_stage(stage: str) -> None:
    """Reject an unknown ``stage`` with a 422 BEFORE any backlog filter is built (T-37-01)."""
    if stage not in STAGE_TO_FUNCTION:
        raise HTTPException(status_code=422, detail="unknown stage")


async def _load_control_row(session: AsyncSession, stage: str, *, lock: bool = False) -> PipelineStageControl:
    """Return the (already-validated) stage's control row, creating it at defaults if absent.

    Migration 020 seeds the three rows, so in production ``session.get`` always returns a row.
    The defensive create keeps a fresh / partially-migrated DB from 500ing on the first control
    action and gives a non-null return for the type checker.

    When ``lock`` is true the row is fetched ``FOR UPDATE`` so a read-modify-write (the priority
    delta) serializes against a concurrent control action on the SAME stage — without it, two
    in-flight ``+delta`` requests both read the old value and one delta is silently lost (WR-02).
    """
    row = await session.get(PipelineStageControl, stage, with_for_update=True) if lock else await session.get(PipelineStageControl, stage)
    if row is None:
        row = PipelineStageControl(stage=stage, paused=False, priority=50)
        session.add(row)
    return row


def _response(row: PipelineStageControl) -> dict[str, Any]:
    """Return the ``{stage, priority, paused}`` shape from the durable control row."""
    return {"stage": row.stage, "priority": row.priority, "paused": row.paused}


@router.post("/pipeline/stages/{stage}/priority")
async def set_priority(
    stage: str,
    body: StagePriorityDelta,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Apply a clamped priority delta to ``stage`` and reorder its queued backlog."""
    _validate_stage(stage)
    row = await _load_control_row(session, stage, lock=True)
    new_priority = max(_PRIORITY_MIN, min(_PRIORITY_MAX, row.priority + body.delta))
    row.priority = new_priority
    await set_stage_priority(session, stage, new_priority)
    await session.commit()
    return _response(row)


@router.post("/pipeline/stages/{stage}/pause")
async def pause(
    stage: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Pause ``stage`` (drain): set ``paused=true`` and park the queued backlog."""
    _validate_stage(stage)
    row = await _load_control_row(session, stage)
    row.paused = True
    await pause_stage(session, stage)
    await session.commit()
    return _response(row)


@router.post("/pipeline/stages/{stage}/resume")
async def resume(
    stage: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Resume ``stage``: set ``paused=false`` and un-park ONLY the pause-parked backlog rows."""
    _validate_stage(stage)
    row = await _load_control_row(session, stage)
    row.paused = False
    await resume_stage(session, stage)
    await session.commit()
    return _response(row)
