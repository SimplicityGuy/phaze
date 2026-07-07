"""Tests for POST /api/internal/agent/files/{file_id}/presign-download (Phase 53, Plan 02).

This route is the SERVER side of the Phase 52 pod client ``request_download_url``: it mints a
just-in-time presigned GET URL (KSTAGE-03) and returns the integrity hash sourced SERVER-side
from ``FileRecord.sha256_hash`` (T-53-06 / AUTH-01) -- never from the request. The presign is
served by a wire-compatible ``ThreadedMotoServer`` (real HTTP) pointed at by ControlSettings.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
import uuid

import boto3
from moto.server import ThreadedMotoServer
import pytest

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.schemas.agent_analysis import PresignDownloadResponse


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


_BUCKET = "phaze-test-staging"
_CREDS = {"aws_access_key_id": "testing", "aws_secret_access_key": "testing"}
_SHA = hashlib.sha256(b"phaze-presign-download-test").hexdigest()


@pytest.fixture
def moto_s3_server() -> Iterator[str]:
    """Start a wire-compatible moto S3 server on a free port; yield its endpoint URL."""
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def s3_env(moto_s3_server: str, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Callable[[str], object]) -> Iterator[str]:
    """Drive the registry off a one-kueue-backend backends.toml pointed at the moto server + create the bucket.

    The presign-download route now reads the active bucket's identity/creds via the transitional
    ``active_bucket`` accessor (REG-04, D-14), so the test config is a single kueue backend + one
    bucket in backends.toml instead of the removed flat ``PHAZE_S3_*`` env vars.
    """
    monkeypatch.setenv("PHAZE_ROLE", "control")
    backends_toml_env(
        f"""
        [[backends]]
        kind = "kueue"
        id = "cluster-01"
        rank = 10
        cap = 4
        buckets = ["staging"]

        [backends.kube]
        api_url = "https://kube.test"
        namespace = "phaze"
        local_queue = "phaze-lq"

        [[buckets]]
        id = "staging"
        scope = "shared"
        endpoint_url = "{moto_s3_server}"
        bucket = "{_BUCKET}"
        region = "us-east-1"
        addressing_style = "path"
        access_key_id = "testing"
        secret_access_key = "testing"
        """
    )
    boto3.client("s3", endpoint_url=moto_s3_server, region_name="us-east-1", **_CREDS).create_bucket(Bucket=_BUCKET)
    yield moto_s3_server
    get_settings.cache_clear()


async def _seed_file(session: AsyncSession, agent: Agent, *, sha256: str = _SHA) -> FileRecord:
    """Insert a FileRecord owned by the seeded agent with a known sha256."""
    file = FileRecord(
        id=uuid.uuid4(),
        sha256_hash=sha256,
        original_path="/test/music/song.mp3",
        original_filename="song.mp3",
        current_path="/test/music/song.mp3",
        file_type="mp3",
        file_size=4096,
        state=FileState.AWAITING_CLOUD,
        agent_id=agent.id,
    )
    session.add(file)
    await session.commit()
    return file


async def _seed_cloud_job(session: AsyncSession, file_id: uuid.UUID, *, status: CloudJobStatus = CloudJobStatus.UPLOADED) -> None:
    """Insert a CloudJob row for ``file_id`` (defaults to UPLOADED -- the staged-object-ready state).

    Phase 70 (MKUE-02): stamps ``staging_bucket`` = the registry bucket id ("staging") so the presign
    handler resolves it to a ``BucketConfig`` and mints the GET against exactly that recorded bucket.
    """
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file_id,
            s3_key=f"phaze-staging/{file_id}",
            status=status.value,
            upload_id="upload-xyz",
            staging_bucket="staging",
        )
    )
    await session.commit()


async def test_presign_download_returns_url_and_server_sourced_sha256(
    s3_env: str,
    authenticated_client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """A valid token returns 200 with a presigned download_url + expected_sha256 from FileRecord."""
    agent, _token = seed_test_agent
    file = await _seed_file(session, agent)
    await _seed_cloud_job(session, file.id, status=CloudJobStatus.UPLOADED)

    resp = await authenticated_client.post(f"/api/internal/agent/files/{file.id}/presign-download")

    assert resp.status_code == 200
    body = resp.json()
    assert body["expected_sha256"] == _SHA  # server-sourced, from FileRecord.sha256_hash
    assert str(file.id) in body["download_url"]  # file_id-scoped key in the URL


async def test_presign_download_body_validates_against_response_schema(
    s3_env: str,
    authenticated_client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """The response body parses cleanly under PresignDownloadResponse (extra=forbid, 64-hex sha)."""
    agent, _token = seed_test_agent
    file = await _seed_file(session, agent)
    await _seed_cloud_job(session, file.id, status=CloudJobStatus.UPLOADED)

    resp = await authenticated_client.post(f"/api/internal/agent/files/{file.id}/presign-download")

    parsed = PresignDownloadResponse.model_validate(resp.json())
    assert parsed.expected_sha256 == _SHA
    assert parsed.download_url


async def test_presign_download_unauthenticated_returns_401(client: AsyncClient) -> None:
    """A request with no bearer token is rejected by the auth dependency."""
    resp = await client.post(f"/api/internal/agent/files/{uuid.uuid4()}/presign-download")
    assert resp.status_code == 401


async def test_presign_download_unknown_file_returns_404(
    s3_env: str,
    authenticated_client: AsyncClient,
) -> None:
    """An unknown file_id returns 404 (not a 500)."""
    resp = await authenticated_client.post(f"/api/internal/agent/files/{uuid.uuid4()}/presign-download")
    assert resp.status_code == 404


async def test_presign_download_mints_per_call_with_server_sourced_hash(
    s3_env: str,
    authenticated_client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """Each call mints a fresh presign (KSTAGE-03); expected_sha256 is always the FileRecord hash."""
    agent, _token = seed_test_agent
    file = await _seed_file(session, agent)
    await _seed_cloud_job(session, file.id, status=CloudJobStatus.UPLOADED)

    first = await authenticated_client.post(f"/api/internal/agent/files/{file.id}/presign-download")
    second = await authenticated_client.post(f"/api/internal/agent/files/{file.id}/presign-download")

    assert first.status_code == second.status_code == 200
    # The integrity hash is server-sourced on EVERY call -- never echoed from a request body.
    assert first.json()["expected_sha256"] == _SHA
    assert second.json()["expected_sha256"] == _SHA
    assert str(file.id) in first.json()["download_url"]
    assert str(file.id) in second.json()["download_url"]


async def test_presign_download_not_uploaded_returns_409(
    s3_env: str,
    authenticated_client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """A file whose cloud_job is not yet UPLOADED returns 409, not a dead presigned URL (WR-03)."""
    agent, _token = seed_test_agent
    file = await _seed_file(session, agent)
    await _seed_cloud_job(session, file.id, status=CloudJobStatus.UPLOADING)  # bytes not staged yet

    resp = await authenticated_client.post(f"/api/internal/agent/files/{file.id}/presign-download")

    assert resp.status_code == 409, resp.text
    assert "not ready" in resp.json()["detail"]


async def test_presign_download_no_cloud_job_returns_409(
    s3_env: str,
    authenticated_client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """A file with no cloud_job row at all returns 409 (nothing was ever staged) -- WR-03."""
    agent, _token = seed_test_agent
    file = await _seed_file(session, agent)  # no cloud_job seeded

    resp = await authenticated_client.post(f"/api/internal/agent/files/{file.id}/presign-download")

    assert resp.status_code == 409, resp.text


@pytest.mark.parametrize("cloud_status", [CloudJobStatus.SUBMITTED, CloudJobStatus.RUNNING])
async def test_presign_download_staged_non_terminal_returns_url(
    s3_env: str,
    authenticated_client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    cloud_status: CloudJobStatus,
) -> None:
    """A running analyze pod (cloud_job SUBMITTED or RUNNING) can still fetch a presigned URL (200).

    Bug 260706-vqz: submit_cloud_job stamps SUBMITTED at Kueue Job creation, BEFORE the pod runs,
    so the pod always observes SUBMITTED/RUNNING -- an UPLOADED-only guard was unreachable for it.
    """
    agent, _token = seed_test_agent
    file = await _seed_file(session, agent)
    await _seed_cloud_job(session, file.id, status=cloud_status)

    resp = await authenticated_client.post(f"/api/internal/agent/files/{file.id}/presign-download")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert str(file.id) in body["download_url"]  # file_id-scoped key in the URL
    assert body["expected_sha256"] == _SHA  # server-sourced, from FileRecord.sha256_hash


@pytest.mark.parametrize("cloud_status", [CloudJobStatus.SUCCEEDED, CloudJobStatus.FAILED])
async def test_presign_download_terminal_status_returns_409(
    s3_env: str,
    authenticated_client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    cloud_status: CloudJobStatus,
) -> None:
    """A terminal cloud_job (SUCCEEDED/FAILED) 409s -- the staged object may already be cleaned up (WR-03)."""
    agent, _token = seed_test_agent
    file = await _seed_file(session, agent)
    await _seed_cloud_job(session, file.id, status=cloud_status)

    resp = await authenticated_client.post(f"/api/internal/agent/files/{file.id}/presign-download")

    assert resp.status_code == 409, resp.text
    assert "not ready" in resp.json()["detail"]


async def test_presign_download_unresolvable_bucket_returns_409(
    s3_env: str,
    authenticated_client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """An UPLOADED row whose ``staging_bucket`` is absent from the registry returns 409, not a dead URL (MKUE-02)."""
    agent, _token = seed_test_agent
    file = await _seed_file(session, agent)
    # UPLOADED but the recorded bucket id is not in the registry -> resolve_bucket_config -> None -> 409.
    session.add(
        CloudJob(id=uuid.uuid4(), file_id=file.id, s3_key=f"phaze-staging/{file.id}", status=CloudJobStatus.UPLOADED.value, staging_bucket="ghost"),
    )
    await session.commit()

    resp = await authenticated_client.post(f"/api/internal/agent/files/{file.id}/presign-download")

    assert resp.status_code == 409, resp.text
    assert "staging bucket" in resp.json()["detail"]
