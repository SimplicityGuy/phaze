"""Tests for the scan API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze.models.scan_batch import ScanBatch, ScanStatus


if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_trigger_scan_returns_batch_id(client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/v1/scan should return 200 with batch_id and message."""
    monkeypatch.setattr("phaze.routers.scan.settings.scan_path", str(tmp_path))
    monkeypatch.setattr("phaze.routers.scan.run_scan", AsyncMock())

    response = await client.post("/api/v1/scan", json={})

    assert response.status_code == 200
    data = response.json()
    assert "batch_id" in data
    uuid.UUID(data["batch_id"])  # validates it's a proper UUID
    assert data["message"] == "Scan started"


@pytest.mark.asyncio
async def test_trigger_scan_with_path_override(client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/v1/scan with path override should use the provided path."""
    monkeypatch.setattr("phaze.routers.scan.run_scan", AsyncMock())

    response = await client.post("/api/v1/scan", json={"path": str(tmp_path)})

    assert response.status_code == 200
    data = response.json()
    assert "batch_id" in data


@pytest.mark.asyncio
async def test_trigger_scan_invalid_path(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/v1/scan with nonexistent path should return 400."""
    monkeypatch.setattr("phaze.routers.scan.run_scan", AsyncMock())

    response = await client.post("/api/v1/scan", json={"path": "/nonexistent/path"})

    assert response.status_code == 400
    assert "not a valid directory" in response.json()["detail"]


@pytest.mark.asyncio
async def test_trigger_scan_path_traversal(client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
