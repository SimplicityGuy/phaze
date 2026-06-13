"""Tests for the per-stage control-plane endpoints (Phase 37, plan 04).

Focus: input validation (allowlist 422), priority clamp at both bounds, the
``{stage, priority, paused}`` return shape, and that the durable
``pipeline_stage_control`` row reflects each action. The live ``saq_jobs`` backlog
reorder/park/un-park is proven by the Plan 03 real-PG integration tests; here the
service helpers' raw UPDATE runs against a minimal, empty ``saq_jobs`` table (a no-op)
so the endpoint -> helper -> SQL wiring is exercised end-to-end without a live broker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from phaze.models import PipelineStageControl


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# Minimal mirror of the SAQ-managed saq_jobs table (only the columns the service helpers
# touch). saq_jobs is NOT a SQLAlchemy model, so Base.metadata.create_all never builds it;
# the helpers' raw UPDATE would otherwise fail on an absent relation. Empty table => no-op.
_SAQ_JOBS_DDL = text(
    """
    CREATE TABLE IF NOT EXISTS saq_jobs (
        key TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        priority SMALLINT NOT NULL DEFAULT 0,
        scheduled BIGINT NOT NULL DEFAULT 0
    )
    """
)


async def _seed_stages(session: AsyncSession) -> None:
    """Create the empty saq_jobs table and seed the 3 control rows at the baseline."""
    await session.execute(_SAQ_JOBS_DDL)
    session.add_all(
        [
            PipelineStageControl(stage="metadata", paused=False, priority=50),
            PipelineStageControl(stage="analyze", paused=False, priority=50),
            PipelineStageControl(stage="fingerprint", paused=False, priority=50),
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_unknown_stage_returns_422(client: AsyncClient, session: AsyncSession) -> None:
    """An unknown stage is rejected with 422 before any backlog filter is built (T-37-01)."""
    await _seed_stages(session)

    response = await client.post("/pipeline/stages/bogus/priority", json={"delta": 5})
    assert response.status_code == 422
    assert response.json()["detail"] == "unknown stage"


@pytest.mark.asyncio
async def test_priority_clamps_high(client: AsyncClient, session: AsyncSession) -> None:
    """A delta that would exceed 100 clamps the persisted priority to 100 (T-37-02)."""
    await _seed_stages(session)

    response = await client.post("/pipeline/stages/analyze/priority", json={"delta": 100})
    assert response.status_code == 200
    assert response.json() == {"stage": "analyze", "priority": 100, "paused": False}

    row = await session.get(PipelineStageControl, "analyze")
    assert row is not None
    assert row.priority == 100


@pytest.mark.asyncio
async def test_priority_clamps_low(client: AsyncClient, session: AsyncSession) -> None:
    """A delta that would drop below 0 clamps the persisted priority to 0 (T-37-02)."""
    await _seed_stages(session)

    response = await client.post("/pipeline/stages/analyze/priority", json={"delta": -100})
    assert response.status_code == 200
    assert response.json() == {"stage": "analyze", "priority": 0, "paused": False}

    row = await session.get(PipelineStageControl, "analyze")
    assert row is not None
    assert row.priority == 0


@pytest.mark.asyncio
async def test_valid_delta_persists_new_priority(client: AsyncClient, session: AsyncSession) -> None:
    """A within-range delta returns and persists the new absolute priority."""
    await _seed_stages(session)

    response = await client.post("/pipeline/stages/analyze/priority", json={"delta": -10})
    assert response.status_code == 200
    assert response.json() == {"stage": "analyze", "priority": 40, "paused": False}

    row = await session.get(PipelineStageControl, "analyze")
    assert row is not None
    assert row.priority == 40


@pytest.mark.asyncio
async def test_pause_then_resume_flip_and_persist_paused(client: AsyncClient, session: AsyncSession) -> None:
    """pause sets paused=true and resume sets it back to false; both persist + return shape."""
    await _seed_stages(session)

    pause_response = await client.post("/pipeline/stages/fingerprint/pause")
    assert pause_response.status_code == 200
    assert pause_response.json() == {"stage": "fingerprint", "priority": 50, "paused": True}

    paused_row = await session.get(PipelineStageControl, "fingerprint")
    assert paused_row is not None
    assert paused_row.paused is True

    resume_response = await client.post("/pipeline/stages/fingerprint/resume")
    assert resume_response.status_code == 200
    assert resume_response.json() == {"stage": "fingerprint", "priority": 50, "paused": False}

    resumed_row = await session.get(PipelineStageControl, "fingerprint")
    assert resumed_row is not None
    assert resumed_row.paused is False
