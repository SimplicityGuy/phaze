"""MANUAL-META (D-06) regression guards: metadata extraction is operator-triggered ONLY.

Two paths previously auto-enqueued the metadata-extraction task and both have been removed
in Phase 35:

1. Agent file-upsert (``routers/agent_files.py``): a freshly-INSERTed music/video row used
   to fire ``extract_file_metadata`` onto the per-agent queue. It must NOT anymore.
2. Legacy ingestion scan (``services/ingestion.py::run_scan``): discovery used to enqueue
   ``extract_file_metadata`` per MUSIC/VIDEO record. It must NOT anymore.

These tests pin the absence of those enqueues so a future change cannot silently restore
auto-extraction (which would re-introduce the un-throttled extraction churn D-06 removed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio

from phaze.database import get_session
from phaze.routers import agent_files
from phaze.services.ingestion import run_scan


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


async def test_legacy_scan_does_not_enqueue_metadata() -> None:
    """``run_scan`` discovery must NOT enqueue metadata extraction (D-06), even music/video."""
    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    # A queue that records any enqueue; the test asserts it is never called.
    mock_queue = AsyncMock()

    mock_records = [
        {
            "id": uuid.uuid4(),
            "file_type": "mp3",
            "sha256_hash": "a" * 64,
            "original_path": "/a.mp3",
            "original_filename": "a.mp3",
            "current_path": "/a.mp3",
            "file_size": 100,
            "state": "discovered",
            "batch_id": None,
        },
        {
            "id": uuid.uuid4(),
            "file_type": "mp4",
            "sha256_hash": "b" * 64,
            "original_path": "/b.mp4",
            "original_filename": "b.mp4",
            "current_path": "/b.mp4",
            "file_size": 200,
            "state": "discovered",
            "batch_id": None,
        },
    ]

    batch_id = uuid.uuid4()
    with (
        patch("phaze.services.ingestion.discover_and_hash_files", return_value=mock_records),
        patch("phaze.services.ingestion.bulk_upsert_files", new_callable=AsyncMock, return_value=len(mock_records)),
    ):
        # Pass a queue even though it should be ignored -- proves the removal, not just a None guard.
        await run_scan("/fake/path", batch_id, mock_session_factory, queue=mock_queue)

    mock_queue.enqueue.assert_not_called()
