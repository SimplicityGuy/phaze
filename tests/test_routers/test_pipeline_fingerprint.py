"""Tests for fingerprint pipeline endpoints."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


class _FakeQueue:
    """A fake SAQ queue that records every enqueue against a shared capture list."""

    def __init__(self, name: str, capture: list[tuple[str, str, dict]]) -> None:
        self.name = name
        self._capture = capture

    async def enqueue(self, task_name: str, **kwargs: object) -> None:
        self._capture.append((self.name, task_name, dict(kwargs)))


class _FakeTaskRouter:
    """A fake AgentTaskRouter: ``queue_for`` yields a ``phaze-agent-<id>`` queue."""

    def __init__(self, capture: list[tuple[str, str, dict]]) -> None:
        self._capture = capture

    def queue_for(self, agent_id: str) -> _FakeQueue:
        return _FakeQueue(f"phaze-agent-{agent_id}", self._capture)


def _wire_fakes(client: AsyncClient) -> list[tuple[str, str, dict]]:
    """Attach fake controller_queue + task_router; return the shared capture list."""
    capture: list[tuple[str, str, dict]] = []
    state = client._transport.app.state  # type: ignore[union-attr]
    state.controller_queue = _FakeQueue("controller", capture)
    state.task_router = _FakeTaskRouter(capture)
    return capture


async def _seed_active_agent(session: AsyncSession, *, agent_id: str = "nox") -> Agent:
    """Seed one active (non-revoked, recently-seen) agent so select_active_agent resolves it."""
    agent = Agent(id=agent_id, name=agent_id, scan_roots=[], last_seen_at=datetime.now(UTC))
    session.add(agent)
    await session.commit()
    return agent


async def _drain_background() -> None:
    """Yield until the router's background enqueue tasks have drained."""
    import phaze.routers.pipeline as pipeline_mod

    for _ in range(500):
        if not pipeline_mod._background_tasks:
            return
        await asyncio.sleep(0)


def _make_file(*, state: str = FileState.METADATA_EXTRACTED) -> FileRecord:
    """Create a FileRecord with the given state."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=state,
    )


@pytest.mark.asyncio
async def test_trigger_fingerprint_enqueues_eligible(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/fingerprint enqueues fingerprint_file onto phaze-agent-nox (not default)."""
    session.add_all([_make_file(state=FileState.METADATA_EXTRACTED) for _ in range(3)])
    await session.commit()
    await _seed_active_agent(session)
    capture = _wire_fakes(client)

    response = await client.post("/api/v1/fingerprint")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 3

    await _drain_background()
    assert len(capture) == 3
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-nox", "fingerprint_file")}
    assert all(q != "default" for q, _, _ in capture)


@pytest.mark.asyncio
async def test_trigger_fingerprint_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/fingerprint with files but no active agent surfaces a visible empty-state."""
    session.add_all([_make_file(state=FileState.METADATA_EXTRACTED) for _ in range(3)])
    await session.commit()
    capture = _wire_fakes(client)  # no active agent seeded

    response = await client.post("/api/v1/fingerprint")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0
    assert "no active agent" in data["message"].lower()

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_trigger_fingerprint_no_eligible(client: AsyncClient) -> None:
    """POST /api/v1/fingerprint returns 0 when no eligible files exist."""
    response = await client.post("/api/v1/fingerprint")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0


@pytest.mark.asyncio
async def test_fingerprint_progress_returns_counts(client: AsyncClient, session: AsyncSession) -> None:
    """GET /api/v1/fingerprint/progress returns total/completed/failed counts."""
    # 2 files in METADATA_EXTRACTED (eligible, not yet done)
    f1 = _make_file(state=FileState.METADATA_EXTRACTED)
    f2 = _make_file(state=FileState.METADATA_EXTRACTED)
    # 1 file in FINGERPRINTED (completed)
    f3 = _make_file(state=FileState.FINGERPRINTED)
    session.add_all([f1, f2, f3])
    await session.flush()

    # Add a failed fingerprint result
    session.add(FingerprintResult(file_id=f1.id, engine="audfprint", status="failed", error_message="timeout"))
    await session.commit()

    response = await client.get("/api/v1/fingerprint/progress")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert data["completed"] == 1
    assert data["failed"] == 1


@pytest.mark.asyncio
async def test_pipeline_stats_include_fingerprinted(client: AsyncClient, session: AsyncSession) -> None:
    """Pipeline stats include FINGERPRINTED stage."""
    session.add(_make_file(state=FileState.FINGERPRINTED))
    await session.commit()

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    assert "Fingerprinted" in response.text
