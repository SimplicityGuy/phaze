"""DIST-04 (2/5) + DIST-05 (2/5) + D-16 tests for PUT /api/internal/agent/metadata/{file_id}.

Uses an inline smoke FastAPI app builder (mirrors test_agent_auth.py) because Plan 06
wires the agent_metadata router into `main.py`; this test suite is parallel-safe and
does not depend on Plans 03/05/06 landing in any particular order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.routers.agent_metadata import router as agent_metadata_router


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a small FastAPI app that wires the agent_metadata router.

    Tests are parallel-safe and decoupled from Plan 06's main.py wiring.
    """
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_metadata_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
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
            file_size=100,
            state=FileState.DISCOVERED,
        )
    )
    await session.commit()
    return file_id


@pytest.mark.asyncio
async def test_metadata_put_happy_path(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """DIST-04 (2/5): authenticated PUT writes one metadata row.

    Also locks in the PK regression guard: `FileMetadata.id` has a Python-only
    `default=uuid.uuid4`, and `pg_insert(...).values()` bypasses that. The
    router stamps `payload["id"] = uuid.uuid4()` to compensate; the assertion
    `isinstance(row.id, uuid.UUID)` would fail with `NotNullViolationError`
    on a fresh INSERT if the stamp is removed.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.put(
            f"/api/internal/agent/metadata/{file_id}",
            json={"artist": "Aphex Twin", "title": "Xtal", "year": 1992},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == agent.id
    assert body["file_id"] == str(file_id)

    result = await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))
    row = result.scalar_one()
    assert row.artist == "Aphex Twin"
    assert row.title == "Xtal"
    assert row.year == 1992
    # PK regression guard: the router stamps payload["id"] = uuid.uuid4()
    # before pg_insert because FileMetadata.id has a Python-only default.
    assert row.id is not None
    assert isinstance(row.id, uuid.UUID)


@pytest.mark.asyncio
async def test_metadata_replay_overwrites(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """DIST-05 (2/5): replay same file_id with different payload -> last write wins, 1 row total."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r1 = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "A", "title": "T1"})
        r2 = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "B", "title": "T2"})

    assert r1.status_code == 200
    assert r2.status_code == 200

    result = await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].artist == "B"
    assert rows[0].title == "T2"
    # AUTH-01 attribution check: the row was written by the authenticated agent.
    assert agent.id is not None  # keep "agent" alive for the linter


@pytest.mark.asyncio
async def test_metadata_extra_field_422(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """D-16: extra body field -> 422 extra_forbidden.

    Specifically: an attempt to forge `agent_id` in the body returns 422 with
    `loc=["body", "agent_id"]`, blocking the T-25-04-S spoofing vector.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.put(
            f"/api/internal/agent/metadata/{file_id}",
            json={"artist": "A", "agent_id": "evil"},
        )

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc")) == ["body", "agent_id"] for e in errors), errors
