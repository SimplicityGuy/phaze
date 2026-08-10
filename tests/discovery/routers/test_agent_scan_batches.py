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
async def test_defensive_live_409_when_literal_bypassed(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """Coverage gap fill (Codecov PR #59): agent_scan_batches.py:99.

    The Pydantic Literal["running","completed","failed"] rejects "live" at 422,
    so the in-handler defensive check normally cannot fire. This test bypasses
    the Literal via ``ScanBatchPatch.model_construct`` and invokes the handler
    function directly to pin the defensive 409 branch — if a future schema
    widening (e.g., adding "live" to the Literal) accidentally exposes the
    transition, the handler still refuses with 409.
    """
    from fastapi import HTTPException

    from phaze.routers.agent_scan_batches import patch_scan_batch
    from phaze.schemas.agent_scan_batches import ScanBatchPatch

    agent, _ = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    # model_construct skips Literal validation; this is the only way to reach
    # the defensive 409 branch in the handler body.
    rogue = ScanBatchPatch.model_construct(status="live")

    with pytest.raises(HTTPException) as excinfo:
        await patch_scan_batch(batch_id=batch_id, body=rogue, agent=agent, session=session)

    assert excinfo.value.status_code == 409
    assert "LIVE" in excinfo.value.detail or "live" in excinfo.value.detail.lower()

    # DB unchanged.
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.status == ScanStatus.RUNNING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["total_files", "processed_files"])
async def test_int32_overflowing_count_422s_without_mutating_the_row(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    field: str,
) -> None:
    """phaze-ty0o: an int32-overflowing ``total_files``/``processed_files`` is rejected 422 first.

    ``scan_batches.total_files``/``.processed_files`` are ``Integer`` (int4, max 2147483647).
    Pre-fix these were entirely unbounded on the wire -- an out-of-range count reaching Postgres
    would raise ``NumericValueOutOfRange`` and abort the transaction rather than fail cleanly.
    """
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)

    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={field: 2147483648})

    assert r.status_code == 422, r.text
    assert field in r.text
    assert "less_than_equal" in r.text, r.text

    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.total_files == 0
    assert b.processed_files == 0


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


# ---------------------------------------------------------------------------
# Incident 260608: completed_at terminal-timestamp stamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_completed_stamps_completed_at(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """RUNNING -> COMPLETED stamps completed_at in the same commit."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/scan-batches/{batch_id}",
            json={"status": "completed", "total_files": 3, "processed_files": 3},
        )
    assert r.status_code == 200, r.text
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.completed_at is not None


@pytest.mark.asyncio
async def test_terminal_failed_stamps_completed_at(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """RUNNING -> FAILED stamps completed_at."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/scan-batches/{batch_id}",
            json={"status": "failed", "error_message": "Permission denied"},
        )
    assert r.status_code == 200, r.text
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.completed_at is not None


@pytest.mark.asyncio
async def test_processed_files_only_leaves_completed_at_null(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """A non-terminal PATCH (processed_files only, still RUNNING) leaves completed_at NULL."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"processed_files": 2})
    assert r.status_code == 200, r.text
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.status == ScanStatus.RUNNING.value
    assert b.completed_at is None


@pytest.mark.asyncio
async def test_same_state_patch_does_not_stamp_completed_at(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """Idempotent same-state PATCH (status='running' on a RUNNING batch) never stamps completed_at."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"status": "running"})
    assert r.status_code == 200, r.text
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.completed_at is None


# ---------------------------------------------------------------------------
# PR4: last_progress_at heartbeat stamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_patch_stamps_last_progress_at(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """A real PATCH advancing processed_files stamps last_progress_at."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"processed_files": 7})
    assert r.status_code == 200, r.text
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.status == ScanStatus.RUNNING.value
    assert b.last_progress_at is not None, "a real applied PATCH must stamp the heartbeat"


@pytest.mark.asyncio
async def test_terminal_patch_stamps_last_progress_at(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """A terminal RUNNING -> COMPLETED PATCH stamps last_progress_at too."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/scan-batches/{batch_id}",
            json={"status": "completed", "total_files": 4, "processed_files": 4},
        )
    assert r.status_code == 200, r.text
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.last_progress_at is not None


@pytest.mark.asyncio
async def test_same_state_no_op_does_not_stamp_last_progress_at(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """An idempotent same-state PATCH (status='running' on a RUNNING batch) does NOT stamp last_progress_at.

    The no-op echo returns BEFORE any write, so the heartbeat (NULL on seed)
    stays NULL -- a repeated same-state PATCH must not bump the heartbeat.
    """
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    # Confirm the seed left last_progress_at NULL.
    await session.commit()
    session.expire_all()
    seeded = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert seeded.last_progress_at is None
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"status": "running"})
    assert r.status_code == 200, r.text
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.last_progress_at is None, "same-state no-op PATCH must NOT stamp the heartbeat"


# ---------------------------------------------------------------------------
# phaze-v392: terminal-state guard for status-less PATCHes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", [ScanStatus.COMPLETED, ScanStatus.FAILED])
async def test_status_less_progress_patch_on_terminal_batch_409s(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    terminal_status: ScanStatus,
) -> None:
    """A status-less progress PATCH (`ScanBatchPatch(processed_files=...)`, no `status`) against an
    already-terminal batch must be rejected with 409, not silently applied.

    Mirrors the real trigger: `tasks/scan.py:302`/`:309` issue exactly this shape
    (`ScanBatchPatch(processed_files=total)`) on every chunk, with no `status` field at all -- a SAQ
    at-least-once retry re-running an already-completed `scan_directory` task re-issues these against
    the now-terminal row. Pre-fix, every guard (steps 3-5) was conditioned on `body.status is not
    None`, so this shape fell straight through to the unconditional setattr + commit.
    """
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, terminal_status)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"processed_files": 999})
    assert r.status_code == 409, r.text
    assert "terminal" in r.text.lower()

    # DB unchanged: the terminal row's processed_files must NOT have been overwritten.
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.processed_files == 0
    assert b.status == terminal_status.value


@pytest.mark.asyncio
async def test_status_less_total_files_patch_on_terminal_batch_409s(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """The pre-count PATCH shape (`ScanBatchPatch(total_files=...)`, `tasks/scan.py:262`) is also
    guarded against a terminal row -- a second status-less field, same bypass class."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.COMPLETED)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"total_files": 42})
    assert r.status_code == 409, r.text

    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.total_files == 0


@pytest.mark.asyncio
async def test_status_less_progress_patch_on_terminal_batch_does_not_bump_heartbeat_or_updated_at(
    session: AsyncSession, seed_test_agent: tuple[Agent, str]
) -> None:
    """A rejected status-less PATCH against a terminal batch is a genuine no-write: neither
    `last_progress_at` nor `updated_at` moves (the 409 must be raised BEFORE step 6/6b)."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.COMPLETED)
    await session.commit()
    session.expire_all()
    before = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    before_updated_at, before_last_progress_at = before.updated_at, before.last_progress_at

    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"processed_files": 5})
    assert r.status_code == 409

    await session.commit()
    session.expire_all()
    after = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert after.updated_at == before_updated_at
    assert after.last_progress_at == before_last_progress_at


@pytest.mark.asyncio
async def test_empty_patch_on_terminal_batch_is_idempotent_echo(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """A PATCH with NO fields set at all against a terminal batch is a genuine no-op: 200 echo."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.COMPLETED)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_same_terminal_status_reaffirmed_with_no_other_fields_still_echoes(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """Re-PATCHing the SAME terminal status with no other field is still the idempotent 200 echo
    (the terminal guard must not regress this pre-existing same-state no-op behavior)."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.COMPLETED)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json={"status": "completed"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_same_terminal_status_with_extra_mutating_field_still_409s(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """Re-affirming the SAME terminal status while ALSO carrying a mutating field (e.g. a duplicate
    terminal PATCH after a lost ack) is refused -- the row-state guard, not the presence of `status`,
    decides whether a mutation is permitted."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.COMPLETED)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/scan-batches/{batch_id}",
            json={"status": "completed", "processed_files": 999},
        )
    assert r.status_code == 409, r.text
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.processed_files == 0


# ---------------------------------------------------------------------------
# phaze-01a3h: at-least-once retry of the agent's REAL terminal PATCH body (which always
# carries extra fields alongside status, e.g. tasks/scan.py:317-320/:296-299/:337-340) must be
# a 200 echo, not a 409 -- the old guard only recognized a bare {"status"} body as a replay.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_completed_replay_of_real_agent_body_is_idempotent_echo(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """Reproduces tasks/scan.py:317-320's terminal PATCH (status="completed" + matching
    total_files/processed_files) being retried after a lost-response TransportError: the
    IDENTICAL body resent against the now-COMPLETED row must echo 200, not 409 (phaze-01a3h)."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    body = {"status": "completed", "total_files": 42, "processed_files": 42}
    async with _make_client(session, raw_token) as ac:
        first = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json=body)
        assert first.status_code == 200, first.text
        replay = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["total_files"] == 42
    assert replay.json()["processed_files"] == 42


@pytest.mark.asyncio
async def test_terminal_failed_replay_of_real_agent_body_is_idempotent_echo(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """Same replay-tolerance for tasks/scan.py's failed-terminal PATCH shape
    (status="failed" + matching error_message, :296-299/:337-340)."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    body = {"status": "failed", "error_message": "Controller error: boom"}
    async with _make_client(session, raw_token) as ac:
        first = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json=body)
        assert first.status_code == 200, first.text
        replay = await ac.patch(f"/api/internal/agent/scan-batches/{batch_id}", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["error_message"] == "Controller error: boom"


@pytest.mark.asyncio
async def test_terminal_completed_replay_with_a_differing_field_still_409s(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """A retry is only tolerated when it is VALUE-IDENTICAL to the committed row -- a body that
    differs in even one field from a genuinely different attempt (not a replay of the same
    request) still 409s, so the widened echo test cannot mask a real conflicting write."""
    agent, raw_token = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)
    async with _make_client(session, raw_token) as ac:
        first = await ac.patch(
            f"/api/internal/agent/scan-batches/{batch_id}",
            json={"status": "completed", "total_files": 42, "processed_files": 42},
        )
        assert first.status_code == 200, first.text
        conflicting = await ac.patch(
            f"/api/internal/agent/scan-batches/{batch_id}",
            json={"status": "completed", "total_files": 43, "processed_files": 43},
        )
    assert conflicting.status_code == 409, conflicting.text
    await session.commit()
    session.expire_all()
    b = (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()
    assert b.total_files == 42
    assert b.processed_files == 42


def test_router_registered_in_main_app() -> None:
    """Task 3: phaze.main.create_app() must include the agent_scan_batches router.

    Asserts the PATCH /api/internal/agent/scan-batches/{batch_id} operation is
    reachable on the production app -- not just the smoke-app fixture used by
    the other tests in this file. This is the Plan 03 Task 3 wiring acceptance
    check.
    """
    from phaze.main import create_app
    from tests._route_introspection import iter_effective_routes

    app = create_app()
    routes = list(iter_effective_routes(app))
    paths = [r.path for r in routes]
    assert any("/api/internal/agent/scan-batches" in p for p in paths), f"agent_scan_batches.router not registered in create_app(); paths={paths}"

    # Also confirm the PATCH operation has the right method binding.
    matching = [r for r in routes if "/api/internal/agent/scan-batches" in r.path]
    assert any("PATCH" in getattr(r, "methods", set()) for r in matching), "No PATCH method bound on the scan-batches route"


# ---------------------------------------------------------------------------
# phaze-bnvx: concurrent PATCH must not bypass the terminal/state-machine guards.
#
# The hermetic ``session`` fixture binds every session to ONE connection inside a
# single uncommitted outer transaction, so it cannot exercise a real row lock
# between two genuinely concurrent transactions. This test therefore stands up a
# dedicated NullPool engine against the same isolated test database (mirroring
# tests/review/routers/test_agent_execution.py::test_concurrent_patch_does_not_regress_terminal_status,
# phaze-6zxs), commits a RUNNING row, and races two terminal PATCHes (completed vs
# failed) each on its OWN connection. With the ``session.get(..., with_for_update=True)``
# load the two PATCHes serialize on the row lock: the loser blocks until the winner
# commits, then re-reads the committed terminal status and gets 409 instead of
# silently overwriting it. Without the lock both reads observe the same stale
# RUNNING snapshot and the last commit wins blindly -- the regression this bead closes.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_terminal_patches_serialize_on_the_row_lock(async_engine) -> None:  # type: ignore[no-untyped-def]
    """Two concurrent terminal PATCHes (completed vs failed) never both apply (phaze-bnvx).

    Takes the session-scoped ``async_engine`` fixture purely so the schema exists on the isolated
    test database (this test builds its own committed rows on that same NullPool engine so the two
    racing PATCHes run on genuinely separate connections -- the hermetic ``session`` fixture binds
    everything to ONE connection and cannot exercise a real row lock).
    """
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    engine = async_engine
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    raw_token = "phaze_agent_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    agent_id = "g02-conc-scan-agent"
    batch_id = uuid.uuid4()

    async def _override_session():  # type: ignore[no-untyped-def]
        async with factory() as s:
            yield s

    app = FastAPI(title="conc-agent-scan-batches", version="test")
    app.include_router(agent_scan_batches.router)
    app.dependency_overrides[get_session] = _override_session

    async def _reset_to_running() -> None:
        async with factory() as s:
            row = await s.get(ScanBatch, batch_id)
            row.status = ScanStatus.RUNNING.value
            row.completed_at = None
            row.error_message = None
            await s.commit()

    try:
        async with factory() as s:
            s.add(Agent(id=agent_id, name=agent_id, token_hash=token_hash, scan_roots=["/test/music"]))
            s.add(
                ScanBatch(
                    id=batch_id,
                    agent_id=agent_id,
                    scan_path="/test/music/conc",
                    status=ScanStatus.RUNNING.value,
                    total_files=0,
                    processed_files=0,
                )
            )
            await s.commit()

        headers = {"Authorization": f"Bearer {raw_token}"}
        url = f"/api/internal/agent/scan-batches/{batch_id}"
        # Repeat rounds to make a stale-read interleaving overwhelmingly likely if the lock is absent.
        for _ in range(15):
            await _reset_to_running()
            async with (
                AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac_a,
                AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac_b,
            ):
                r_a, r_b = await asyncio.gather(
                    ac_a.patch(url, json={"status": "completed"}),
                    ac_b.patch(url, json={"status": "failed", "error_message": "conc race"}),
                    return_exceptions=False,
                )
            statuses = {r_a.status_code, r_b.status_code}
            # Exactly one PATCH applies (200); the loser re-reads the committed terminal
            # status and is refused with 409 -- never both 200 (that would mean the second
            # commit silently overwrote the first's terminal outcome).
            assert statuses <= {200, 409}, (r_a.status_code, r_b.status_code)
            assert 200 in statuses and 409 in statuses, f"both PATCHes did not resolve to exactly one winner: {r_a.status_code=} {r_b.status_code=}"

            async with factory() as s:
                row = await s.get(ScanBatch, batch_id)
                assert row.status in (ScanStatus.COMPLETED.value, ScanStatus.FAILED.value)
                # The row's status matches whichever response returned 200 -- confirms the
                # 409 loser never silently applied its own field mutations on top.
                winner_status = "completed" if r_a.status_code == 200 else "failed"
                assert row.status == winner_status, (
                    f"terminal outcome regressed under concurrency: row.status={row.status!r}, winner was {winner_status!r}"
                )
    finally:
        async with factory() as s:
            batch = await s.get(ScanBatch, batch_id)
            if batch is not None:
                await s.delete(batch)
            ag = await s.get(Agent, agent_id)
            if ag is not None:
                await s.delete(ag)
            await s.commit()
        # NOTE: do NOT dispose ``engine`` -- it is the session-scoped ``async_engine`` fixture,
        # owned (and disposed) by conftest.
