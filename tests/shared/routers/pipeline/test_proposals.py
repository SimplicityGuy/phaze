"""Controller-side tests for `routers/pipeline/proposals.py` (split from test_pipeline.py, phaze-7l8jh).

POST /api/v1/proposals/generate and /pipeline/proposals -- `routers/pipeline/proposals.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.routers.pipeline._shared import (
    _cloud_compute_registry,  # noqa: F401 -- autouse fixture, never referenced by name
    _make_file_with_convergence,
    _reset_saq_jobs_minimal,
    drain_router_background_tasks,
    pytest,
    text,
    wire_fakes,
)


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_proposals_generate_batches(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/proposals/generate enqueues generate_proposals onto the controller queue."""
    files = []
    related = []
    for _ in range(15):
        file_rec, analysis, metadata = _make_file_with_convergence()
        files.append(file_rec)
        related.extend([analysis, metadata])
    session.add_all(files)
    await session.flush()
    session.add_all(related)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/api/v1/proposals/generate")
    assert response.status_code == 200
    data = response.json()
    assert data["total_files"] == 15
    assert data["enqueued_batches"] == 2  # 15 files / 10 batch_size = 2 batches

    await drain_router_background_tasks()
    assert len(capture) == 2
    assert {(q, t) for q, t, _ in capture} == {("controller", "generate_proposals")}


@pytest.mark.asyncio
async def test_proposals_generate_no_files(client: AsyncClient) -> None:
    """POST /api/v1/proposals/generate with no ANALYZED files returns zero counts."""
    response = await client.post("/api/v1/proposals/generate")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued_batches"] == 0
    assert data["total_files"] == 0


@pytest.mark.asyncio
async def test_proposals_generate_refuses_while_batch_in_flight(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-8qheu: a second trigger while a prior generate_proposals batch is queued/active is refused.

    The set-hash dedup key (``generate_proposals:<sha256(sorted file_ids)>``) is NOT robust to the
    pending set moving between two triggers (removing one file's proposal shifts every later chunk
    boundary), so a re-trigger must be REFUSED server-side instead of recomputing + re-enqueueing
    the pending backlog. Files that would otherwise batch stay untouched -- zero new enqueues.
    """
    files = []
    related = []
    for _ in range(5):
        file_rec, analysis, metadata = _make_file_with_convergence()
        files.append(file_rec)
        related.extend([analysis, metadata])
    session.add_all(files)
    await session.flush()
    session.add_all(related)
    await _reset_saq_jobs_minimal(session)
    await session.execute(text("INSERT INTO saq_jobs (key, status) VALUES (:key, 'active')"), {"key": "generate_proposals:already-in-flight"})
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/api/v1/proposals/generate")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued_batches"] == 0
    assert data["total_files"] == 0
    assert "already in progress" in data["message"]

    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_trigger_proposals_ui_with_files(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/proposals enqueues generate_proposals onto the controller queue."""
    files = []
    related = []
    for _ in range(5):
        file_rec, analysis, metadata = _make_file_with_convergence()
        files.append(file_rec)
        related.extend([analysis, metadata])
    session.add_all(files)
    await session.flush()
    session.add_all(related)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/pipeline/proposals")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "proposal generation" in response.text

    await drain_router_background_tasks()
    assert len(capture) == 1
    assert {(q, t) for q, t, _ in capture} == {("controller", "generate_proposals")}


@pytest.mark.asyncio
async def test_trigger_proposals_ui_no_files(client: AsyncClient) -> None:
    """POST /pipeline/proposals with no ANALYZED files returns HTML with zero count."""
    response = await client.post("/pipeline/proposals")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_trigger_proposals_ui_refuses_while_batch_in_flight(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-8qheu: the HTMX trigger twin also refuses a re-trigger while a batch is queued/active."""
    files = []
    related = []
    for _ in range(5):
        file_rec, analysis, metadata = _make_file_with_convergence()
        files.append(file_rec)
        related.extend([analysis, metadata])
    session.add_all(files)
    await session.flush()
    session.add_all(related)
    await _reset_saq_jobs_minimal(session)
    await session.execute(text("INSERT INTO saq_jobs (key, status) VALUES (:key, 'queued')"), {"key": "generate_proposals:already-in-flight"})
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/pipeline/proposals")
    assert response.status_code == 200
    assert "already in progress" in response.text

    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_enqueue_proposals_background(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/proposals/generate enqueues batched jobs in background."""
    files = []
    related = []
    for _ in range(5):
        file_rec, analysis, metadata = _make_file_with_convergence()
        files.append(file_rec)
        related.extend([analysis, metadata])
    session.add_all(files)
    await session.flush()
    session.add_all(related)
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/api/v1/proposals/generate")
    assert response.status_code == 200
    data = response.json()
    assert data["total_files"] == 5
    assert data["enqueued_batches"] == 1  # 5 files / 10 batch_size = 1 batch

    await drain_router_background_tasks()
    assert [(q, t) for q, t, _ in capture] == [("controller", "generate_proposals")]
