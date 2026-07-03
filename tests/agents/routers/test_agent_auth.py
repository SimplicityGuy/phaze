"""AUTH-01 + AUTH-04 + OpenAPI bearer-scheme tests for the agent auth dep.

These tests exercise ``phaze.routers.agent_auth.get_authenticated_agent`` via a
self-contained smoke FastAPI app so they don't depend on Plans 03-05 landing
first. Test 6 (OpenAPI) hits the same smoke app.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import update
from sqlalchemy.sql import func as sa_func

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a small FastAPI app that wires the auth dep onto a /smoke route.

    Why: Plans 03-05 land the real routers in later waves; this Plan-02 test
    suite is parallel-safe and does not depend on those routers existing yet.
    """
    app = FastAPI(title="smoke", version="test")

    @app.get("/smoke")
    async def smoke(agent: Annotated[Agent, Depends(get_authenticated_agent)]) -> dict[str, str]:
        return {"agent_id": agent.id}

    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest.mark.asyncio
async def test_missing_header_returns_401(session: AsyncSession) -> None:
    """AUTH-01 (1/4): no Authorization header -> 401 + WWW-Authenticate: Bearer (RFC 6750)."""
    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/smoke")
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_malformed_header_returns_401(session: AsyncSession) -> None:
    """AUTH-01 (2/4): non-Bearer scheme -> 401."""
    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/smoke", headers={"Authorization": "Token notabearer"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_unknown_token_returns_403(session: AsyncSession) -> None:
    """AUTH-01 (3/4): well-formed bearer not in agents.token_hash -> 403."""
    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get(
            "/smoke",
            headers={"Authorization": "Bearer phaze_agent_definitely_not_in_db_12345"},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


@pytest.mark.asyncio
async def test_revoke_blocks_next_call(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """AUTH-04 (1/2): setting revoked_at mid-test -> next call returns 403 (no restart)."""
    agent, raw_token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        # Sanity: first call succeeds
        r1 = await ac.get("/smoke")
        assert r1.status_code == 200
        assert r1.json() == {"agent_id": agent.id}

        # Revoke mid-test
        await session.execute(update(Agent).where(Agent.id == agent.id).values(revoked_at=sa_func.now()))
        await session.commit()

        # Same bearer, next call -> 403 (the dep does a fresh SELECT every request; partial index predicate matches)
        r2 = await ac.get("/smoke")
        assert r2.status_code == 403
        assert r2.json() == {"detail": "Forbidden"}


@pytest.mark.asyncio
async def test_new_token_authenticates(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """AUTH-04 (2/2): a NEW agent + NEW token_hash authenticates cleanly."""
    import hashlib
    import secrets as secrets_mod

    # Insert a second agent with a new token
    new_raw_token = "phaze_agent_" + secrets_mod.token_urlsafe(32)
    new_token_hash = hashlib.sha256(new_raw_token.encode("utf-8")).hexdigest()
    second_agent = Agent(
        id="test-agent-02",
        name="test-agent-02",
        token_hash=new_token_hash,
        scan_roots=["/test/music2"],
    )
    session.add(second_agent)
    await session.commit()
    await session.refresh(second_agent)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {new_raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.get("/smoke")

    assert response.status_code == 200
    assert response.json() == {"agent_id": "test-agent-02"}  # NOT the seed_test_agent's id


@pytest.mark.asyncio
async def test_openapi_bearer_scheme(session: AsyncSession) -> None:
    """OpenAPI: components.securitySchemes.bearerAuth has type=http, scheme=bearer (auto-emitted)."""
    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/openapi.json")
    assert response.status_code == 200
    schemas = response.json()["components"]["securitySchemes"]
    assert "bearerAuth" in schemas, f"Expected `bearerAuth` in securitySchemes; got {sorted(schemas)}"
    assert schemas["bearerAuth"]["type"] == "http"
    assert schemas["bearerAuth"]["scheme"] == "bearer"
