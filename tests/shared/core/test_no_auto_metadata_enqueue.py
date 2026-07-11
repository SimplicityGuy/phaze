"""MANUAL-META (D-06) regression guard: metadata extraction is operator-triggered ONLY.

The agent file-upsert path (``routers/agent_files.py``) once auto-enqueued the
metadata-extraction task: a freshly-INSERTed music/video row used to fire
``extract_file_metadata`` onto the per-agent queue. Phase 35 removed that auto-enqueue.

This test pins the absence of that enqueue so a future change cannot silently restore
auto-extraction (which would re-introduce the un-throttled extraction churn D-06 removed).

The legacy ingestion-scan (``services/ingestion.py::run_scan``) sibling guard was removed
in Phase 89 (LEGACY-01) together with ``services/ingestion.py`` itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio

from phaze.database import get_session
from phaze.routers import agent_files


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _make_record(path: str = "/test/music/a.mp3", ext: str = "mp3", size: int = 100) -> dict[str, object]:
    return {
        "sha256_hash": "0" * 64,
        "original_path": path,
        "original_filename": path.rsplit("/", 1)[-1],
        "current_path": path,
        "file_type": ext,
        "file_size": size,
    }


@pytest_asyncio.fixture
async def upsert_client_and_router(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> AsyncGenerator[tuple[AsyncClient, AsyncMock]]:
    """Smoke app mounting the real agent_files handler with a mock task_router.

    Mirrors ``tests/test_routers/test_agent_files.py``'s smoke-app fixture: pre-seeds the
    agent's LIVE sentinel batch so the absent-batch_id branch resolves, and exposes the
    ``app.state.task_router`` mock so the test can assert it is NEVER awaited (D-06).
    """
    from phaze.models.scan_batch import ScanBatch, ScanStatus

    agent, raw_token = seed_test_agent
    session.add(
        ScanBatch(
            agent_id=agent.id,
            scan_path="<watcher>",
            status=ScanStatus.LIVE.value,
            total_files=0,
            processed_files=0,
        ),
    )
    await session.commit()

    app = FastAPI(title="no-auto-enqueue-smoke", version="test")
    app.include_router(agent_files.router)
    app.dependency_overrides[get_session] = lambda: session
    mock_router = AsyncMock()
    app.state.task_router = mock_router

    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        yield ac, mock_router


@pytest.mark.asyncio
async def test_agent_upsert_does_not_enqueue_metadata(
    upsert_client_and_router: tuple[AsyncClient, AsyncMock],
) -> None:
    """An INSERT via the agent file-upsert endpoint must NOT enqueue metadata extraction."""
    client, mock_router = upsert_client_and_router
    chunk = {"files": [_make_record(path="/test/music/a.mp3"), _make_record(path="/test/music/b.mp3")]}
    response = await client.post("/api/internal/agent/files", json=chunk)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["inserted"] == 2
    assert body["enqueued"] == 0
    # The hard guard: the per-agent router's enqueue path was never touched.
    mock_router.enqueue_for_agent.assert_not_awaited()
