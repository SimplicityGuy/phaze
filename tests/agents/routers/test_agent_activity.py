"""Controller-side contract tests for GET /admin/agents/{agent_id}/_activity — Phase 88 (88-03, DRILL-02).

Covers:
- Known agent -> 200 HTML fragment: liveness header (kind badge + status pill + last-seen), the 6-stage
  COUNT matrix (Meta/FP/Analyze/Prop/Appr/Exec), per-lane queue depths, recent scan batches, and the
  D-03 own-tick (hx-trigger="every 5s" re-fetching into #detail-pane).
- Appr=Stage.REVIEW / Exec=Stage.APPLY remap (RESEARCH Pitfall 3): a proposal-only file reads DONE in
  the Appr column and NOT DONE in the Exec column.
- Unknown agent_id -> friendly empty fragment (404 body, never a 500 / JSON / HTTPException).
- Agent owning 0 files -> "This agent owns no files yet." empty state.
- Queue depths degrade to 0 (the smoke app has no app.state.task_router) — never a 500.
- The fragment reads ONLY derived stage_status_case counts — never renders FileRecord.state (T-88-10).

Uses the smoke-app fixture from test_admin_agents.py (mounts admin_agents.router on a bare FastAPI app,
overrides get_session with the project-wide real-PG session fixture). The per-agent GROUP BY runs on
real PG (5433) so the GroupingError-safe inner-subquery shape is exercised end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers import admin_agents


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


_AGENT_ID = "activity-agent"


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a smoke FastAPI app mounting only admin_agents.router (mirrors test_admin_agents.py)."""
    app = FastAPI(title="agent-activity-smoke", version="test")
    app.include_router(admin_agents.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


async def _file(session: AsyncSession, *, agent_id: str = _AGENT_ID, file_type: str = "mp3") -> FileRecord:
    """Seed a bare FileRecord owned by ``agent_id`` (flush only — get_session never commits)."""
    fid = uuid.uuid4()
    rec = FileRecord(
        id=fid,
        sha256_hash=uuid.uuid4().hex,
        original_path=f"/media/{fid}.{file_type}",
        original_filename=f"{fid}.{file_type}",
        current_path=f"/media/{fid}.{file_type}",
        file_type=file_type,
        file_size=1234,
        state=FileState.DISCOVERED.value,
        agent_id=agent_id,
    )
    session.add(rec)
    await session.flush()
    return rec


@pytest_asyncio.fixture
async def smoke(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Smoke client with one live agent owning a small mixed corpus."""
    session.add(Agent(id=_AGENT_ID, name="ActivityBox", scan_roots=["/data/music"], last_seen_at=datetime.now(UTC), kind="fileserver"))
    await session.flush()

    # metadata: 1 done + 1 failed; analyze: 1 done; fingerprint: 1 done. (Downstream stays not_started.)
    f = await _file(session)
    session.add(FileMetadata(file_id=f.id, failed_at=None))
    f = await _file(session)
    session.add(FileMetadata(file_id=f.id, failed_at=datetime.now(UTC)))
    f = await _file(session)
    session.add(AnalysisResult(file_id=f.id, analysis_completed_at=datetime.now(UTC)))
    f = await _file(session)
    session.add(FingerprintResult(file_id=f.id, engine="audfprint", status="success"))
    await session.flush()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def empty_agent_smoke(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Smoke client with a registered agent that owns NO files."""
    session.add(Agent(id=_AGENT_ID, name="EmptyBox", scan_roots=[], last_seen_at=datetime.now(UTC), kind="compute"))
    await session.flush()
    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_known_agent_returns_activity_fragment(smoke: AsyncClient) -> None:
    """A known agent returns a 200 body fragment with all D-05 sections + the own-tick."""
    response = await smoke.get(f"/admin/agents/{_AGENT_ID}/_activity")
    assert response.status_code == 200, response.text
    body = response.text
    # It is a BODY fragment, never the full page / shell chrome.
    assert "<html" not in body
    # (1) Liveness header — agent name + kind badge + status pill.
    assert "ActivityBox" in body
    assert 'aria-label="Kind: file server"' in body
    assert 'aria-label="Status: alive"' in body
    assert "Last seen" in body
    # (2) The 6-stage matrix — all six remapped column/row labels present.
    for label in (">Meta<", ">FP<", ">Analyze<", ">Prop<", ">Appr<", ">Exec<"):
        assert label in body, f"missing stage row label {label}"
    # (3) Per-lane queue depths.
    assert "Queue depth by lane" in body
    # (4) Recent scan batches section.
    assert "Recent scan batches" in body
    # D-03 own-tick: the body re-fetches this endpoint into #detail-pane every 5s.
    assert 'hx-get="/admin/agents/activity-agent/_activity"' in body
    assert 'hx-trigger="every 5s"' in body
    assert 'hx-target="#detail-pane"' in body


@pytest.mark.asyncio
async def test_seeded_counts_visible(smoke: AsyncClient) -> None:
    """The per-agent counts land in the matrix cells (derived-truth aria-labels)."""
    body = (await smoke.get(f"/admin/agents/{_AGENT_ID}/_activity")).text
    # metadata seeded 1 done + 1 failed (the other 2 files are not_started for metadata).
    assert 'aria-label="Meta done: 1"' in body
    assert 'aria-label="Meta failed: 1"' in body
    # analyze + fingerprint each seeded 1 done.
    assert 'aria-label="Analyze done: 1"' in body
    assert 'aria-label="FP done: 1"' in body


@pytest.mark.asyncio
async def test_appr_exec_remap(session: AsyncSession) -> None:
    """A proposal-only file reads DONE under Appr (Stage.REVIEW) and NOT-DONE under Exec (Stage.APPLY)."""
    session.add(Agent(id=_AGENT_ID, name="RemapBox", scan_roots=[], last_seen_at=datetime.now(UTC), kind="fileserver"))
    await session.flush()
    f = await _file(session)
    # A RenameProposal makes propose + review DONE; apply stays not_started (no completed execution_log).
    session.add(RenameProposal(file_id=f.id, proposed_filename="x.mp3", status=ProposalStatus.PENDING.value))
    await session.flush()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        body = (await ac.get(f"/admin/agents/{_AGENT_ID}/_activity")).text

    # Appr (=review) shows the proposal as done; Exec (=apply) does NOT (getting the remap backwards
    # would flip these). The single owned file means Appr done=1 and Exec done=0.
    assert 'aria-label="Appr done: 1"' in body
    assert 'aria-label="Exec done: 0"' in body
    assert 'aria-label="Exec not started: 1"' in body


@pytest.mark.asyncio
async def test_unknown_agent_friendly_empty_fragment(smoke: AsyncClient) -> None:
    """An unknown agent_id renders a friendly empty fragment (404 body), never a 500 / JSON."""
    response = await smoke.get("/admin/agents/does-not-exist/_activity")
    assert response.status_code == 404
    body = response.text
    assert "<html" not in body
    assert "Agent not found" in body
    # It is HTML, not a JSON error envelope.
    assert '{"detail"' not in body


@pytest.mark.asyncio
async def test_zero_file_agent_empty_state(empty_agent_smoke: AsyncClient) -> None:
    """A registered agent owning 0 files renders the "owns no files yet" empty state, not a 500."""
    response = await empty_agent_smoke.get(f"/admin/agents/{_AGENT_ID}/_activity")
    assert response.status_code == 200, response.text
    body = response.text
    assert "EmptyBox" in body
    assert "This agent owns no files yet." in body


@pytest.mark.asyncio
async def test_queue_depths_degrade_without_app_state(smoke: AsyncClient) -> None:
    """The smoke app has no app.state.task_router — queue depths degrade to 0, never a 500."""
    response = await smoke.get(f"/admin/agents/{_AGENT_ID}/_activity")
    assert response.status_code == 200
    body = response.text
    # Every lane degrades to 0 (the all-zero dict is still rendered — never the "unavailable" fallback,
    # which only shows on an empty dict).
    assert "Queue depth by lane" in body
    assert "analyze" in body


@pytest.mark.asyncio
async def test_no_raw_state_render(smoke: AsyncClient) -> None:
    """The fragment must NOT leak a raw FileRecord.state value (T-88-10 / Pitfall 5)."""
    body = (await smoke.get(f"/admin/agents/{_AGENT_ID}/_activity")).text
    # The seeded files are all DISCOVERED — that raw state string must never reach the fragment.
    assert "discovered" not in body.lower()
