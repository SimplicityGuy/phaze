"""Contract tests for the control-side S3 object-staging callbacks (Phase 53, Plan 04).

Two endpoints mirror the ``agent_push.py`` split (report_pushed / report_push_mismatch):

- ``POST /api/internal/agent/s3/{file_id}/uploaded`` -- the file-server agent reports the ordered
  ``(part_number, etag)`` list it collected; control completes the multipart upload CONTROL-SIDE
  (KSTAGE-01/DIST-01 -- never the agent) and flips ``cloud_job`` ``UPLOADING -> UPLOADED`` with a
  rowcount guard (a duplicate/late callback is an idempotent 200 that does NOT re-complete).
- ``POST /api/internal/agent/s3/{file_id}/failed`` -- the agent reports an upload failure; under
  the re-drive cap control re-drives (``cloud_staging.redrive_upload``) and increments the ledger
  attempt counter (cleared=False); at/over the cap control sets ``cloud_job`` FAILED, aborts the
  multipart, deletes the staged object, and clears the ledger (cleared=True, KSTAGE-04).

S3 SDK + re-drive calls are monkeypatched (AsyncMocks) so the contract is exercised without a live
S3 backend; the FakeTaskRouter stands in for the per-agent queue.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from phaze.database import get_session
from phaze.main import create_app
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_s3 import router as agent_s3_router
from phaze.services import cloud_staging, s3_staging
from phaze.services.scheduling_ledger import upsert_ledger_entry
from tests._queue_fakes import FakeTaskRouter


if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


class _StubCfg(SimpleNamespace):
    """A duck-typed ControlSettings stand-in carrying only the field the router reads."""

    def __init__(self, *, push_max_attempts: int = 3) -> None:
        super().__init__(push_max_attempts=push_max_attempts)


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, push_max_attempts: int = 3) -> None:
    monkeypatch.setattr("phaze.routers.agent_s3.get_settings", lambda: _StubCfg(push_max_attempts=push_max_attempts))


def _make_client(session: AsyncSession, task_router: FakeTaskRouter, token: str | None = None) -> AsyncClient:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_s3_router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.task_router = task_router
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash="a" * 64,
            original_path=f"/test/music/{file_id}.flac",
            original_filename=f"{file_id}.flac",
            current_path=f"/test/music/{file_id}.flac",
            file_type="flac",
            file_size=4096,
            state=FileState.AWAITING_CLOUD,
        )
    )
    await session.commit()
    return file_id


async def _seed_cloud_job(session: AsyncSession, file_id: uuid.UUID, *, status: CloudJobStatus = CloudJobStatus.UPLOADING) -> None:
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file_id,
            s3_key=f"phaze-staging/{file_id}",
            status=status.value,
            upload_id="upload-xyz",
        )
    )
    await session.commit()


async def _seed_ledger(session: AsyncSession, file_id: uuid.UUID, *, attempt: int | None = None) -> None:
    payload: dict[str, Any] = {"file_id": str(file_id)}
    if attempt is not None:
        payload["s3_upload_attempt"] = attempt
    await upsert_ledger_entry(session, key=f"s3_upload:{file_id}", function="s3_upload", kwargs=payload)
    await session.commit()


async def _cloud_job(session: AsyncSession, file_id: uuid.UUID) -> CloudJob | None:
    stmt = select(CloudJob).where(CloudJob.file_id == file_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _ledger_row(session: AsyncSession, key: str) -> SchedulingLedger | None:
    stmt = select(SchedulingLedger).where(SchedulingLedger.key == key).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# /uploaded
# ---------------------------------------------------------------------------


async def test_uploaded_completes_multipart_control_side_and_flips_state(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uploaded -> control completes the multipart + cloud_job UPLOADING -> UPLOADED, 200."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch)
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    complete = AsyncMock()
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", complete)

    body = {"parts": [{"part_number": 1, "etag": '"etag-1"'}, {"part_number": 2, "etag": '"etag-2"'}]}
    async with _make_client(session, FakeTaskRouter(), raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["file_id"] == str(file_id)
    # Control completed the multipart itself (KSTAGE-01), with the agent-reported parts.
    complete.assert_awaited_once()
    call_args = complete.await_args.args
    assert call_args[0] == file_id
    assert call_args[1] == "upload-xyz"
    assert sorted(call_args[2]) == [(1, '"etag-1"'), (2, '"etag-2"')]
    # State flipped.
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADED.value


async def test_uploaded_duplicate_is_idempotent_noop_without_recompleting(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate /uploaded (cloud_job already UPLOADED) is an idempotent 200 that does NOT re-complete."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch)
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADED)  # already completed
    complete = AsyncMock()
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", complete)

    body = {"parts": [{"part_number": 1, "etag": '"etag-1"'}]}
    async with _make_client(session, FakeTaskRouter(), raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    assert r.status_code == 200, r.text
    complete.assert_not_awaited()  # must NOT re-complete an already-UPLOADED object
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADED.value


async def test_uploaded_rejects_identity_in_body(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """extra=forbid: a body smuggling file_id/agent_id is a 422 (AUTH-01: file_id is path-only)."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch)
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id)
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", AsyncMock())

    body = {"parts": [{"part_number": 1, "etag": '"e"'}], "file_id": str(uuid.uuid4()), "agent_id": "evil"}
    async with _make_client(session, FakeTaskRouter(), raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    assert r.status_code == 422


async def test_uploaded_unauthenticated_returns_401(session: AsyncSession) -> None:
    """No bearer token -> 401."""
    async with _make_client(session, FakeTaskRouter(), token=None) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{uuid.uuid4()}/uploaded", json={"parts": []})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /failed
# ---------------------------------------------------------------------------


async def test_failed_under_cap_redrives_and_increments_counter(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the cap: redrive_upload called, cloud_job stays uploading, attempt counter ++ -> cleared=False."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, push_max_attempts=3)
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    await _seed_ledger(session, file_id, attempt=0)
    redrive = AsyncMock()
    monkeypatch.setattr(cloud_staging, "redrive_upload", redrive)

    async with _make_client(session, FakeTaskRouter(), raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/failed", json={"detail": "transfer error"})

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is False
    redrive.assert_awaited_once()
    # cloud_job is left UPLOADING (the re-drive keeps the slot).
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value
    # attempt counter incremented in the ledger payload.
    row = await _ledger_row(session, f"s3_upload:{file_id}")
    assert row is not None
    assert row.payload.get("s3_upload_attempt") == 1


async def test_failed_at_cap_fails_terminally_and_cleans_up(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the cap: cloud_job FAILED + abort multipart + delete staged object + ledger cleared -> cleared=True."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, push_max_attempts=3)
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    await _seed_ledger(session, file_id, attempt=3)  # next attempt (4) exceeds the cap
    abort = AsyncMock()
    delete = AsyncMock()
    redrive = AsyncMock()
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)
    monkeypatch.setattr(cloud_staging, "redrive_upload", redrive)

    async with _make_client(session, FakeTaskRouter(), raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/failed", json={"detail": "fatal"})

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is True
    redrive.assert_not_awaited()  # terminal, not re-driven
    abort.assert_awaited_once()
    delete.assert_awaited_once()
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.FAILED.value
    assert await _ledger_row(session, f"s3_upload:{file_id}") is None  # ledger cleared


async def test_failed_unauthenticated_returns_401(session: AsyncSession) -> None:
    """No bearer token -> 401."""
    async with _make_client(session, FakeTaskRouter(), token=None) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{uuid.uuid4()}/failed", json={})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# router mount
# ---------------------------------------------------------------------------


def test_agent_s3_router_is_mounted_on_the_app() -> None:
    """The agent_s3 routes resolve through the real application (router is mounted in main.py)."""
    app = create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/api/internal/agent/s3/{file_id}/uploaded" in paths
    assert "/api/internal/agent/s3/{file_id}/failed" in paths
