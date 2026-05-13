"""Controller-side contract tests for Phase 27 D-05..D-08 pipeline_scans router.

Covers:
- POST /pipeline/scans -- form validation (T-27-03 ``..`` rejection, prefix check,
  agent-revoked guard), ScanBatch creation, AgentTaskRouter.enqueue_for_agent
  call assertion, atomicity on rejection paths.
- GET /pipeline/scans/{batch_id} -- HTMX poll partial; running carries
  hx-trigger="every 2s" + hx-swap="outerHTML", terminal states OMIT both
  (Pitfall 6 invariant verified at the controller level).
- GET /pipeline/scans/agent-roots -- HTMX swap partial; empty-state copy for
  agents with no scan_roots configured.
- Dashboard render -- Trigger Scan card heading + Recent Scans heading present
  on /pipeline/ output.

Uses a self-contained smoke-app fixture (mirrors test_agent_files.py:53-65)
that installs an ``AsyncMock`` at ``app.state.task_router`` so tests can
assert against ``enqueue_for_agent.await_args_list`` without a real Redis
connection. The fixture seeds a single non-revoked agent with scan_roots
configured so most happy-path tests need no extra setup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers import pipeline, pipeline_scans


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> tuple[FastAPI, AsyncMock]:
    """Build a smoke FastAPI app mounting pipeline_scans + pipeline routers.

    Returns the app AND the AsyncMock installed at ``app.state.task_router``
    so happy-path tests can assert against ``enqueue_for_agent`` call args.
    """
    app = FastAPI(title="pipeline-scans-smoke", version="test")
    app.include_router(pipeline_scans.router)
    app.include_router(pipeline.router)
    app.dependency_overrides[get_session] = lambda: session
    mock_router = AsyncMock()
    app.state.task_router = mock_router
    # The pipeline router's existing trigger endpoints reference app.state.queue;
    # install a benign mock to keep the dashboard handler import-safe even
    # though dashboard tests do not exercise the queue.
    app.state.queue = AsyncMock()
    return app, mock_router


@pytest_asyncio.fixture
async def smoke(session: AsyncSession) -> AsyncGenerator[tuple[AsyncClient, AsyncMock]]:
    """Smoke client + mock task_router; seeds one non-revoked agent with scan_roots."""
    # Seed a known test agent. Use a kebab-case slug compatible with the
    # Agent.id_charset check constraint.
    agent = Agent(
        id="test-agent",
        name="Test Agent",
        token_hash=None,
        scan_roots=["/data/music", "/data/videos"],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    app, mock_router = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, mock_router


async def _count_batches(session: AsyncSession) -> int:
    """Count ScanBatch rows in the test session."""
    rows = (await session.execute(select(ScanBatch))).scalars().all()
    return len(rows)


# ---------------------------------------------------------------------------
# Task 1 (router contract) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_scans_happy_path(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """POST /pipeline/scans creates a RUNNING ScanBatch and enqueues scan_directory."""
    ac, mock_router = smoke
    pre_count = await _count_batches(session)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    assert response.status_code == 200, response.text
    # Body contains the running-state markup (heading + RUNNING pill).
    assert "Scan in progress" in response.text
    assert "RUNNING" in response.text
    assert 'hx-trigger="every 2s"' in response.text

    # AgentTaskRouter.enqueue_for_agent called exactly once with the documented contract.
    mock_router.enqueue_for_agent.assert_awaited_once()
    call = mock_router.enqueue_for_agent.await_args
    assert call.kwargs["agent_id"] == "test-agent"
    assert call.kwargs["task_name"] == "scan_directory"
    payload = call.kwargs["payload"]
    assert payload.scan_path == "/data/music/2026/"
    assert payload.agent_id == "test-agent"
    assert isinstance(payload.batch_id, uuid.UUID)

    # Exactly one new ScanBatch row.
    post_count = await _count_batches(session)
    assert post_count == pre_count + 1
    new_batch = (await session.execute(select(ScanBatch).where(ScanBatch.scan_path == "/data/music/2026/"))).scalar_one()
    assert new_batch.status == "running"
    assert new_batch.agent_id == "test-agent"


@pytest.mark.asyncio
async def test_post_scans_subpath_rejects_dotdot(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """T-27-03: subpath containing ``..`` rejects with 400 + error card; NO batch created."""
    ac, mock_router = smoke
    pre_count = await _count_batches(session)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "../../etc"},
    )
    assert response.status_code == 400
    assert 'role="alert"' in response.text
    # Jinja autoescapes `'` to `&#39;`, so check on a substring that survives escaping.
    assert "Subpath must not contain" in response.text
    assert "path traversal" in response.text

    # Atomicity: NO ScanBatch row created on rejection.
    post_count = await _count_batches(session)
    assert post_count == pre_count
    # And NO enqueue.
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_path_outside_scan_root(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """T-27-03: scan_root not in agent.scan_roots (prefix-check fails) rejects with 400."""
    ac, mock_router = smoke

    # /data/photos is NOT in the seeded agent's scan_roots (which are
    # /data/music + /data/videos). The prefix-check fails.
    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/photos", "subpath": "vacation/"},
    )
    assert response.status_code == 400
    assert "Resolved path is outside the selected scan root." in response.text
    assert await _count_batches(session) == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_unknown_agent_400(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Unknown agent_id rejects with 400 + 'Unknown or revoked agent.'."""
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "nonexistent-agent", "scan_root": "/data/music", "subpath": ""},
    )
    assert response.status_code == 400
    assert "Unknown or revoked agent." in response.text
    assert await _count_batches(session) == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_scan_root_not_in_agent_roots(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """scan_root NOT in agent.scan_roots is treated as outside-root (prefix-check fails)."""
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        # /etc is not in seeded agent's scan_roots.
        data={"agent_id": "test-agent", "scan_root": "/etc", "subpath": ""},
    )
    assert response.status_code == 400
    assert "Resolved path is outside the selected scan root." in response.text
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_scan_progress_running_returns_polling_partial(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """GET /pipeline/scans/{batch_id} for RUNNING batch carries hx-trigger + hx-swap=outerHTML."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/2026/",
        status=ScanStatus.RUNNING.value,
        total_files=10,
        processed_files=3,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get(f"/pipeline/scans/{batch.id}")
    assert response.status_code == 200
    assert 'hx-trigger="every 2s"' in response.text
    assert 'hx-swap="outerHTML"' in response.text
    assert f'hx-get="/pipeline/scans/{batch.id}"' in response.text
    assert "RUNNING" in response.text


@pytest.mark.asyncio
async def test_get_scan_progress_completed_omits_hx_trigger(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Pitfall 6: COMPLETED batch response OMITS hx-trigger and hx-get (HTMX halts polling)."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/2026/",
        status=ScanStatus.COMPLETED.value,
        total_files=10,
        processed_files=10,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get(f"/pipeline/scans/{batch.id}")
    assert response.status_code == 200
    # Pitfall 6 invariant: NO HTMX polling attributes in terminal-state markup.
    assert "hx-trigger" not in response.text
    assert "hx-get" not in response.text
    assert "Scan complete" in response.text
    assert "COMPLETED" in response.text


@pytest.mark.asyncio
async def test_get_scan_progress_failed_renders_error_message(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """FAILED batch renders error_message AND omits hx-trigger."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/missing/",
        status=ScanStatus.FAILED.value,
        total_files=0,
        processed_files=0,
        error_message="path missing",
    )
    session.add(batch)
    await session.commit()

    response = await ac.get(f"/pipeline/scans/{batch.id}")
    assert response.status_code == 200
    assert "path missing" in response.text
    assert "FAILED" in response.text
    assert "hx-trigger" not in response.text
    assert "hx-get" not in response.text


@pytest.mark.asyncio
async def test_agent_roots_swap_returns_partial(smoke: tuple[AsyncClient, AsyncMock]) -> None:
    """GET /pipeline/scans/agent-roots returns scan_path_picker.html with the agent's scan_roots."""
    ac, _ = smoke

    response = await ac.get("/pipeline/scans/agent-roots", params={"agent_id": "test-agent"})
    assert response.status_code == 200
    assert '<select id="scan-root"' in response.text
    assert '<option value="/data/music">/data/music</option>' in response.text
    assert '<option value="/data/videos">/data/videos</option>' in response.text


@pytest.mark.asyncio
async def test_agent_roots_swap_unknown_agent_yields_empty_state(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """Unknown agent or empty scan_roots yields the empty-state copy."""
    ac, _ = smoke

    response = await ac.get("/pipeline/scans/agent-roots", params={"agent_id": "totally-bogus-agent"})
    assert response.status_code == 200
    # Unknown agent renders the agent=None branch (placeholder "Select an agent first").
    assert "Select an agent first" in response.text


# ---------------------------------------------------------------------------
# Task 2 (template / UI-SPEC) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_renders_trigger_scan_card(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """GET /pipeline/ surfaces the Trigger Scan card heading + agent dropdown + picker slot."""
    ac, _ = smoke

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert 'id="trigger-scan-heading"' in response.text
    assert ">Trigger Scan</h2>" in response.text
    assert '<select id="scan-agent"' in response.text
    assert 'id="scan-path-picker"' in response.text
    # Agent option populated as "{name} ({id})" per CONTEXT D-Discretion.
    assert "Test Agent (test-agent)" in response.text


@pytest.mark.asyncio
async def test_dashboard_renders_recent_scans_section(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """GET /pipeline/ surfaces the Recent Scans heading + empty state when no batches."""
    ac, _ = smoke

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert 'id="recent-scans-heading"' in response.text
    assert ">Recent Scans</h2>" in response.text
    # No batches seeded -> empty state.
    assert "No scans yet" in response.text


@pytest.mark.asyncio
async def test_dashboard_recent_scans_shows_failed_row_with_inline_error(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Failed batch renders the second inline-error <tr> with red surface + error_message."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/oops/",
        status=ScanStatus.FAILED.value,
        total_files=0,
        processed_files=0,
        error_message="path missing",
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert 'colspan="6"' in response.text
    assert "bg-red-50" in response.text
    assert "path missing" in response.text


@pytest.mark.asyncio
async def test_dashboard_recent_scans_excludes_live_batches(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """LIVE sentinel batches MUST be excluded from Recent Scans (CONTEXT D-05 / UI-SPEC line 401)."""
    ac, _ = smoke
    live_batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="<watcher>",
        status=ScanStatus.LIVE.value,
        total_files=0,
        processed_files=0,
    )
    session.add(live_batch)
    await session.commit()

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    # The LIVE sentinel must not surface; the table renders the empty state.
    assert "<watcher>" not in response.text
    assert "No scans yet" in response.text


@pytest.mark.asyncio
async def test_status_pill_running_uses_blue_surface(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """RUNNING status pill renders with bg-blue-100 dark:bg-blue-950 + aria-label."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/",
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert "bg-blue-100 dark:bg-blue-950" in response.text
    assert 'aria-label="Status: running"' in response.text


@pytest.mark.asyncio
async def test_status_pill_completed_uses_green_surface(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """COMPLETED status pill renders with bg-green-100."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/done/",
        status=ScanStatus.COMPLETED.value,
        total_files=5,
        processed_files=5,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert "bg-green-100" in response.text
    assert 'aria-label="Status: completed"' in response.text


@pytest.mark.asyncio
async def test_status_pill_failed_uses_red_surface(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """FAILED status pill renders with bg-red-100."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/oops/",
        status=ScanStatus.FAILED.value,
        total_files=0,
        processed_files=0,
        error_message="oops",
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert "bg-red-100" in response.text
    assert 'aria-label="Status: failed"' in response.text


@pytest.mark.asyncio
async def test_router_registered_in_main_app() -> None:
    """pipeline_scans.router is registered in main.create_app() (production wiring)."""
    from phaze.main import create_app

    app = create_app()
    paths = {route.path for route in app.routes if hasattr(route, "path")}  # type: ignore[attr-defined]
    # All three handlers must be reachable on the production app.
    assert "/pipeline/scans" in paths
    assert "/pipeline/scans/{batch_id}" in paths
    assert "/pipeline/scans/agent-roots" in paths
