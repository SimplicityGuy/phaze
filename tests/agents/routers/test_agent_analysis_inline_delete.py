"""Inline staged-object delete on the analysis-result callback (Phase 53, Plan 05 -- D-02, KSTAGE-04).

The success (``put_analysis``) and failure (``report_analysis_failed``) result callbacks delete the
staged S3 object inline -- the moment it is provably no longer needed -- guarded on a ``cloud_job``
row existing so the all-local path makes ZERO S3 calls (no client build, no S3 config required).

Covers:
- success path WITH a cloud_job row -> ``delete_staged_object`` called exactly once
- failure path WITH a cloud_job row -> ``delete_staged_object`` called exactly once
- the all-local guard (no cloud_job row) on BOTH paths -> zero S3 calls, no S3 config needed
- a delete error never corrupts the recorded result (record-first discipline, T-53-21)

Mirrors ``test_agent_analysis.py``: smoke-app wiring, seed a FileRecord for FK satisfaction, and
patch the module-level ``s3_staging`` so no real S3 backend is touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.routers import agent_analysis
from phaze.routers.agent_analysis import router as agent_analysis_router
from phaze.services import s3_staging


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


# One-kueue registry so the inline-delete handler resolves the recorded ``staging_bucket`` ("staging")
# to its ``BucketConfig`` (MKUE-02). The all-local tests seed NO cloud_job, so they never read settings.
_STAGING_REGISTRY = """
    [[backends]]
    kind = "kueue"
    id = "cluster-01"
    rank = 10
    cap = 4
    buckets = ["staging"]

    [backends.kube]
    api_url = "https://kube.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq"

    [[buckets]]
    id = "staging"
    scope = "shared"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-staging"
"""


def _make_client(session: AsyncSession, token: str | None = None) -> AsyncClient:
    """Build an AsyncClient over a smoke app wiring only the agent_analysis router."""
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_analysis_router)
    app.dependency_overrides[get_session] = lambda: session
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
    """Seed a FileRecord so the analysis/cloud_job FKs (files.id) are satisfied."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash="0" * 64,
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
        )
    )
    await session.commit()
    return file_id


async def _seed_cloud_job(session: AsyncSession, file_id: uuid.UUID, *, staging_bucket: str | None = "staging") -> None:
    """Seed a cloud_job row so the inline-delete guard treats the file as staged.

    Phase 70 (MKUE-02): a bucketed row (``staging_bucket`` set) drives the inline delete against the
    recorded bucket; a ``staging_bucket=None`` row (compute / unstaged) makes ZERO S3 calls.
    """
    session.add(
        CloudJob(
            file_id=file_id,
            s3_key=s3_staging.staged_object_key(file_id),
            status=CloudJobStatus.UPLOADED,
            upload_id="test-upload-id",
            staging_bucket=staging_bucket,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_put_analysis_with_cloud_job_deletes_object(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """A successful put_analysis for a staged file deletes the object exactly once, on the recorded bucket (D-02/MKUE-02)."""
    backends_toml_env(_STAGING_REGISTRY)
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id)

    delete_mock = AsyncMock()
    monkeypatch.setattr(agent_analysis.s3_staging, "delete_staged_object", delete_mock)

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 128.0})

    assert response.status_code == 200, response.text
    delete_mock.assert_awaited_once()
    assert delete_mock.await_args.args[0] == file_id  # file_id (bucket is the 2nd arg, MKUE-02)
    assert delete_mock.await_args.args[1].id == "staging"  # acted on the RECORDED staging bucket

    # Result is durably recorded and marked complete (record-first holds). Phase 90 (D-09): the derived
    # 'analyzed' authority is analysis_completed_at, not files.state (that write was removed).
    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert row.bpm == 128.0
    assert row.analysis_completed_at is not None


@pytest.mark.asyncio
async def test_report_failed_with_cloud_job_deletes_object(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """A report_analysis_failed for a staged file deletes the object exactly once, on the recorded bucket (D-02/MKUE-02)."""
    backends_toml_env(_STAGING_REGISTRY)
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id)

    delete_mock = AsyncMock()
    monkeypatch.setattr(agent_analysis.s3_staging, "delete_staged_object", delete_mock)

    async with _make_client(session, raw_token) as ac:
        response = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/failed",
            json={"reason": "timeout", "error": "windowed analysis exceeded budget"},
        )

    assert response.status_code == 200, response.text
    delete_mock.assert_awaited_once()
    assert delete_mock.await_args.args[0] == file_id
    assert delete_mock.await_args.args[1].id == "staging"

    # Phase 90 (D-09): the derived analyze-failure authority is analysis.failed_at, not files.state.
    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert row.failed_at is not None


@pytest.mark.asyncio
async def test_put_analysis_all_local_makes_zero_s3_calls(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cloud_job row -> the success path never calls s3_staging (T-53-22, no S3 config needed)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)  # NO cloud_job row seeded.

    delete_mock = AsyncMock()
    monkeypatch.setattr(agent_analysis.s3_staging, "delete_staged_object", delete_mock)

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 90.0})

    assert response.status_code == 200, response.text
    # The guard short-circuits before any S3 call -> zero invocations, no S3 backend required.
    delete_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_analysis_bucketless_cloud_job_makes_zero_s3_calls(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """A cloud_job row with staging_bucket=None (compute / unstaged) makes ZERO S3 calls (MKUE-02 None-skip)."""
    backends_toml_env(_STAGING_REGISTRY)
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, staging_bucket=None)  # bucketless row -> no S3 object

    delete_mock = AsyncMock()
    monkeypatch.setattr(agent_analysis.s3_staging, "delete_staged_object", delete_mock)

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 100.0})

    assert response.status_code == 200, response.text
    # None-bucket row skips S3 cleanly -- mirrors the all-local guard (no client build, no S3 config).
    delete_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_failed_all_local_makes_zero_s3_calls(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cloud_job row -> the failure path never calls s3_staging (T-53-22, no S3 config needed)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)  # NO cloud_job row seeded.

    delete_mock = AsyncMock()
    monkeypatch.setattr(agent_analysis.s3_staging, "delete_staged_object", delete_mock)

    async with _make_client(session, raw_token) as ac:
        response = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "crashed"})

    assert response.status_code == 200, response.text
    delete_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_error_does_not_corrupt_recorded_result(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """A cleanup blip is swallowed: the recorded analysis result is preserved (T-53-21)."""
    backends_toml_env(_STAGING_REGISTRY)
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id)

    delete_mock = AsyncMock(side_effect=s3_staging.S3StagingError("transient S3 blip"))
    monkeypatch.setattr(agent_analysis.s3_staging, "delete_staged_object", delete_mock)

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 140.0})

    # Delete blew up but the request still succeeds and the result is committed.
    assert response.status_code == 200, response.text
    delete_mock.assert_awaited_once()
    assert delete_mock.await_args.args[0] == file_id

    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert row.bpm == 140.0
    # Phase 90 (D-09): completion derives from analysis_completed_at, not the removed files.state write.
    assert row.analysis_completed_at is not None


@pytest.mark.asyncio
async def test_put_analysis_deletes_object_after_the_result_commits(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """phaze-uoiw: the S3 delete fires AFTER session.commit(), not inside the still-open transaction.

    Pre-fix the delete ran BEFORE commit, so a commit failure could delete the staged object while
    rolling back the recorded result (record-first violated) and the S3 round-trip was held across the
    analysis + cloud_job row locks. We spy on the ordering: the last ``AsyncSession.commit`` must be
    recorded strictly before ``delete_staged_object`` runs.
    """
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    backends_toml_env(_STAGING_REGISTRY)
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id)

    order: list[str] = []
    real_commit = _AsyncSession.commit

    async def _spy_commit(self: _AsyncSession) -> None:
        await real_commit(self)
        order.append("commit")

    async def _delete_recording(_fid: uuid.UUID, _bucket: Any) -> None:
        order.append("delete")

    monkeypatch.setattr(_AsyncSession, "commit", _spy_commit)
    monkeypatch.setattr(agent_analysis.s3_staging, "delete_staged_object", _delete_recording)

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 111.0})

    assert response.status_code == 200, response.text
    # The delete ran, and the commit that precedes it is recorded first: commit-then-delete ordering.
    assert order[-2:] == ["commit", "delete"]
