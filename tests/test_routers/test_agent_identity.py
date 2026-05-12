"""Contract tests for GET /api/internal/agent/whoami (Phase 26 D-15..D-17).

Uses the per-router smoke-app pattern from Phase 25 (test_agent_metadata.py:30-38)
to decouple from main.py's create_app() wiring (which Plan 12 will update).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers import agent_identity


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Self-contained app exposing only the agent_identity router."""
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_identity.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _make_client(session: AsyncSession, token: str | None = None) -> AsyncClient:
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def test_whoami_happy_path(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> None:
    """Authenticated GET /whoami returns 200 + AgentIdentity body matching the seeded agent."""
    agent, raw_token = seed_test_agent
    async with _make_client(session, raw_token) as ac:
        response = await ac.get("/api/internal/agent/whoami")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == agent.id
    assert body["name"] == agent.name
    assert body["scan_roots"] == agent.scan_roots
    parsed = datetime.fromisoformat(body["created_at"])
    assert parsed.tzinfo is not None


async def test_whoami_missing_header_returns_401(session: AsyncSession) -> None:
    """No Authorization header -> 401."""
    async with _make_client(session, token=None) as ac:
        response = await ac.get("/api/internal/agent/whoami")
    assert response.status_code == 401
    assert "bearer" in response.headers.get("WWW-Authenticate", "").lower()


async def test_whoami_unknown_token_returns_403(session: AsyncSession) -> None:
    """Well-formed bearer with unknown hash -> 403."""
    async with _make_client(session, token="phaze_agent_unknown-token-string-1234") as ac:  # noqa: S106
        response = await ac.get("/api/internal/agent/whoami")
    assert response.status_code == 403


async def test_whoami_revoked_token_returns_403(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """Revoking an agent immediately blocks future /whoami calls (AUTH-04)."""
    agent, raw_token = seed_test_agent
    async with _make_client(session, raw_token) as ac:
        pre = await ac.get("/api/internal/agent/whoami")
        assert pre.status_code == 200
        # Revoke mid-session
        await session.execute(update(Agent).where(Agent.id == agent.id).values(revoked_at=datetime.now(UTC)))
        await session.commit()
        post = await ac.get("/api/internal/agent/whoami")
    assert post.status_code == 403
