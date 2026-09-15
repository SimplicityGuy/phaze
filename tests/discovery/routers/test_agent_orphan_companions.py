"""Authenticated persistence contract for scan-owned orphan COMPANION diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import secrets
from typing import TYPE_CHECKING
import unicodedata
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.orphan_companion_diagnostic import OrphanCompanionDiagnostic
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers import agent_orphan_companions


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def diagnostic_client(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> AsyncGenerator[AsyncClient]:
    _agent, raw_token = seed_test_agent
    app = FastAPI()
    app.include_router(agent_orphan_companions.router)
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {raw_token}"},
    ) as client:
        yield client


async def _seed_batch(
    session: AsyncSession,
    agent_id: str,
    *,
    status: ScanStatus = ScanStatus.RUNNING,
    scan_path: str = "/archive/root",
    configured_root: str = "/archive/root",
) -> ScanBatch:
    batch = ScanBatch(
        agent_id=agent_id,
        scan_path=scan_path,
        configured_root=configured_root,
        status=status.value,
        total_files=0,
        processed_files=0,
    )
    session.add(batch)
    await session.commit()
    return batch


def _chunk(*paths: str) -> dict[str, object]:
    return {"diagnostics": [{"normalized_path": path, "companion_extension": "." + path.rsplit(".", 1)[-1].lower()} for path in paths]}


@pytest.mark.asyncio
async def test_owner_persists_normalized_metadata_only_rows_and_replay_is_idempotent(
    diagnostic_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    agent, _token = seed_test_agent
    batch = await _seed_batch(session, agent.id)
    nfd_path = "/archive/root/orphans/cafe\u0301.nfo"
    payload = _chunk(nfd_path, "/archive/root/orphans/list.M3U")

    first = await diagnostic_client.post(f"/api/internal/agent/scan-batches/{batch.id}/orphan-companions", json=payload)
    second = await diagnostic_client.post(f"/api/internal/agent/scan-batches/{batch.id}/orphan-companions", json=payload)

    assert first.status_code == 200, first.text
    assert first.json() == {"batch_id": str(batch.id), "inserted": 2, "existing": 0}
    assert second.status_code == 200, second.text
    assert second.json() == {"batch_id": str(batch.id), "inserted": 0, "existing": 2}
    rows = (await session.execute(select(OrphanCompanionDiagnostic).order_by(OrphanCompanionDiagnostic.normalized_path))).scalars().all()
    assert len(rows) == 2
    assert {row.normalized_path for row in rows} == {
        unicodedata.normalize("NFC", nfd_path),
        "/archive/root/orphans/list.M3U",
    }
    assert {row.configured_root for row in rows} == {"/archive/root"}
    assert {row.companion_extension for row in rows} == {".nfo", ".m3u"}
    assert (await session.execute(select(func.count()).select_from(FileRecord))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(FileCompanion))).scalar_one() == 0


@pytest.mark.asyncio
async def test_unknown_batch_is_404_and_cross_agent_is_403(
    diagnostic_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    _agent, _token = seed_test_agent
    missing = await diagnostic_client.post(
        f"/api/internal/agent/scan-batches/{uuid.uuid4()}/orphan-companions",
        json=_chunk("/archive/root/info.nfo"),
    )
    assert missing.status_code == 404

    raw_token = "phaze_agent_" + secrets.token_urlsafe(32)
    other = Agent(
        id="other-fileserver",
        name="Other Fileserver",
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        scan_roots=["/other"],
    )
    session.add(other)
    await session.commit()
    batch = await _seed_batch(session, other.id, scan_path="/other", configured_root="/other")
    denied = await diagnostic_client.post(
        f"/api/internal/agent/scan-batches/{batch.id}/orphan-companions",
        json=_chunk("/other/info.nfo"),
    )
    assert denied.status_code == 403
    assert (await session.execute(select(func.count()).select_from(OrphanCompanionDiagnostic))).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_status", [ScanStatus.COMPLETED, ScanStatus.FAILED, ScanStatus.LIVE])
async def test_non_running_batch_is_rejected(
    diagnostic_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    batch_status: ScanStatus,
) -> None:
    agent, _token = seed_test_agent
    root = "/archive/root" if batch_status is not ScanStatus.LIVE else "<watcher>"
    batch = await _seed_batch(session, agent.id, status=batch_status, scan_path=root, configured_root=root)
    response = await diagnostic_client.post(
        f"/api/internal/agent/scan-batches/{batch.id}/orphan-companions",
        json=_chunk("/archive/root/info.nfo"),
    )
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/archive/rooted/info.nfo", "/archive/root/../escape.nfo", "relative/info.nfo"])
async def test_path_outside_configured_root_is_rejected_before_persistence(
    diagnostic_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    path: str,
) -> None:
    agent, _token = seed_test_agent
    batch = await _seed_batch(session, agent.id)
    response = await diagnostic_client.post(
        f"/api/internal/agent/scan-batches/{batch.id}/orphan-companions",
        json=_chunk(path),
    )
    assert response.status_code == 422
    assert (await session.execute(select(func.count()).select_from(OrphanCompanionDiagnostic))).scalar_one() == 0


@pytest.mark.asyncio
async def test_path_inside_configured_root_but_outside_selected_scan_path_is_rejected(
    diagnostic_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    agent, _token = seed_test_agent
    batch = await _seed_batch(session, agent.id, scan_path="/archive/root/selected")
    response = await diagnostic_client.post(
        f"/api/internal/agent/scan-batches/{batch.id}/orphan-companions",
        json=_chunk("/archive/root/sibling/info.nfo"),
    )
    assert response.status_code == 422
    assert (await session.execute(select(func.count()).select_from(OrphanCompanionDiagnostic))).scalar_one() == 0


@pytest.mark.asyncio
async def test_revoked_and_missing_tokens_are_unauthorized(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    agent, raw_token = seed_test_agent
    batch = await _seed_batch(session, agent.id)
    agent.revoked_at = datetime.now(UTC)
    await session.commit()
    app = FastAPI()
    app.include_router(agent_orphan_companions.router)
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post(f"/api/internal/agent/scan-batches/{batch.id}/orphan-companions", json=_chunk("/archive/root/info.nfo"))
        revoked = await client.post(
            f"/api/internal/agent/scan-batches/{batch.id}/orphan-companions",
            json=_chunk("/archive/root/info.nfo"),
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert missing.status_code == 401
    assert revoked.status_code == 403
