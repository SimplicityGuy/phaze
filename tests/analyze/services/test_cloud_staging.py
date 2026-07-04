"""Tests for the control-plane cloud-staging producer + re-drive helper (Phase 53, Plan 04).

``stage_file_to_s3`` is the upload-trigger seam: it creates the ``cloud_job`` row, initiates the
multipart upload, presigns the part URLs, and enqueues exactly one ``s3_upload`` job through the
single per-agent enqueue seam (DIST-01/KSTAGE-01). The multipart init + presign run against a
wire-compatible ``ThreadedMotoServer`` (real HTTP); the enqueue is captured by a ``FakeTaskRouter``.

The producer is built + unit-tested here but NOT wired into ``stage_cloud_window`` (Phase 55 owns
that), so these tests drive it directly.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING
import uuid

import boto3
from moto.server import ThreadedMotoServer
import pytest
from sqlalchemy import select

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.services import cloud_staging
from phaze.services.enqueue_router import NoActiveAgentError
from phaze.tasks.s3_upload import UPLOAD_FILE_SAQ_TIMEOUT_SEC
from tests._queue_fakes import FakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


_BUCKET = "phaze-test-staging"
_CREDS = {"aws_access_key_id": "testing", "aws_secret_access_key": "testing"}
_PART_SIZE = 5242880  # 5 MiB (S3 minimum) so part_count is predictable from file_size


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
    """Drive the registry off a one-kueue-backend backends.toml + keep the global part-size knob.

    ``s3_staging`` now reads bucket identity/creds via ``active_bucket`` (REG-04, D-14), so the
    bucket lives in backends.toml; ``PHAZE_S3_MULTIPART_PART_SIZE_BYTES`` is a kept-global tuning
    knob (D-15) still read from ``ControlSettings``.
    """
    monkeypatch.setenv("PHAZE_ROLE", "control")
    monkeypatch.setenv("PHAZE_S3_MULTIPART_PART_SIZE_BYTES", str(_PART_SIZE))
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


async def _seed_file(session: AsyncSession, agent_id: str, *, file_size: int) -> FileRecord:
    """Insert a FileRecord owned by ``agent_id`` with the given size."""
    file = FileRecord(
        id=uuid.uuid4(),
        agent_id=agent_id,
        sha256_hash="a" * 64,
        original_path="/test/music/song.flac",
        original_filename="song.flac",
        current_path="/test/music/song.flac",
        file_type="flac",
        file_size=file_size,
        state=FileState.AWAITING_CLOUD,
    )
    session.add(file)
    await session.commit()
    return file


async def _cloud_job(session: AsyncSession, file_id: uuid.UUID) -> CloudJob | None:
    # populate_existing forces a fresh load of the row (it is mutated via core UPDATE on a re-stage)
    # without expiring other instances in the session (e.g. the live ``file`` the producer reads).
    stmt = select(CloudJob).where(CloudJob.file_id == file_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def test_stage_file_to_s3_creates_cloud_job_presigns_and_enqueues(
    s3_env: str,
    session: AsyncSession,
) -> None:
    """The producer stages end-to-end: cloud_job row + multipart + presign + one s3_upload enqueue."""
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file_size = _PART_SIZE * 2 + 1  # ceil(file_size / part_size) == 3
    file = await _seed_file(session, fileserver_id, file_size=file_size)
    file_id = file.id
    expected_parts = math.ceil(file_size / _PART_SIZE)

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router)

    # cloud_job row: uploading + file_id-scoped key + multipart upload_id set.
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value
    assert job.s3_key == f"phaze-staging/{file_id}"
    assert job.upload_id  # multipart initiated

    # Exactly one s3_upload job enqueued on the fileserver agent's queue.
    queue = task_router.queues[fileserver_id]
    assert len(queue.captured) == 1
    task_name, payload = queue.captured[0]
    assert task_name == "s3_upload"
    assert payload["file_id"] == str(file_id)
    assert payload["part_size_bytes"] == _PART_SIZE
    assert payload["agent_id"] == fileserver_id
    assert len(payload["part_urls"]) == expected_parts  # part_count = ceil(size / part_size)
    for url in payload["part_urls"]:
        assert str(file_id) in url  # file_id-scoped presigned URLs


async def test_stage_file_to_s3_uses_deterministic_key_and_explicit_timeout(
    s3_env: str,
    session: AsyncSession,
) -> None:
    """The enqueue carries the deterministic s3_upload:<file_id> key and the explicit SAQ timeout."""
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file = await _seed_file(session, fileserver_id, file_size=_PART_SIZE)
    file_id = file.id

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router)

    policy = task_router.queues[fileserver_id].captured_policy[0]
    assert policy["key"] == f"s3_upload:{file_id}"
    assert policy["timeout"] == UPLOAD_FILE_SAQ_TIMEOUT_SEC


async def test_stage_file_to_s3_is_idempotent_on_file_id(
    s3_env: str,
    session: AsyncSession,
) -> None:
    """A second stage for the same file_id upserts (unique FK) -- no duplicate cloud_job row."""
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file = await _seed_file(session, fileserver_id, file_size=_PART_SIZE)
    file_id = file.id

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router)
    await cloud_staging.stage_file_to_s3(session, file, task_router)

    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalars().all()
    assert len(rows) == 1  # unique FK on file_id -- the re-stage updated, not duplicated


async def test_stage_file_to_s3_holds_cleanly_with_no_fileserver_agent(
    s3_env: str,
    session: AsyncSession,
) -> None:
    """No fileserver online -> NoActiveAgentError surfaces and no half-written cloud_job is committed."""
    # Seed a COMPUTE agent only (to own the file) so the fileserver-scoped select finds nothing.
    compute = await seed_active_agent(session, agent_id="compute-01", kind="compute")
    file = await _seed_file(session, compute.id, file_size=_PART_SIZE)
    file_id = file.id

    task_router = FakeTaskRouter()
    with pytest.raises(NoActiveAgentError):
        await cloud_staging.stage_file_to_s3(session, file, task_router)

    assert await _cloud_job(session, file_id) is None  # nothing committed on the clean hold
    assert task_router.queues == {}  # nothing enqueued


async def test_redrive_upload_aborts_old_multipart_and_restages(
    s3_env: str,
    session: AsyncSession,
) -> None:
    """redrive_upload aborts the prior multipart (best-effort) and re-stages with a fresh upload."""
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file = await _seed_file(session, fileserver_id, file_size=_PART_SIZE)
    file_id = file.id

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router)
    first_upload_id = (await _cloud_job(session, file_id)).upload_id  # type: ignore[union-attr]

    await cloud_staging.redrive_upload(session, file, task_router)

    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value
    assert job.upload_id != first_upload_id  # a fresh multipart was initiated
    # Two enqueues total (original + re-drive); only one cloud_job row (idempotent FK).
    assert len(task_router.queues[fileserver_id].captured) == 2
    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalars().all()
    assert len(rows) == 1
