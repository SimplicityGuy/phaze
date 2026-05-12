"""Contract tests for PUT /api/internal/agent/analysis/{file_id} (Phase 26 D-26).

Mirrors `tests/test_routers/test_agent_metadata.py` exactly: smoke-app pattern,
seed FileRecord for FK satisfaction, expire_all to bypass session cache between
PUTs. Covers happy path, idempotent replay, partial-PUT field-level LWW (CR-01
invariant), empty-body no-op for existing rows, first-PUT-with-empty-body
creates a row, 422 on extra fields (D-16 / AUTH-01 spoof block), and the auth
401/403 surface from `Depends(get_authenticated_agent)`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.routers.agent_analysis import router as agent_analysis_router


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a small FastAPI app that wires the agent_analysis router.

    Tests are parallel-safe and decoupled from Plan 12's main.py wiring.
    """
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_analysis_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _make_client(session: AsyncSession, token: str | None = None) -> AsyncClient:
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
    """Seed a FileRecord so AnalysisResult.file_id FK (files.id) is satisfied."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash="0" * 64,
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
            state=FileState.DISCOVERED,
        )
    )
    await session.commit()
    return file_id


@pytest.mark.asyncio
async def test_analysis_put_happy_path(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """PUT with full body creates AnalysisResult row and returns 200."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={
                "bpm": 128.5,
                "musical_key": "C# minor",
                "mood": {"happy": 0.7, "energetic": 0.8},
                "style": {"electronic": 0.9, "house": 0.6},
                "danceability": 0.85,
                "energy": 0.92,
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == agent.id
    assert body["file_id"] == str(file_id)

    result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    row = result.scalar_one()
    assert row.bpm == 128.5
    assert row.musical_key == "C# minor"
    # PK regression guard: AnalysisResult.id has a Python-only default, the router
    # must stamp `payload["id"] = uuid.uuid4()` so pg_insert doesn't fail NOT NULL.
    assert row.id is not None
    assert isinstance(row.id, uuid.UUID)
    # Storage conversion: dict[str, float] -> summary string for mood/style (String(50) columns).
    assert row.mood is not None
    assert "energetic=0.80" in row.mood
    assert "happy=0.70" in row.mood
    assert row.style is not None
    assert "electronic=0.90" in row.style
    assert "house=0.60" in row.style


@pytest.mark.asyncio
async def test_analysis_put_replay_idempotent(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """PUT twice with same payload -> 1 row in DB (D-26 idempotent upsert)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    payload = {"bpm": 120.0, "musical_key": "G major"}

    async with _make_client(session, raw_token) as ac:
        r1 = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=payload)
        r2 = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=payload)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    session.expire_all()
    result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].bpm == 120.0
    assert rows[0].musical_key == "G major"


@pytest.mark.asyncio
async def test_analysis_partial_put_preserves_other_fields(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """CR-01 invariant: partial PUT only updates the fields the caller set."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r_full = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={
                "bpm": 128.0,
                "musical_key": "A minor",
                "danceability": 0.8,
                "energy": 0.9,
            },
        )
        r_partial = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={"bpm": 130.0},  # only bpm
        )

    assert r_full.status_code == 200, r_full.text
    assert r_partial.status_code == 200, r_partial.text

    session.expire_all()
    result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    row = result.scalar_one()
    assert row.bpm == 130.0, "partial PUT failed to update bpm"
    assert row.musical_key == "A minor", f"CR-01 regression: musical_key was clobbered to {row.musical_key!r}"
    assert row.danceability == 0.8, f"CR-01 regression: danceability was clobbered to {row.danceability!r}"
    assert row.energy == 0.9, f"CR-01 regression: energy was clobbered to {row.energy!r}"


@pytest.mark.asyncio
async def test_analysis_empty_put_is_noop_for_existing_row(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Empty body PUT against existing row: 200 + row preserved (ON CONFLICT DO NOTHING branch)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r_seed = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={"bpm": 100.0, "musical_key": "F"},
        )
        r_empty = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={})

    assert r_seed.status_code == 200, r_seed.text
    assert r_empty.status_code == 200, r_empty.text

    session.expire_all()
    result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    row = result.scalar_one()
    assert row.bpm == 100.0
    assert row.musical_key == "F"


@pytest.mark.asyncio
async def test_analysis_first_put_with_empty_body_creates_row(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Empty body PUT on a brand-new file_id creates a row with all fields NULL."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={})

    assert r.status_code == 200, r.text
    session.expire_all()
    result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    row = result.scalar_one()
    assert row.bpm is None
    assert row.musical_key is None
    assert row.mood is None
    assert row.style is None
    assert row.danceability is None
    assert row.energy is None


@pytest.mark.asyncio
async def test_analysis_extra_field_422(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """D-16: extra='forbid' rejects unknown fields (AUTH-01 -- no agent_id forgery)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={"bpm": 120.0, "agent_id": "spoofed-agent"},
        )

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc")) == ["body", "agent_id"] for e in errors), errors


@pytest.mark.asyncio
async def test_analysis_missing_auth_returns_401(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """No Authorization header -> 401 (HTTPBearer auto_error)."""
    agent, _ = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, token=None) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 120.0})

    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analysis_unknown_token_returns_403(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """Well-formed bearer with unknown hash -> 403 (auth dep doesn't leak agent existence)."""
    agent, _ = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, token="phaze_agent_unknown-token-1234") as ac:  # noqa: S106 -- test fixture, not a real secret
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 120.0})

    assert r.status_code == 403
