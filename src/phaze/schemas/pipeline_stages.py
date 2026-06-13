"""Pydantic schemas for the per-stage control-plane endpoints (Phase 37)."""

from pydantic import BaseModel


class StagePriorityDelta(BaseModel):
    """Request body for ``POST /pipeline/stages/{stage}/priority``.

    Carries a signed ``delta`` applied to the stage's current priority. The UI steps
    by +/-10 (10 discrete levels across the 0-100 range), but the body may carry any
    integer; the endpoint clamps ``current + delta`` to ``[0, 100]`` before persisting.
    LOWER priority dequeues SOONER (maps directly onto SAQ's ``saq_jobs.priority`` with
    no inversion).
    """

    delta: int
