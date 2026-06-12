"""Tests for the scan API endpoints."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze.models.scan_batch import ScanBatch, ScanStatus
from tests._queue_fakes import FakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


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
    await seed_active_agent(session)
    client._transport.app.state.task_router = FakeTaskRouter()  # type: ignore[union-attr]

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
    await seed_active_agent(session)
    client._transport.app.state.task_router = FakeTaskRouter()  # type: ignore[union-attr]

    response = await client.post("/api/v1/scan", json={"path": str(tmp_path)})

    assert response.status_code == 200
    data = response.json()
    assert "batch_id" in data


@pytest.mark.asyncio
async def test_trigger_scan_does_not_auto_enqueue_extract(
    client: AsyncClient,
    session: AsyncSession,
    async_engine,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MANUAL-META (D-06): a legacy scan discovers + persists rows but NEVER auto-enqueues
    ``extract_file_metadata``. Metadata extraction is operator-triggered only.

    This is the endpoint-level guard (full HTTP path: trigger_scan -> run_scan) that
    complements the unit-level guard in ``tests/test_no_auto_metadata_enqueue.py``. Before
    Phase 35 this path fired ``extract_file_metadata`` per discovered MUSIC/VIDEO record;
    the removal must hold at the API boundary too.
    """
    from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession, async_sessionmaker

    # A real audio file so discovery classifies it MUSIC -- the pre-D-06 trigger for the
    # (now removed) per-record enqueue loop. If auto-enqueue ever returns, this catches it.
    (tmp_path / "song.mp3").write_bytes(b"\x00fake-mp3-bytes")

    await seed_active_agent(session, "nox")
    router = FakeTaskRouter()
    client._transport.app.state.task_router = router  # type: ignore[union-attr]

    # run_scan opens its own session via phaze.routers.scan.async_session (the prod
    # factory). Point it at the test engine so discovery runs against the same database
    # the fixtures seeded.
    test_factory = async_sessionmaker(async_engine, class_=SAAsyncSession, expire_on_commit=False)
    monkeypatch.setattr("phaze.routers.scan.async_session", test_factory)

    response = await client.post("/api/v1/scan", json={"path": str(tmp_path)})
    assert response.status_code == 200

    await _drain_background_scans()

    # The hard guard: discovery persisted the row but enqueued no metadata extraction.
    assert not any(task == "extract_file_metadata" for _, task, _ in router.captures), (
        f"D-06 violation: scan auto-enqueued extract_file_metadata: {router.captures}"
    )
    # And nothing leaked onto the removed consumer-less default queue.
    assert all(name != "default" for name, _, _ in router.captures)


@pytest.mark.asyncio
async def test_trigger_scan_no_active_agent_returns_503(
    client: AsyncClient,
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    """With no active agent, POST /api/v1/scan is a visible 503 and captures no enqueue."""
    # Only the conftest LEGACY agent exists (last_seen_at IS NULL -> not selectable).
    router = FakeTaskRouter()
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
