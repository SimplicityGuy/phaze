"""Recovery must never re-enqueue a job that is GUARANTEED to fail (phaze-71nz).

``recover_orphaned_work`` replays each orphaned ``scheduling_ledger`` row's STORED payload verbatim.
On 2026-07-31 an operator Recover did that to 430 orphaned ``s3_upload`` rows whose payloads embedded
presigned S3 PUT URLs signed at the ORIGINAL enqueue. 428 ran their retries out to terminal ``failed``
against the object store (122x HTTP 403, 257x HTTP 400); zero succeeded. The HTTP layer returned 200
and "Recovery started — re-enqueuing any orphaned work across all stages" throughout.

These tests drive that scenario through the REAL producer against a REAL (wire-compatible
``ThreadedMotoServer``) object store rather than a mocked S3 client, because the failure IS an
object-store response -- a mocked client is exactly what would let this regress unnoticed
(acceptance 5). ``tests/analyze/services/backends/test_cloud_staging.py`` established this harness; the setup
fixtures here mirror it deliberately.

WHAT "DEAD CREDENTIALS" MEANS IN THIS HARNESS -- read before changing an assertion. moto does NOT
enforce presigned-URL expiry (a URL minted with ``ExpiresIn=0`` is still honoured), so the presign-TTL
half of the incident cannot be reproduced against it directly; that half is covered in
``tests/analyze/core/test_replay_safety.py``, where the detector is verified against REAL presigned
URLs in both the SigV4 and the SigV2 ``AWSAccessKeyId``/``Expires``/``Signature`` form the live
deployment emitted. What moto DOES reproduce faithfully is the other half of the same production
reality: an orphaned staging upload's multipart is swept -- by ``redrive_upload``'s abort, by the
terminal ``/failed`` handler, or by the ``AbortIncompleteMultipartUpload`` bucket-lifecycle rule this
repo configures (``s3_staging.ensure_bucket_lifecycle_ttl``, phaze-sqpv) -- after which its part URLs
are rejected by the store no matter how well signed they are. So the stored payload is proven dead
BY THE STORE ITSELF before recovery runs, and the regenerated payload is proven live BY THE STORE
after it.

MUTATION: revert the fix (let ``_replay_row`` enqueue the stored ``s3_upload`` payload verbatim) and
``test_recovery_regenerates_presigned_urls_instead_of_replaying_dead_ones`` goes RED on both halves --
the enqueued URLs are the stale ones, and PUTting to them is refused by the object store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

import boto3
import httpx
from moto.server import ThreadedMotoServer
import pytest
from sqlalchemy import select

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services import cloud_staging, s3_staging
from phaze.services.scheduling_ledger import get_ledger_rows, upsert_ledger_entry
from phaze.tasks.reenqueue import recover_orphaned_work
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, FakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


_BUCKET = "phaze-test-staging"
_CREDS = {"aws_access_key_id": "testing", "aws_secret_access_key": "testing"}
_PART_SIZE = 5242880  # 5 MiB (S3 minimum) so part_count is predictable from file_size
_AGENT = "fileserver-01"


# ---------------------------------------------------------------------------
# Harness: a real object store + a real backends.toml bucket registry
# ---------------------------------------------------------------------------


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
    """Drive the registry off a one-kueue-backend backends.toml pointed at the live moto endpoint."""
    from phaze.config import get_settings

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
    from phaze.config import get_settings

    return s3_staging.resolve_bucket_config(get_settings(), "staging")  # type: ignore[arg-type]


def _patch_queue_probes(monkeypatch: pytest.MonkeyPatch, *, inflight: int = 0, live: set[str] | None = None) -> None:
    """Stub the two ``saq_jobs`` probes -- the unit DB carries no SAQ broker table."""

    async def _fake_inflight(_session: AsyncSession) -> int:
        return inflight

    async def _fake_live(_session: AsyncSession) -> set[str]:
        return set(live or set())

    monkeypatch.setattr("phaze.tasks.reenqueue.count_inflight_jobs", _fake_inflight)
    monkeypatch.setattr("phaze.tasks.reenqueue.get_live_job_keys", _fake_live)


def _make_ctx(router: DedupFakeTaskRouter, controller_queue: DedupFakeQueue) -> dict[str, Any]:
    """Build a controller-shaped ctx (mirrors ``test_recovery.py::_make_ctx``)."""
    from phaze.database import async_session

    return {"async_session": async_session, "queue": controller_queue, "task_router": router}


async def _seed_file(session: AsyncSession, *, file_size: int = _PART_SIZE) -> FileRecord:
    """Insert a FileRecord owned by the seeded fileserver agent."""
    uid = uuid.uuid4()
    file = FileRecord(
        id=uid,
        agent_id=_AGENT,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.flac",
        original_filename=f"{uid.hex}.flac",
        current_path=f"/music/{uid.hex}.flac",
        file_type="flac",
        file_size=file_size,
    )
    session.add(file)
    await session.commit()
    return file


async def _stage_and_seed_orphaned_ledger_row(session: AsyncSession, bucket: Any) -> tuple[FileRecord, list[str]]:
    """Stage a file for real, then leave its ``s3_upload`` ledger row ORPHANED and its multipart swept.

    This is the production shape the incident found, assembled from real parts:

    1. ``stage_file_to_s3`` really initiates a multipart on the store and really presigns the part
       URLs -- these are the credentials that end up in the ledger payload;
    2. the payload is written to ``scheduling_ledger`` exactly as the ``before_enqueue`` chokepoint
       would (same key, same kwargs);
    3. the ``cloud_job`` row is moved to terminal ``failed`` -- the row must be OUT of ``IN_FLIGHT``
       and out of ``awaiting`` or recovery's own cloud-ownership exclusions would skip it before
       replay is even reached (phaze-fc2l / SCHED-05 / 83-06);
    4. the multipart is aborted, as the terminal handler / the lifecycle backstop would have done by
       the time a row has sat orphaned long enough to matter. From here the stored part URLs are dead
       to the STORE, not merely to the clock.

    Returns the file and the stale ``part_urls`` now sitting in the ledger.
    """
    file = await _seed_file(session)
    staging_router = FakeTaskRouter()
    await cloud_staging.stage_file_to_s3(session, file, staging_router, bucket)

    _, payload = staging_router.queues[f"{_AGENT}-io"].captured[0]
    stale_urls = list(payload["part_urls"])
    await upsert_ledger_entry(session, key=f"s3_upload:{file.id}", function="s3_upload", kwargs=dict(payload), timeout=3600, retries=0)

    job = (await session.execute(_cloud_job_stmt(file.id))).scalar_one()
    upload_id = job.upload_id
    job.status = CloudJobStatus.FAILED.value
    await session.commit()

    await s3_staging.abort_multipart_upload(file.id, upload_id, bucket)
    return file, stale_urls


def _all_enqueues(router: DedupFakeTaskRouter) -> list[tuple[str, dict[str, Any]]]:
    """Every job enqueued on every per-agent lane queue the router handed out.

    NOT ``router.queues == {}``: the replay path resolves a lane queue BEFORE deciding whether to
    enqueue, so an empty-but-present queue is normal. What must be empty is the set of jobs.
    """
    return [captured for queue in router.queues.values() for captured in queue.captured]


def _cloud_job_stmt(file_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Re-read the file's ``cloud_job`` row, forcing a fresh load (it is mutated by core UPDATE)."""
    return select(CloudJob).where(CloudJob.file_id == file_id).execution_options(populate_existing=True)


# ---------------------------------------------------------------------------
# The regression test (acceptance 1 + 4 + 5)
# ---------------------------------------------------------------------------


async def test_recovery_regenerates_presigned_urls_instead_of_replaying_dead_ones(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An orphaned ``s3_upload`` row recovers with FRESH credentials -- proven against the real store.

    The three assertions are the whole bead:

    1. the STORED payload is dead -- the object store itself refuses a PUT to it. This is what made
       428 of 430 replayed rows terminal;
    2. recovery enqueues DIFFERENT part URLs. This is the mutation-sensitive assertion: revert to
       verbatim replay and it fails immediately, with no dependence on how the store expresses its
       refusal;
    3. the FRESH payload WORKS -- a real part PUT lands and the store returns an ETag. Recovery
       genuinely recovers the stage rather than merely declining to break it (which is why option (a),
       regenerate, was chosen over option (b), exclude ``s3_upload`` from replay).
    """
    await seed_active_agent(session, agent_id=_AGENT, kind="fileserver")
    file, stale_urls = await _stage_and_seed_orphaned_ledger_row(session, bucket)

    # 1. The stored credentials are dead AT THE STORE, before recovery runs a line.
    dead = httpx.put(stale_urls[0], content=b"phaze-part-bytes")
    assert not dead.is_success, f"the stale part URL was accepted ({dead.status_code}) -- the harness is not reproducing a dead upload"

    _patch_queue_probes(monkeypatch)
    router = DedupFakeTaskRouter()
    result = await recover_orphaned_work(_make_ctx(router, DedupFakeQueue("controller")), force=True)

    assert result["stages"]["s3_upload"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert result["unreplayable"] == 0

    task_name, replayed = router.queues[f"{_AGENT}-io"].captured[0]
    assert task_name == "s3_upload"
    assert replayed["file_id"] == str(file.id)

    # 2. NOT the stored credentials.
    assert replayed["part_urls"] != stale_urls
    assert set(replayed["part_urls"]).isdisjoint(stale_urls)

    # 3. The regenerated credentials actually work against the real object store.
    fresh = httpx.put(replayed["part_urls"][0], content=b"phaze-part-bytes")
    assert fresh.is_success, f"the regenerated part URL was refused ({fresh.status_code}: {fresh.text[:200]})"
    assert fresh.headers.get("etag")


async def test_regeneration_re_stages_the_cloud_job_on_a_fresh_multipart(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery drives the SAME ``cloud_job`` row back to UPLOADING on a NEW ``upload_id``.

    The regenerated URLs are only meaningful if the row that tracks them moved with them: the agent's
    ``/uploaded`` callback completes the multipart named by ``cloud_job.upload_id``, so a fresh presign
    against a stale recorded ``upload_id`` would be a second, quieter version of the same defect.
    Reusing ``redrive_upload`` -- rather than hand-rolling a presign inside recovery -- is what makes
    this hold for free, and is the reason option (a) does not duplicate staging logic.
    """
    await seed_active_agent(session, agent_id=_AGENT, kind="fileserver")
    file, _ = await _stage_and_seed_orphaned_ledger_row(session, bucket)
    before = (await session.execute(_cloud_job_stmt(file.id))).scalar_one()
    stale_upload_id = before.upload_id

    _patch_queue_probes(monkeypatch)
    await recover_orphaned_work(_make_ctx(DedupFakeTaskRouter(), DedupFakeQueue("controller")), force=True)

    after = (await session.execute(_cloud_job_stmt(file.id))).scalar_one()
    assert after.status == CloudJobStatus.UPLOADING.value
    assert after.upload_id != stale_upload_id
    assert after.staging_bucket == bucket.id


# ---------------------------------------------------------------------------
# The deliberate, VISIBLE skip (acceptance 3)
# ---------------------------------------------------------------------------


async def test_regeneration_without_an_online_fileserver_is_unreplayable_not_reenqueued(
    s3_env: str,
    session: AsyncSession,
    bucket,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fileserver to re-presign against -> ``unreplayable``, zero enqueues, and the ledger row kept.

    The counter has to be its OWN outcome, not folded into ``skipped`` (which means "already running",
    a benign state) or ``errored`` (which means "retry it"). This row is neither: the work is real,
    uncovered, and waiting for an agent. Conflating it is how "Recovery started" came to read the same
    for a run that recovered everything and a run that recovered nothing.
    """
    agent = await seed_active_agent(session, agent_id=_AGENT, kind="fileserver")
    file, stale_urls = await _stage_and_seed_orphaned_ledger_row(session, bucket)

    # The owning fileserver goes away between staging and recovery (the cold-boot / revoked case).
    agent.revoked_at = datetime.now(UTC)
    await session.commit()

    _patch_queue_probes(monkeypatch)
    router = DedupFakeTaskRouter()
    result = await recover_orphaned_work(_make_ctx(router, DedupFakeQueue("controller")), force=True)

    assert result["stages"]["s3_upload"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 1}
    assert result["unreplayable"] == 1
    # Nothing enqueued anywhere -- emphatically NOT the stale credentials.
    assert _all_enqueues(router) == []

    rows = await get_ledger_rows(session)
    kept = [r for r in rows if r.key == f"s3_upload:{file.id}"]
    assert len(kept) == 1, "the un-recovered row must stay in the ledger for the next pass"
    assert kept[0].payload["part_urls"] == stale_urls


# ---------------------------------------------------------------------------
# The GENERAL guard: not pinned to s3_upload (acceptance 2)
# ---------------------------------------------------------------------------


async def test_recovery_refuses_any_payload_carrying_time_limited_material(
    s3_env: str,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``process_file`` row carrying a presigned URL is REFUSED -- the guard is about payloads, not names.

    This is acceptance 2 in its strictest form. ``process_file`` is declared time-invariant and has a
    perfectly ordinary payload; here it carries a real presigned URL as a stand-in for the NEXT
    producer to store expiring material. Recovery must refuse it on the payload's own evidence, with
    no entry anywhere naming this function -- otherwise the fix buys exactly one incident's worth of
    safety and the next producer regresses identically.
    """
    await seed_active_agent(session, agent_id=_AGENT, kind="fileserver")
    file = await _seed_file(session)

    client = boto3.client("s3", endpoint_url=s3_env, region_name="us-east-1", **_CREDS)
    presigned = client.generate_presigned_url("get_object", Params={"Bucket": _BUCKET, "Key": "phaze-staging/x"}, ExpiresIn=900)
    payload = {"file_id": str(file.id), "original_path": file.original_path, "agent_id": _AGENT, "scratch_url": presigned}
    await upsert_ledger_entry(session, key=f"process_file:{file.id}", function="process_file", kwargs=payload)
    await session.commit()

    _patch_queue_probes(monkeypatch)
    router = DedupFakeTaskRouter()
    result = await recover_orphaned_work(_make_ctx(router, DedupFakeQueue("controller")), force=True)

    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 1}
    assert result["unreplayable"] == 1
    assert _all_enqueues(router) == []


async def test_a_clean_process_file_row_still_replays_verbatim(
    s3_env: str,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not cost the 2,512 rows that replayed FINE in the same incident run.

    ``process_file`` payloads carry ``file_id`` / paths / caps / ``agent_id`` / ``models_path`` and
    nothing that expires, which is exactly why they recovered correctly on 2026-07-31. An
    over-eager refusal here would convert the incident tool into a no-op, so the negative case is
    locked alongside the positive one.
    """
    await seed_active_agent(session, agent_id=_AGENT, kind="fileserver")
    file = await _seed_file(session)
    payload = {"file_id": str(file.id), "original_path": file.original_path, "file_type": "flac", "agent_id": _AGENT, "models_path": "/models"}
    await upsert_ledger_entry(session, key=f"process_file:{file.id}", function="process_file", kwargs=payload)
    await session.commit()

    _patch_queue_probes(monkeypatch)
    router = DedupFakeTaskRouter()
    result = await recover_orphaned_work(_make_ctx(router, DedupFakeQueue("controller")), force=True)

    assert result["stages"]["process_file"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert result["unreplayable"] == 0
    assert router.queues[f"{_AGENT}-analyze"].captured[0][0] == "process_file"
