"""Tests for the scan API endpoints."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch, ScanStatus


if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Test doubles for the per-agent enqueue routing (Phase 30 Plan 04)
# ---------------------------------------------------------------------------


class _CaptureQueue:
    """A SAQ-queue stand-in that records every enqueue as (queue_name, task, kwargs)."""

    def __init__(self, name: str, captures: list[tuple[str, str, dict[str, Any]]]) -> None:
        self.name = name
        self._captures = captures

    async def enqueue(self, task_name: str, **kwargs: Any) -> None:
        self._captures.append((self.name, task_name, kwargs))


class _CaptureRouter:
    """AgentTaskRouter stand-in: ``queue_for(id)`` returns a named capture queue.

    All capture queues share one ``captures`` list so a test can assert the exact
    ``(queue_name, task_name, kwargs)`` tuples enqueued across the run.
    """

    def __init__(self) -> None:
        self.captures: list[tuple[str, str, dict[str, Any]]] = []
        self._queues: dict[str, _CaptureQueue] = {}

    def queue_for(self, agent_id: str) -> _CaptureQueue:
        name = f"phaze-agent-{agent_id}"
        if name not in self._queues:
            self._queues[name] = _CaptureQueue(name, self.captures)
        return self._queues[name]


async def _seed_active_agent(session: AsyncSession, agent_id: str = "nox") -> Agent:
    """Insert a non-revoked agent with a recent ``last_seen_at`` (selectable as active)."""
    agent = Agent(id=agent_id, name=agent_id, scan_roots=[], last_seen_at=datetime.now(UTC))
    session.add(agent)
    await session.commit()
    return agent


async def _drain_background_scans() -> None:
    """Await every outstanding background scan task until the registry empties.

    ``trigger_scan`` fires ``run_scan`` via ``asyncio.create_task`` and tracks it in
    the module-level ``_background_tasks`` set (cleared by a done-callback). Draining
    here lets the auto-enqueue loop run before assertions.
    """
    from phaze.routers import scan as scan_module

    while scan_module._background_tasks:
        await asyncio.gather(*list(scan_module._background_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_trigger_scan_returns_batch_id(
    client: AsyncClient,
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/v1/scan should return 200 with batch_id and message."""
    monkeypatch.setattr("phaze.routers.scan.settings.scan_path", str(tmp_path))
    monkeypatch.setattr("phaze.routers.scan.run_scan", AsyncMock())
    await _seed_active_agent(session)
    client._transport.app.state.task_router = _CaptureRouter()  # type: ignore[union-attr]

    response = await client.post("/api/v1/scan", json={})

    assert response.status_code == 200
    data = response.json()
    assert "batch_id" in data
    uuid.UUID(data["batch_id"])  # validates it's a proper UUID
    assert data["message"] == "Scan started"


@pytest.mark.asyncio
async def test_trigger_scan_with_path_override(
    client: AsyncClient,
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/v1/scan with path override should use the provided path."""
    monkeypatch.setattr("phaze.routers.scan.run_scan", AsyncMock())
    await _seed_active_agent(session)
    client._transport.app.state.task_router = _CaptureRouter()  # type: ignore[union-attr]

    response = await client.post("/api/v1/scan", json={"path": str(tmp_path)})

    assert response.status_code == 200
    data = response.json()
    assert "batch_id" in data


@pytest.mark.asyncio
async def test_trigger_scan_enqueues_extract_on_active_agent_queue(
    client: AsyncClient,
    session: AsyncSession,
    async_engine,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy scan auto-enqueues extract_file_metadata onto phaze-agent-<id>, never default."""
    from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession, async_sessionmaker

    # A real audio file so discovery classifies it MUSIC and the enqueue loop fires.
    (tmp_path / "song.mp3").write_bytes(b"\x00fake-mp3-bytes")

    await _seed_active_agent(session, "nox")
    router = _CaptureRouter()
    client._transport.app.state.task_router = router  # type: ignore[union-attr]

    # run_scan opens its own session via phaze.routers.scan.async_session (the prod
    # factory). Point it at the test engine so discovery + the enqueue loop run
    # against the same database the fixtures seeded.
    test_factory = async_sessionmaker(async_engine, class_=SAAsyncSession, expire_on_commit=False)
    monkeypatch.setattr("phaze.routers.scan.async_session", test_factory)

    response = await client.post("/api/v1/scan", json={"path": str(tmp_path)})
    assert response.status_code == 200

    await _drain_background_scans()

    assert router.captures, "expected at least one enqueue from the discovered audio file"
    assert any(name == "phaze-agent-nox" and task == "extract_file_metadata" for name, task, _ in router.captures)
    assert all(name != "default" for name, _, _ in router.captures)


@pytest.mark.asyncio
async def test_trigger_scan_no_active_agent_returns_503(
    client: AsyncClient,
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    """With no active agent, POST /api/v1/scan is a visible 503 and captures no enqueue."""
    # Only the conftest LEGACY agent exists (last_seen_at IS NULL -> not selectable).
    router = _CaptureRouter()
    client._transport.app.state.task_router = router  # type: ignore[union-attr]

    response = await client.post("/api/v1/scan", json={"path": str(tmp_path)})

    assert response.status_code == 503
    assert "No active agent" in response.json()["detail"]
    assert router.captures == []


@pytest.mark.asyncio
async def test_trigger_scan_invalid_path(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/v1/scan with nonexistent path should return 400."""
    monkeypatch.setattr("phaze.routers.scan.run_scan", AsyncMock())

    response = await client.post("/api/v1/scan", json={"path": "/nonexistent/path"})

    assert response.status_code == 400
    assert "not a valid directory" in response.json()["detail"]


@pytest.mark.asyncio
async def test_trigger_scan_path_traversal(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/v1/scan with path traversal should return 400."""
    monkeypatch.setattr("phaze.routers.scan.run_scan", AsyncMock())

    response = await client.post("/api/v1/scan", json={"path": "/data/../etc/passwd"})

    assert response.status_code == 400
    assert "Path traversal" in response.json()["detail"]


@pytest.mark.asyncio
async def test_scan_status_not_found(client: AsyncClient) -> None:
    """GET /api/v1/scan/{batch_id} for unknown ID should return 404."""
    random_id = uuid.uuid4()

    response = await client.get(f"/api/v1/scan/{random_id}")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


@pytest.mark.asyncio
async def test_scan_status_found(client: AsyncClient, session: AsyncSession) -> None:
    """GET /api/v1/scan/{batch_id} for existing batch should return status info."""
    batch_id = uuid.uuid4()
    batch = ScanBatch(
        id=batch_id,
        scan_path="/data/music",
        status=ScanStatus.COMPLETED,
        total_files=42,
        processed_files=42,
    )
    session.add(batch)
    await session.commit()

    response = await client.get(f"/api/v1/scan/{batch_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["batch_id"] == str(batch_id)
    assert data["status"] == "completed"
    assert data["scan_path"] == "/data/music"
    assert data["total_files"] == 42
    assert data["processed_files"] == 42
    assert data["error_message"] is None
    assert "created_at" in data
