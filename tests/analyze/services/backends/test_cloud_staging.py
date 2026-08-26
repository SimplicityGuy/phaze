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
from phaze.tasks.s3_upload import S3_UPLOAD_SAQ_RETRIES, UPLOAD_FILE_SAQ_TIMEOUT_SEC, upload_file_saq_timeout_sec
from tests._queue_fakes import DedupFakeTaskRouter, FakeTaskRouter, seed_active_agent


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


async def test_stage_file_to_s3_scales_presign_ttl_with_part_count(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-pq1fe: a multi-part upload presigns every part with a TTL SCALED to the transfer's own
    sanctioned budget (``upload_file_saq_timeout_sec(part_count)``), not the flat
    ``s3_presign_put_ttl_sec`` default -- the fixed 1h TTL made every part URL for a long-enough
    sequential transfer expire before the agent reached it.
    """
    cfg = get_settings()
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    # Enough parts that the sanctioned per-part budget alone (630s/part) exceeds the flat 3600s TTL
    # default -- the exact shape of the bug: a fixed 1h cap while the sanctioned transfer time scales.
    part_count = 6
    file_size = _PART_SIZE * part_count
    file = await _seed_file(session, fileserver_id, file_size=file_size)

    captured: dict[str, object] = {}
    real_presign = s3_staging.presign_upload_parts

    async def _spy_presign(*args: object, **kwargs: object) -> list[str]:
        captured["expires_in_sec"] = kwargs.get("expires_in_sec")
        return await real_presign(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(s3_staging, "presign_upload_parts", _spy_presign)

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    expected_ttl = max(cfg.s3_presign_put_ttl_sec, upload_file_saq_timeout_sec(part_count))
    assert expected_ttl > cfg.s3_presign_put_ttl_sec  # sanity: the 6-part budget actually widened it
    assert captured["expires_in_sec"] == expected_ttl


async def test_stage_file_to_s3_presign_ttl_never_drops_below_configured_floor(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short (single-part) transfer whose sanctioned budget is well under the configured TTL keeps
    the OPERATOR's configured floor, rather than shrinking the TTL down to the transfer budget.
    """
    cfg = get_settings()
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file = await _seed_file(session, fileserver_id, file_size=_PART_SIZE)  # ceil == 1 part

    captured: dict[str, object] = {}
    real_presign = s3_staging.presign_upload_parts

    async def _spy_presign(*args: object, **kwargs: object) -> list[str]:
        captured["expires_in_sec"] = kwargs.get("expires_in_sec")
        return await real_presign(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(s3_staging, "presign_upload_parts", _spy_presign)

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    assert upload_file_saq_timeout_sec(1) < cfg.s3_presign_put_ttl_sec  # sanity: budget is the smaller one
    assert captured["expires_in_sec"] == cfg.s3_presign_put_ttl_sec


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


async def test_stage_file_to_s3_restage_bumps_updated_at(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
) -> None:
    """phaze-2hv9: a re-stage (ON CONFLICT) resets ``cloud_job.updated_at``, not just status/upload_id.

    ``updated_at`` is the stranded-staging reaper's age clock. Its client-side ``onupdate=func.now()`` is NOT
    injected into an ON CONFLICT DO UPDATE SET, so pre-fix the conflict path left it frozen at the first
    dispatch -- a re-driven upload inherited the entire prior attempt's elapsed time and was reaped live. The
    fix stamps ``updated_at=func.now()`` in the set_. Here the first stamp is forced far into the past; a
    re-stage must pull it back to ~now.
    """
    from sqlalchemy import text as sa_text

    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    file = await _seed_file(session, fileserver.id, file_size=_PART_SIZE)
    file_id = file.id

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    # Force the first stage's clock 10h into the past -- older than any staleness bound.
    await session.execute(
        sa_text("UPDATE cloud_job SET updated_at = now() - make_interval(secs => 36000) WHERE file_id = :fid"),
        {"fid": file_id},
    )
    await session.commit()
    stale = (await _cloud_job(session, file_id)).updated_at  # type: ignore[union-attr]

    # A re-stage must bump the clock forward off that stale value.
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    bumped = (await _cloud_job(session, file_id)).updated_at  # type: ignore[union-attr]
    assert bumped > stale  # the ON CONFLICT path reset the reaper's age clock


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


async def test_stage_file_to_s3_rejects_file_size_exceeding_s3_max_object_size(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-wz1q: an absurd agent-supplied ``file_size`` (unvalidated wire input --
    ``schemas/agent_files.py`` accepts up to ``int64`` and declines a storage-domain cap on purpose)
    is rejected BEFORE a multipart upload is even initiated, instead of driving the unbounded
    presigned-part loop the original defect let through. Spies on ``create_multipart_upload`` to prove
    it is never called: the fail-loud check must run before ANY S3 work, not just before the presign
    loop, else a fresh multipart is orphaned for every rejected file.
    """
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    file = await _seed_file(session, fileserver.id, file_size=s3_staging.S3_MAX_OBJECT_SIZE_BYTES + 1)
    file_id = file.id

    create_calls: list[uuid.UUID] = []

    async def _spy_create(file_id_arg: uuid.UUID, *_a: object, **_kw: object) -> str:
        create_calls.append(file_id_arg)
        return "should-not-be-reached"

    monkeypatch.setattr(s3_staging, "create_multipart_upload", _spy_create)

    task_router = FakeTaskRouter()
    with pytest.raises(s3_staging.S3StagingError, match="exceeding S3's"):
        await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    assert create_calls == []  # no multipart upload was ever initiated
    assert await _cloud_job(session, file_id) is None  # nothing committed
    assert task_router.queues == {}  # nothing enqueued


async def test_stage_file_to_s3_caps_effective_part_size_for_huge_file_size(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-wz1q: a ``file_size`` that would blow past S3's 10,000-part multipart ceiling under the
    CONFIGURED part size instead grows the EFFECTIVE part size so ``part_count`` never exceeds the
    cap -- and the same adjusted size is what gets recorded in the ``s3_upload`` payload, never the
    raw config floor (recording the raw value while presigning against the adjusted count would
    silently corrupt the object: the agent would slice bytes at the wrong boundaries).

    Stubs ``s3_staging.presign_upload_parts`` with a fast fake (real SigV4 signing for ~10k parts
    would make this test slow and it is not what is under test here -- the arithmetic under test lives
    entirely in ``cloud_staging._stage_file_to_s3``, not in the S3 SDK call).
    """
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    # Naively (at the configured 5 MiB floor) this needs ceil(file_size / _PART_SIZE) ~= 15,000
    # parts -- comfortably over S3's 10,000-part ceiling.
    file_size = _PART_SIZE * (s3_staging.S3_MAX_PART_COUNT + 5000)
    file = await _seed_file(session, fileserver_id, file_size=file_size)

    expected_part_size = max(_PART_SIZE, math.ceil(file_size / s3_staging.S3_MAX_PART_COUNT))
    expected_part_count = max(1, math.ceil(file_size / expected_part_size))
    assert expected_part_size > _PART_SIZE  # sanity: the cap actually had to widen the floor
    assert expected_part_count <= s3_staging.S3_MAX_PART_COUNT  # sanity: the widened size respects it

    captured: dict[str, object] = {}

    async def _fake_presign(file_id: uuid.UUID, upload_id: str, part_count: int, bucket_arg: object, **_kw: object) -> list[str]:
        captured["part_count"] = part_count
        return [f"https://example.invalid/{file_id}/{upload_id}/{n}" for n in range(part_count)]

    monkeypatch.setattr(s3_staging, "presign_upload_parts", _fake_presign)

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    assert captured["part_count"] == expected_part_count

    queue = task_router.queues[f"{fileserver_id}-io"]
    _, payload = queue.captured[0]
    assert payload["part_size_bytes"] == expected_part_size  # the ADJUSTED size, never the raw config floor
    assert len(payload["part_urls"]) == expected_part_count


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


async def test_stage_file_to_s3_enqueue_failure_leaves_committed_row_for_redrive(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-bbwx x phaze-grzo (integration): a flush-time enqueue failure is NOT an orphaned multipart.

    Under the parked-enqueue design the enqueue fires only AFTER the cloud_job UPLOADING row (with
    its upload_id) is committed, so an enqueue failure leaves a durable record every cleanup path
    (redrive_upload's abort, the stranded-staging reaper) can find. The best-effort abort
    compensation must therefore NOT fire here -- aborting would leave the committed row pointing at
    a dead upload_id -- and the wrapper must not raise (the flush is best-effort per item).
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

    # No raise: the wrapper commits, then the best-effort flush swallows the enqueue failure.
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    job = await _cloud_job(session, file_id)
    assert job is not None  # the UPLOADING row committed despite the enqueue failure
    assert len(created_ids) == 1
    assert job.upload_id == created_ids[0]  # durable upload_id record -- redrive/reaper can find it
    assert aborted == []  # multipart NOT aborted: recovery owns this row now


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
    # phaze-grzo: redrive PARKS its enqueue on the no-commit core; the caller fires it post-commit.
    assert len(task_router.queues[f"{fileserver_id}-io"].captured) == 1  # not yet fired
    await session.commit()
    assert await cloud_staging.flush_pending_s3_enqueues(session) == 1

    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value
    assert job.upload_id != first_upload_id  # a fresh multipart was initiated
    assert job.staging_bucket == bucket.id  # re-staged onto the same recorded bucket
    # Two enqueues total (original + re-drive); only one cloud_job row (idempotent FK).
    assert len(task_router.queues[f"{fileserver_id}-io"].captured) == 2
    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalars().all()
    assert len(rows) == 1


async def test_stage_file_to_s3_enqueue_pins_retries_to_zero(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
) -> None:
    """phaze-oj7x: the s3_upload enqueue stamps an EXPLICIT retries=0 so SAQ never independently replays.

    The control plane (``/failed`` re-drive + the stranded-staging reaper) is the sole re-drive vehicle. An
    unset ``retries`` would be clobbered to ``worker_max_retries`` (=4) by ``apply_project_job_defaults``,
    re-arming SAQ to replay the ORIGINAL payload against a multipart the re-drive already aborted (a
    guaranteed ``NoSuchUpload`` per part). Pinning it to 0 lets the failing job settle terminal, releasing the
    deterministic key so the next control/reaper enqueue can actually land.
    """
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file = await _seed_file(session, fileserver_id, file_size=_PART_SIZE)

    task_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)

    policy = task_router.queues[f"{fileserver_id}-io"].captured_policy[0]
    assert policy["retries"] == 0
    assert policy["retries"] == S3_UPLOAD_SAQ_RETRIES


async def test_stage_file_to_s3_deduped_enqueue_is_surfaced_not_silently_ignored(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
) -> None:
    """phaze-oj7x: when SAQ dedups the deterministic key (a still-incomplete job), the no-op is LOGGED loudly.

    Pre-fix the ``queue.enqueue`` return was ignored, so a re-drive whose fresh payload was silently dropped
    (deduped against the still-active failed job) was reported as a successful re-drive. The dedup is now
    surfaced at the flush (the only place the enqueue result is observable under the parked design,
    phaze-grzo): no fresh job lands (only one enqueue is captured across two stages) and a warning fires.
    """
    from structlog.testing import capture_logs

    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file = await _seed_file(session, fileserver_id, file_size=_PART_SIZE)

    # DedupFakeTaskRouter models SAQ's ON CONFLICT dedup: the second enqueue of a still-live key returns None.
    task_router = DedupFakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, task_router, bucket)  # lands (key now live)
    with capture_logs() as logs:
        await cloud_staging._stage_file_to_s3(session, file, task_router, bucket)  # parks the enqueue
        await session.commit()
        await cloud_staging.flush_pending_s3_enqueues(session)  # deduped -> None, surfaced here

    queue = task_router.queues[f"{fileserver_id}-io"]
    assert len(queue.captured) == 1  # the second enqueue did NOT land a fresh job
    assert any("deduped against a still-incomplete job" in log.get("event", "") for log in logs)


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


async def test_stage_core_parks_enqueue_until_row_committed(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
) -> None:
    """phaze-grzo: the no-commit core PARKS the s3_upload enqueue; it fires ONLY after a commit+flush.

    Enqueue-before-commit let a fast agent POST /uploaded before the cloud_job UPLOADING row committed,
    so report_uploaded saw no UPLOADING row and no-op'd -- stranding the file. The core must not make
    the job worker-visible until the row it reads is durable.
    """
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id
    file = await _seed_file(session, fileserver_id, file_size=_PART_SIZE)

    task_router = FakeTaskRouter()
    # The no-commit core: upserts the UPLOADING row but must NOT have fired the enqueue yet.
    await cloud_staging._stage_file_to_s3(session, file, task_router, bucket)
    assert task_router.captures == []  # parked, not fired -- no job is worker-visible pre-commit

    await session.commit()
    fired = await cloud_staging.flush_pending_s3_enqueues(session)
    assert fired == 1
    assert len(task_router.queues[f"{fileserver_id}-io"].captured) == 1
    # A second flush is a clean no-op (the parked list was popped) -- never a double-fire.
    assert await cloud_staging.flush_pending_s3_enqueues(session) == 0


async def test_drop_pending_discards_parked_enqueue_without_firing(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
) -> None:
    """phaze-grzo: dropping parked enqueues on a rollback prevents ORPHANING (a job vs a rolled-back row)."""
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    file = await _seed_file(session, fileserver.id, file_size=_PART_SIZE)

    task_router = FakeTaskRouter()
    await cloud_staging._stage_file_to_s3(session, file, task_router, bucket)
    # Simulate the caller rolling the tick back: the upsert AND its parked enqueue must both vanish.
    await cloud_staging.drop_pending_s3_enqueues(session)
    await session.rollback()

    assert await cloud_staging.flush_pending_s3_enqueues(session) == 0
    assert task_router.captures == []  # the orphan job was never fired


async def test_drop_pending_aborts_the_multipart_the_dropped_enqueue_named(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-cws5: dropping a parked enqueue best-effort ABORTS the multipart it named, not just the enqueue.

    Pre-fix, ``drop_pending_s3_enqueues`` discarded the parked enqueue but left the real S3 multipart
    upload ``_stage_file_to_s3`` created untouched -- a rollback on the CALLER's side (a later
    candidate poisoning the transaction, or the caller's own post-loop commit failing) orphaned it
    forever, since ``upload_id`` was never persisted (the row that would have carried it just rolled
    back). Spies on the real (moto-backed) ``abort_multipart_upload`` so the assertion proves the SAME
    upload_id ``create_multipart_upload`` minted is the one actually aborted.
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
    await cloud_staging._stage_file_to_s3(session, file, task_router, bucket)
    assert len(created_ids) == 1

    # Simulate a rollback that happens on the CALLER's side -- outside _stage_file_to_s3's own
    # phaze-bbwx compensation (which only covers a raise INSIDE the core).
    await cloud_staging.drop_pending_s3_enqueues(session)
    await session.rollback()

    assert aborted == [(file_id, created_ids[0], bucket)]
    assert await cloud_staging.flush_pending_s3_enqueues(session) == 0


async def test_drop_pending_abort_failure_does_not_raise(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-cws5: a failing best-effort abort must never mask the original rollback / raise on its own.

    Mirrors the phaze-bbwx compensation's discipline: the abort is best-effort logging only, so a
    wedged/unreachable bucket at drop time can never turn a clean rollback into a crash.
    """
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    file = await _seed_file(session, fileserver.id, file_size=_PART_SIZE)

    async def _boom_abort(*_args: object, **_kwargs: object) -> None:
        raise s3_staging.S3StagingError("bucket unreachable")

    task_router = FakeTaskRouter()
    await cloud_staging._stage_file_to_s3(session, file, task_router, bucket)

    monkeypatch.setattr(s3_staging, "abort_multipart_upload", _boom_abort)

    await cloud_staging.drop_pending_s3_enqueues(session)  # must NOT raise despite the failing abort
    await session.rollback()

    assert await cloud_staging.flush_pending_s3_enqueues(session) == 0


async def test_failed_abort_says_which_compensation_orphaned_the_multipart(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two compensation paths share one abort helper, so the LOG must still tell them apart.

    ``_stage_file_to_s3``'s ``except`` (phaze-bbwx) and ``drop_pending_s3_enqueues``' rollback sweep
    (phaze-cws5) are materially different operational events: the first means THIS staging attempt
    failed, the second means it succeeded and the CALLER's transaction rolled back underneath it --
    usually because of a different candidate in the same drain tick. Before the two ``try``/``except``
    blocks were folded into ``_best_effort_abort_orphaned_multipart`` each carried its own message
    prefix; now a single message serves both, and ``orphaned_by`` is what preserves the distinction.

    ``exc_info`` CANNOT stand in for it. A traceback records only the frames from the catching frame
    downward, so with the ``try``/``except`` living in the shared helper both paths render the
    identical two frames. Without this field an operator greps the log and cannot tell a failed
    staging attempt from a rolled-back drain tick, so this test pins the field rather than the prose.
    """
    from structlog.testing import capture_logs

    async def _boom_abort(*_args: object, **_kwargs: object) -> None:
        raise s3_staging.S3StagingError("bucket unreachable")

    async def _boom_presign(*_args: object, **_kwargs: object) -> list[str]:
        raise s3_staging.S3StagingError("presign failed")

    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    file = await _seed_file(session, fileserver.id, file_size=_PART_SIZE)
    task_router = FakeTaskRouter()

    # caller_rollback: the staging attempt SUCCEEDS, then the caller drops the parked enqueue.
    await cloud_staging._stage_file_to_s3(session, file, task_router, bucket)
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", _boom_abort)
    with capture_logs() as rollback_logs:
        await cloud_staging.drop_pending_s3_enqueues(session)

    # staging_failure: a re-stage of the same file fails in its own compensating except (the upsert
    # is idempotent on file_id, so re-using one file keeps this to the two paths under test).
    monkeypatch.setattr(s3_staging, "presign_upload_parts", _boom_presign)
    with capture_logs() as staging_logs, pytest.raises(s3_staging.S3StagingError, match="presign failed"):
        await cloud_staging._stage_file_to_s3(session, file, task_router, bucket)

    assert [log["orphaned_by"] for log in rollback_logs if "orphaned_by" in log] == ["caller_rollback"]
    assert [log["orphaned_by"] for log in staging_logs if "orphaned_by" in log] == ["staging_failure"]
