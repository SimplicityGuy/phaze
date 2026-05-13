"""Contract tests for PATCH /api/internal/agent/scan-batches/{batch_id} (Phase 27 D-10, D-21).

Mirrors tests/test_routers/test_agent_proposals.py:25-35 smoke-app fixture pattern.
The new endpoint must:
- Return 404 BEFORE the cross-tenant guard (unknown batch_id).
- Return 403 BEFORE the state-machine evaluation when caller != owner (T-27-01).
- Reject status="live" at the schema layer (422 -- Literal["running","completed","failed"]).
- Treat same-state PATCH as 200 echo with NO updated_at bump (idempotent no-op invariant).
- Allow only RUNNING -> {COMPLETED, FAILED}; reject all other transitions with 409.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers import agent_scan_batches


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_scan_batches.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _make_client(session: AsyncSession, token: str | None = None) -> AsyncClient:
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_batch(
    session: AsyncSession,
    agent_id: str,
    status: ScanStatus = ScanStatus.RUNNING,
    scan_path: str = "/test/music",
) -> uuid.UUID:
    """Seed a ScanBatch for the given agent and return its id."""
    batch_id = uuid.uuid4()
    batch = ScanBatch(
        id=batch_id,
        agent_id=agent_id,
        scan_path=scan_path,
        status=status.value,
        total_files=0,
        processed_files=0,
    )
    session.add(batch)
    await session.commit()
    return batch_id


@pytest.mark.asyncio
async def test_running_to_completed_200(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """RUNNING -> COMPLETED transition succeeds; counts are applied."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/scan-batches/{batch_id}",
            json={"status": "completed", "total_files": 5, "processed_files": 5},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["total_files"] == 5
    assert body["processed_files"] == 5
    assert body["batch_id"] == str(batch_id)
    assert body["agent_id"] == agent.id


@pytest.mark.asyncio
async def test_running_to_failed_with_error_message_200(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """RUNNING -> FAILED transition succeeds and error_message persists."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/scan-batches/{batch_id}",
            json={"status": "failed", "error_message": "Path missing"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert body["error_message"] == "Path missing"
    # DB assertion: persisted
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.status == ScanStatus.FAILED.value
    assert b.error_message == "Path missing"


@pytest.mark.asyncio
async def test_same_state_idempotent_no_op(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """Re-PATCH to the SAME state returns 200 with NO updated_at bump (zero DB writes)."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    # Capture updated_at after seeding
    await session.commit()
    session.expire_all()
    before = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    before_updated_at = before.updated_at
    async with _make_client(session, raw_token) as ac:
        r1 = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"status": "running"})
        r2 = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"status": "running"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Re-read; updated_at MUST be unchanged
    await session.commit()
    session.expire_all()
    after = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert after.updated_at == before_updated_at, "same-state PATCH must NOT bump updated_at"
    assert after.status == ScanStatus.RUNNING.value


@pytest.mark.asyncio
async def test_completed_to_running_409(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """COMPLETED -> RUNNING is illegal; returns 409."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.COMPLETED)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"status": "running"})
    assert r.status_code == 409
    assert "illegal transition" in r.text.lower()


@pytest.mark.asyncio
async def test_failed_to_completed_409(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """FAILED -> COMPLETED is illegal; returns 409."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.FAILED)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"status": "completed"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_live_status_in_body_422(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """status="live" is rejected at the Pydantic Literal layer (422)."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"status": "live"})
    assert r.status_code == 422
    # DB unchanged
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.status == ScanStatus.RUNNING.value


@pytest.mark.asyncio
async def test_batch_not_found_404(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """Unknown batch_id -> 404 'scan batch not found'."""
    _, raw_token = seed_test_agent
    unknown_id = uuid.uuid4()
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{unknown_id}", json={"status": "completed"})
    assert r.status_code == 404
    assert "not found" in r.text.lower()


@pytest.mark.asyncio
async def test_extra_field_422(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """extra='forbid' rejects unknown fields."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/scan-batches/{batch_id}",
            json={"status": "completed", "unknown": "x"},
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_cross_agent_403_before_state_machine(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """T-27-01: agent B PATCHing agent A's batch must return 403, NOT 409.

    Seed a batch owned by agent A in COMPLETED state -- if the cross-tenant
    guard ran AFTER the state-machine, the response would be 409 (illegal
    RUNNING -> COMPLETED transition attempted by passing status='running').
    The fact that we get 403 PROVES the cross-tenant check runs BEFORE
    state-machine evaluation.
    """
    agent_a, _ = seed_test_agent
    # Seed agent A's batch in a state where any transition would be 409
    batch_id = await _seed_batch(session, agent_a.id, ScanStatus.COMPLETED)

    # Seed a SECOND agent (B) inline -- mirrors test_agent_proposals.py:208-217.
    raw_token_b = "phaze_agent_" + secrets.token_urlsafe(32)
    token_hash_b = hashlib.sha256(raw_token_b.encode("utf-8")).hexdigest()
    agent_b = Agent(
        id="test-agent-b",
        name="test-agent-b",
        token_hash=token_hash_b,
        scan_roots=["/test/b"],
    )
    session.add(agent_b)
    await session.commit()

    async with _make_client(session, raw_token_b) as ac:
        # Attempt an illegal transition (COMPLETED -> RUNNING) -- if cross-tenant
        # guard ran after state-machine eval, this would be 409. Must be 403.
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"status": "running"})
    assert r.status_code == 403, f"Expected 403 (cross-tenant guard FIRST), got {r.status_code}: {r.text}"
    assert r.status_code != 409, "cross-tenant check must run BEFORE state-machine evaluation"
    assert "does not belong" in r.text.lower() or "belong to authenticated" in r.text.lower()


@pytest.mark.asyncio
async def test_missing_auth_returns_401(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """No Authorization header -> 401."""
    agent, _ = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, token=None) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"status": "completed"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unknown_token_returns_403(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """Bearer token whose hash isn't in agents.token_hash -> 403."""
    agent, _ = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, token="phaze_agent_unknown-token-1234") as ac:  # noqa: S106
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"status": "completed"})
    assert r.status_code == 403


def test_router_registered_in_main_app() -> None:
    """Task 3: phaze.main.create_app() must include the agent_scan_batches router.

    Asserts the PATCH /api/internal/agent/scan-batches/{batch_id} operation is
    reachable on the production app -- not just the smoke-app fixture used by
    the other tests in this file. This is the Plan 03 Task 3 wiring acceptance
    check.
    """
    from phaze.main import create_app

    app = create_app()
    paths = [getattr(r, "path", "") for r in app.routes]
    assert any("/api/internal/agent/scan-batches" in p for p in paths), f"agent_scan_batches.router not registered in create_app(); paths={paths}"

    # Also confirm the PATCH operation has the right method binding.
    matching = [r for r in app.routes if "/api/internal/agent/scan-batches" in getattr(r, "path", "")]
    assert any("PATCH" in getattr(r, "methods", set()) for r in matching), "No PATCH method bound on the scan-batches route"
