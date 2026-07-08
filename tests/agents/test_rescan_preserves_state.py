"""Rescan-wipe regression (Phase 77, MIG-03 / CONTEXT D-08) — agent upsert endpoint site.

The agent file-upsert endpoint (``POST /api/internal/agent/files`` →
``routers/agent_files.py::upsert_files``) is the near-identical mirror of the
``services/ingestion.py`` upsert site: it too carries ``"state": base_stmt.excluded.state``
in the ``ON CONFLICT DO UPDATE`` ``set_`` dict, so an agent rescan of an ``ANALYZED`` file
re-stamps ``state = DISCOVERED`` (the handler stamps ``data["state"] = DISCOVERED`` on every
receive), silently wiping progress. Both sites must be fixed or the bug survives on one path.

This test proves the two-part D-08 invariant at the agent endpoint: after re-POSTing the SAME
``(agent_id, original_path)``, (1) the file's ``state`` stays ``ANALYZED``, (2) its ``analysis``
output row survives, and (3) the response reports the row as UPDATED (``inserted`` count 0), not
newly inserted. AUTH-01 is preserved throughout — ``agent_id`` is stamped from the auth
dependency, never the request body.

RED against current (pre-fix) code — the state assertion fails because the ``set_`` clause
overwrites ``analyzed`` with the incoming ``discovered``. GREEN once the ``state`` key is
removed from the ``set_`` dict.

Lives in the ``agents`` bucket (one bucket per file — the agent router is the module under
test). Needs the ephemeral :5433 DB via ``just test-bucket agents``. Uses a self-contained
smoke app mounting only ``agent_files.router`` (mirrors the Phase-25 smoke-app pattern) so the
test does not depend on main.py wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers import agent_files


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


_RESCAN_PATH = "/test/music/rescan/advanced-set.mp3"


def _record() -> dict[str, object]:
    """A single agent file-upsert record for ``_RESCAN_PATH`` (no agent_id — AUTH-01)."""
    return {
        "sha256_hash": "b" * 64,
        "original_path": _RESCAN_PATH,
        "original_filename": "advanced-set.mp3",
        "current_path": _RESCAN_PATH,
        "file_type": "mp3",
        "file_size": 2048,
    }


@pytest_asyncio.fixture
async def agent_upsert_client(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> AsyncGenerator[tuple[AsyncClient, Agent]]:
    """Smoke-app client mounting agent_files.router, with the LIVE sentinel batch pre-seeded.

    The upsert handler resolves the calling agent's LIVE sentinel batch when ``batch_id`` is
    omitted (Phase 27 D-09/D-18); ``seed_test_agent`` pre-dates that flow, so seed one here.
    """
    agent, raw_token = seed_test_agent
    session.add(
        ScanBatch(
            agent_id=agent.id,
            scan_path="<watcher>",
            status=ScanStatus.LIVE.value,
            total_files=0,
            processed_files=0,
        )
    )
    await session.commit()

    app = FastAPI(title="agent-files-rescan-smoke", version="test")
    app.include_router(agent_files.router)
    app.dependency_overrides[get_session] = lambda: session
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        yield ac, agent


@pytest.mark.asyncio
async def test_agent_upsert_rescan_preserves_analyzed_state_and_analysis_row(
    agent_upsert_client: tuple[AsyncClient, Agent],
    session: AsyncSession,
) -> None:
    """Re-POSTing an ANALYZED file keeps state=ANALYZED, its analysis row, and reports inserted=0."""
    client, agent = agent_upsert_client

    # 1. Initial discovery upsert: the file lands at DISCOVERED (server-stamped).
    r1 = await client.post("/api/internal/agent/files", json={"files": [_record()]})
    assert r1.status_code == 200, r1.text
    assert r1.json()["inserted"] == 1

    file = (await session.execute(select(FileRecord).where(FileRecord.original_path == _RESCAN_PATH))).scalar_one()
    assert file.state == FileState.DISCOVERED
    assert file.agent_id == agent.id  # AUTH-01: stamped from auth dep, never the body

    # 2. Advance it to ANALYZED and create its 1:1 analysis output row.
    file.state = FileState.ANALYZED
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=file.id, bpm=128.0, musical_key="Am"))
    await session.commit()

    # 3. Agent rescan: re-POST the SAME (agent_id, original_path). The state-wipe bug resets here.
    r2 = await client.post("/api/internal/agent/files", json={"files": [_record()]})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["upserted"] == 1
    assert body["inserted"] == 0, "rescan of an existing file must report an UPDATE (xmax != 0), not an insert"

    # 4. Invariant (D-08): state survives AND the analysis output row survives.
    session.expire_all()
    reloaded = (await session.execute(select(FileRecord).where(FileRecord.original_path == _RESCAN_PATH))).scalar_one()
    assert reloaded.state == FileState.ANALYZED, "agent rescan wiped the file's state back to DISCOVERED (MIG-03 regression)"

    analysis = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == reloaded.id))).scalar_one_or_none()
    assert analysis is not None, "the file's analysis output row must survive an agent rescan"
