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
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import boto3
from moto.server import ThreadedMotoServer
import pytest
from sqlalchemy import select

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services import cloud_staging, s3_staging
from phaze.services.enqueue_router import NoActiveAgentError, lane_for_task
from phaze.tasks.s3_upload import UPLOAD_FILE_SAQ_TIMEOUT_SEC, upload_file_saq_timeout_sec
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


@pytest.fixture
def bucket(s3_env: str):  # type: ignore[no-untyped-def]
    """Resolve the single staging BucketConfig from the registry env (MKUE-02 per-file bucket param)."""
    return s3_staging.resolve_bucket_config(get_settings(), "staging")  # type: ignore[arg-type]


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
    bucket,  # type: ignore[no-untyped-def]
) -> None:
    """The producer stages end-to-end: cloud_job row + multipart + presign + one s3_upload enqueue."""
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file_size = _PART_SIZE * 2 + 1  # ceil(file_size / part_size) == 3
    file = await _seed_file(session, fileserver_id, file_size=file_size)
    file_id = file.id
    expected_parts = math.ceil(file_size / _PART_SIZE)

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    # cloud_job row: uploading + file_id-scoped key + multipart upload_id set + recorded staging_bucket.
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value
    assert job.s3_key == f"phaze-staging/{file_id}"
    assert job.upload_id  # multipart initiated
    assert job.staging_bucket == bucket.id  # MKUE-02: the passed bucket is recorded on the row

    # Exactly one s3_upload job enqueued on the fileserver agent's queue.
    queue = task_router.queues[f"{fileserver_id}-io"]
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
    bucket,  # type: ignore[no-untyped-def]
) -> None:
    """The enqueue carries the deterministic s3_upload:<file_id> key and the explicit SAQ timeout."""
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file = await _seed_file(session, fileserver_id, file_size=_PART_SIZE)
    file_id = file.id

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    policy = task_router.queues[f"{fileserver_id}-io"].captured_policy[0]
    assert policy["key"] == f"s3_upload:{file_id}"
    # Single-part file: the scaled timeout equals the retained baseline constant.
    assert policy["timeout"] == UPLOAD_FILE_SAQ_TIMEOUT_SEC


async def test_stage_file_to_s3_scales_timeout_with_part_count(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
) -> None:
    """phaze-g37f: a multi-part upload stamps a SAQ timeout SCALED by the part count, not a fixed cap."""
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file_size = _PART_SIZE * 3  # ceil == 3 parts
    file = await _seed_file(session, fileserver_id, file_size=file_size)

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    policy = task_router.queues[f"{fileserver_id}-io"].captured_policy[0]
    assert policy["timeout"] == upload_file_saq_timeout_sec(3)
    assert policy["timeout"] > UPLOAD_FILE_SAQ_TIMEOUT_SEC  # strictly larger than the single-part cap


async def test_stage_file_to_s3_is_idempotent_on_file_id(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
) -> None:
    """A second stage for the same file_id upserts (unique FK) -- no duplicate cloud_job row."""
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file = await _seed_file(session, fileserver_id, file_size=_PART_SIZE)
    file_id = file.id

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalars().all()
    assert len(rows) == 1  # unique FK on file_id -- the re-stage updated, not duplicated


async def test_stage_file_to_s3_holds_cleanly_with_no_fileserver_agent(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
) -> None:
    """No fileserver online -> NoActiveAgentError surfaces and no half-written cloud_job is committed."""
    # Seed a COMPUTE agent only (to own the file) so the fileserver-scoped select finds nothing.
    compute = await seed_active_agent(session, agent_id="compute-01", kind="compute")
    file = await _seed_file(session, compute.id, file_size=_PART_SIZE)
    file_id = file.id

    task_router = FakeTaskRouter()
    with pytest.raises(NoActiveAgentError):
        await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    assert await _cloud_job(session, file_id) is None  # nothing committed on the clean hold
    assert task_router.queues == {}  # nothing enqueued


async def test_stage_file_to_s3_aborts_orphaned_multipart_when_presign_fails(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-bbwx: presign_upload_parts raising after create_multipart_upload best-effort aborts it.

    Before the fix, this exact failure ordering (create succeeds, presign raises before the
    cloud_job upsert runs) discarded the only record of upload_id -- the multipart was orphaned
    forever. Spies on the real (moto-backed) ``create_multipart_upload``/``abort_multipart_upload``
    calls so the assertion proves the SAME upload_id that was created is the one actually aborted
    against the real S3 SDK (not just that "some abort" ran) -- moto's ``ListMultipartUploads``
    returns static example fixture data unrelated to bucket state, so it cannot be used to verify.
    """
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    file = await _seed_file(session, fileserver.id, file_size=_PART_SIZE)
    file_id = file.id

    real_create = s3_staging.create_multipart_upload
    created_ids: list[str] = []

    async def _spy_create(*args: object, **kwargs: object) -> str:
        upload_id = await real_create(*args, **kwargs)  # type: ignore[arg-type]
        created_ids.append(upload_id)
        return upload_id

    real_abort = s3_staging.abort_multipart_upload
    aborted: list[tuple[object, ...]] = []

    async def _spy_abort(*args: object, **kwargs: object) -> None:
        aborted.append(args)
        await real_abort(*args, **kwargs)  # type: ignore[arg-type]

    async def _boom_presign(*_args: object, **_kwargs: object) -> list[str]:
        raise s3_staging.S3StagingError("presign failed")

    monkeypatch.setattr(s3_staging, "create_multipart_upload", _spy_create)
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", _spy_abort)
    monkeypatch.setattr(s3_staging, "presign_upload_parts", _boom_presign)

    task_router = FakeTaskRouter()
    with pytest.raises(s3_staging.S3StagingError, match="presign failed"):
        await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    assert await _cloud_job(session, file_id) is None  # nothing persisted
    assert len(created_ids) == 1
    assert aborted == [(file_id, created_ids[0], bucket)]  # the exact orphaned upload was aborted

    # The upload_id is now genuinely gone from S3: a raw re-abort surfaces NoSuchUpload, which
    # abort_multipart_upload swallows as an idempotent no-op (no raise -> proves it was aborted).
    await s3_staging.abort_multipart_upload(file_id, created_ids[0], bucket)


async def test_stage_file_to_s3_aborts_orphaned_multipart_when_enqueue_fails(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-bbwx: an enqueue failure inside the SAVEPOINT (the queue.connect()/enqueue() hiccup
    phaze-uciu.3's SAVEPOINT rolls back the upsert for) ALSO best-effort aborts the fresh multipart.

    Before the fix, the SAVEPOINT rollback restored the row to its pre-stage state (no upload_id
    persisted anywhere), orphaning the multipart exactly like the presign-failure path.
    """
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    file = await _seed_file(session, fileserver.id, file_size=_PART_SIZE)
    file_id = file.id

    real_create = s3_staging.create_multipart_upload
    created_ids: list[str] = []

    async def _spy_create(*args: object, **kwargs: object) -> str:
        upload_id = await real_create(*args, **kwargs)  # type: ignore[arg-type]
        created_ids.append(upload_id)
        return upload_id

    real_abort = s3_staging.abort_multipart_upload
    aborted: list[tuple[object, ...]] = []

    async def _spy_abort(*args: object, **kwargs: object) -> None:
        aborted.append(args)
        await real_abort(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(s3_staging, "create_multipart_upload", _spy_create)
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", _spy_abort)

    task_router = FakeTaskRouter()
    queue = task_router.queue_for(fileserver.id, lane_for_task("s3_upload"))

    async def _boom_enqueue(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("SAQ pool hiccup")

    queue.enqueue = _boom_enqueue  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    assert await _cloud_job(session, file_id) is None  # SAVEPOINT rolled back the upsert
    assert len(created_ids) == 1
    assert aborted == [(file_id, created_ids[0], bucket)]  # the exact orphaned upload was aborted


async def test_stage_file_to_s3_logs_but_does_not_raise_when_abort_itself_fails(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed best-effort abort (e.g. network blip) never masks the ORIGINAL failure.

    The lifecycle backstop (phaze-sqpv) is the last resort when the compensating abort itself
    cannot reach S3; the caller must still see the original error, not an abort-related one.
    """
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    file = await _seed_file(session, fileserver.id, file_size=_PART_SIZE)

    async def _boom_presign(*_args: object, **_kwargs: object) -> list[str]:
        raise s3_staging.S3StagingError("presign failed")

    async def _boom_abort(*_args: object, **_kwargs: object) -> None:
        raise s3_staging.S3StagingError("abort also failed")

    monkeypatch.setattr(s3_staging, "presign_upload_parts", _boom_presign)
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", _boom_abort)

    task_router = FakeTaskRouter()
    with pytest.raises(s3_staging.S3StagingError, match="presign failed"):
        await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)


async def test_redrive_upload_aborts_old_multipart_and_restages(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
) -> None:
    """redrive_upload aborts the prior multipart (best-effort) and re-stages onto the RECORDED bucket."""
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file = await _seed_file(session, fileserver_id, file_size=_PART_SIZE)
    file_id = file.id

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)
    first_upload_id = (await _cloud_job(session, file_id)).upload_id  # type: ignore[union-attr]

    # redrive resolves the bucket from the RECORDED cloud_job.staging_bucket (MKUE-02) -- no bucket arg.
    await cloud_staging.redrive_upload(session, file, task_router)

    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value
    assert job.upload_id != first_upload_id  # a fresh multipart was initiated
    assert job.staging_bucket == bucket.id  # re-staged onto the same recorded bucket
    # Two enqueues total (original + re-drive); only one cloud_job row (idempotent FK).
    assert len(task_router.queues[f"{fileserver_id}-io"].captured) == 2
    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalars().all()
    assert len(rows) == 1


async def test_redrive_upload_does_not_commit(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-j2tm: redrive_upload MUST NOT commit -- it calls the no-commit core so the /failed
    handler's transaction-scoped advisory lock survives through the attempt stamp. A commit here would
    release the lock mid-handler and let a concurrent /failed lose an increment (defeating the cap).
    """
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    file = await _seed_file(session, fileserver.id, file_size=_PART_SIZE)
    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)  # first stage commits

    from unittest.mock import AsyncMock

    commit_spy = AsyncMock()
    monkeypatch.setattr(session, "commit", commit_spy)

    await cloud_staging.redrive_upload(session, file, task_router)

    commit_spy.assert_not_awaited()  # the caller owns the single commit (lock stays held)


def test_redrive_bucket_falls_back_to_repick_over_backend_set_when_staging_bucket_absent(s3_env: str) -> None:
    """A row missing ``staging_bucket`` (legacy / cleared) re-picks deterministically over its backend's bound set."""
    cfg = get_settings()
    file = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(staging_bucket=None, backend_id="cluster-01")  # cluster-01 is bound to ["staging"] by s3_env
    resolved = cloud_staging._redrive_bucket(cfg, existing, file)  # type: ignore[arg-type]
    assert resolved is not None
    # pick_bucket over the single-element ["staging"] set is deterministically "staging".
    assert resolved.id == s3_staging.pick_bucket(file.id, ["staging"])
    assert resolved.id == "staging"


def test_redrive_bucket_returns_none_when_no_recorded_bucket_and_no_resolvable_backend(s3_env: str) -> None:
    """No recorded ``staging_bucket`` AND no usable backend (None or unknown id) resolves to None (the raise-path input)."""
    cfg = get_settings()
    file = SimpleNamespace(id=uuid.uuid4())
    # backend_id absent entirely
    assert cloud_staging._redrive_bucket(cfg, SimpleNamespace(staging_bucket=None, backend_id=None), file) is None  # type: ignore[arg-type]
    # backend_id set but not present in the resolved registry
    assert cloud_staging._redrive_bucket(cfg, SimpleNamespace(staging_bucket=None, backend_id="ghost"), file) is None  # type: ignore[arg-type]
    # existing row absent entirely
    assert cloud_staging._redrive_bucket(cfg, None, file) is None  # type: ignore[arg-type]


async def test_redrive_upload_raises_when_no_staging_bucket_resolvable(
    s3_env: str,
    session: AsyncSession,
) -> None:
    """redrive_upload fails loudly (never a dead re-stage) when the row has no recorded bucket and no usable backend."""
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    file = await _seed_file(session, fileserver.id, file_size=_PART_SIZE)
    # A cloud_job row with neither a recorded staging_bucket nor a resolvable backend_id -> _redrive_bucket is None.
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            s3_key=s3_staging.staged_object_key(file.id),
            status=CloudJobStatus.UPLOADING.value,
            upload_id=None,
            staging_bucket=None,
            backend_id=None,
        )
    )
    await session.commit()

    task_router = FakeTaskRouter()
    with pytest.raises(s3_staging.S3StagingError, match="could not resolve a staging bucket"):
        await cloud_staging.redrive_upload(session, file, task_router)
    assert task_router.queues == {}  # nothing enqueued on the loud failure
