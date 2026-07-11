"""Contract tests for PATCH /api/internal/agent/proposals/{id}/state (Phase 26 D-28)."""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers import agent_proposals


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_proposals.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _make_client(session: AsyncSession, token: str | None = None) -> AsyncClient:
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_file_and_proposal(
    session: AsyncSession,
    agent_id: str,
    proposal_status: ProposalStatus = ProposalStatus.APPROVED,
    file_state: FileState = FileState.APPROVED,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a FileRecord + RenameProposal pair. Returns (file_id, proposal_id)."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        id=file_id,
        sha256_hash="0" * 64,
        original_path=f"/orig/track-{file_id}.mp3",
        original_filename="track.mp3",
        current_path=f"/orig/track-{file_id}.mp3",
        file_type="mp3",
        file_size=1024,
        state=file_state,
        agent_id=agent_id,
    )
    proposal_id = uuid.uuid4()
    proposal = RenameProposal(
        id=proposal_id,
        file_id=file_id,
        proposed_filename="proposed.mp3",
        proposed_path="/new/proposed.mp3",
        confidence=0.9,
        status=proposal_status,
    )
    session.add(file_record)
    session.add(proposal)
    await session.commit()
    return file_id, proposal_id


async def test_executed_joint_update(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """APPROVED -> EXECUTED + file MOVED + current_path updated, all in one tx."""
    agent, raw_token = seed_test_agent
    file_id, proposal_id = await _seed_file_and_proposal(session, agent.id)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "executed", "file_state": "moved", "current_path": "/new/proposed.mp3"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal_state"] == "executed"
    assert body["file_state"] == "moved"
    assert body["current_path"] == "/new/proposed.mp3"
    # Verify DB state
    await session.commit()
    session.expire_all()
    p = (await session.execute(select(RenameProposal).where(RenameProposal.id == proposal_id))).scalar_one()
    f = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    assert p.status == ProposalStatus.EXECUTED.value
    # SIDECAR-03: the proposal->file.state cascade is gone. Positive guard -- state stays at the
    # seeded default (APPROVED), proving the cascade write is removed, not merely absent.
    assert f.state == FileState.APPROVED.value
    assert f.current_path == "/new/proposed.mp3"


async def test_failed_joint_update(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """APPROVED -> FAILED + file UNCHANGED (no current_path required for 'unchanged')."""
    agent, raw_token = seed_test_agent
    file_id, proposal_id = await _seed_file_and_proposal(session, agent.id)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "failed", "file_state": "unchanged", "error_message": "checksum mismatch"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal_state"] == "failed"
    assert body["file_state"] == "unchanged"
    await session.commit()
    session.expire_all()
    p = (await session.execute(select(RenameProposal).where(RenameProposal.id == proposal_id))).scalar_one()
    f = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    assert p.status == ProposalStatus.FAILED.value
    # SIDECAR-03: file.state stays at the seeded default (APPROVED); no cascade write.
    assert f.state == FileState.APPROVED.value


async def test_same_state_idempotent_no_op(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """PATCH executed -> executed twice -> both return 200, row stays EXECUTED."""
    agent, raw_token = seed_test_agent
    _, proposal_id = await _seed_file_and_proposal(session, agent.id, proposal_status=ProposalStatus.APPROVED)
    async with _make_client(session, raw_token) as ac:
        r1 = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "executed", "file_state": "moved", "current_path": "/p"},
        )
        r2 = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "executed"},
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    # SIDECAR-03: pure replay (no file_state in body) echoes file_state=None; no file.state read.
    assert r2.json()["file_state"] is None
    # Row stays EXECUTED
    await session.commit()
    session.expire_all()
    p = (await session.execute(select(RenameProposal).where(RenameProposal.id == proposal_id))).scalar_one()
    assert p.status == ProposalStatus.EXECUTED.value


async def test_illegal_transition_409(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """EXECUTED -> FAILED is illegal -> 409."""
    agent, raw_token = seed_test_agent
    _, proposal_id = await _seed_file_and_proposal(session, agent.id, proposal_status=ProposalStatus.EXECUTED)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "failed"},
        )
    assert r.status_code == 409
    assert "illegal transition" in r.text.lower() or "executed -> failed" in r.text.lower()


async def test_pending_to_executed_409(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """PENDING is NOT in _PROPOSAL_TRANSITIONS allowed-from set -> 409."""
    agent, raw_token = seed_test_agent
    _, proposal_id = await _seed_file_and_proposal(session, agent.id, proposal_status=ProposalStatus.PENDING)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "executed", "file_state": "moved", "current_path": "/x"},
        )
    assert r.status_code == 409


async def test_proposal_not_found_404(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """Unknown proposal_id -> 404."""
    _, raw_token = seed_test_agent
    unknown_id = uuid.uuid4()
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/proposals/{unknown_id}/state",
            json={"proposal_state": "executed", "file_state": "moved", "current_path": "/x"},
        )
    assert r.status_code == 404


async def test_proposal_extra_field_422(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """extra='forbid' rejects unknown fields."""
    agent, raw_token = seed_test_agent
    _, proposal_id = await _seed_file_and_proposal(session, agent.id)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "executed", "agent_id": "spoofed"},
        )
    assert r.status_code == 422


async def test_moved_without_current_path_422(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """ProposalStatePatch._require_path_when_moved validator -> 422 when current_path missing."""
    agent, raw_token = seed_test_agent
    _, proposal_id = await _seed_file_and_proposal(session, agent.id)
    async with _make_client(session, raw_token) as ac:
        r = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "executed", "file_state": "moved"},
        )
    assert r.status_code == 422
    assert "current_path" in r.text.lower()


async def test_proposal_cross_agent_403(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """W1 / T-26-08-S2: agent B cannot mutate a proposal whose file belongs to agent A."""
    # Seed Agent A + a proposal/file owned by A
    agent_a, _ = seed_test_agent
    _, proposal_id = await _seed_file_and_proposal(session, agent_a.id)

    # Seed a SECOND agent (B) inline, matching conftest.seed_test_agent's pattern.
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
        r = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "executed", "file_state": "moved", "current_path": "/p"},
        )
    assert r.status_code == 403
    assert "does not belong" in r.text.lower() or "belong to authenticated" in r.text.lower()


async def test_proposal_missing_auth_returns_401(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    agent, _ = seed_test_agent
    _, proposal_id = await _seed_file_and_proposal(session, agent.id)
    async with _make_client(session, token=None) as ac:
        r = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "executed", "file_state": "moved", "current_path": "/x"},
        )
    assert r.status_code == 401


async def test_proposal_unknown_token_returns_403(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    agent, _ = seed_test_agent
    _, proposal_id = await _seed_file_and_proposal(session, agent.id)
    async with _make_client(session, token="phaze_agent_unknown-token-1234") as ac:  # noqa: S106
        r = await ac.patch(
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json={"proposal_state": "executed", "file_state": "moved", "current_path": "/x"},
        )
    assert r.status_code == 403
