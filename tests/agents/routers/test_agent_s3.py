"""Contract tests for the control-side S3 object-staging callbacks (Phase 53, Plan 04).

Two endpoints mirror the ``agent_push.py`` split (report_pushed / report_push_mismatch):

- ``POST /api/internal/agent/s3/{file_id}/uploaded`` -- the file-server agent reports the ordered
  ``(part_number, etag)`` list it collected; control completes the multipart upload CONTROL-SIDE
  (KSTAGE-01/DIST-01 -- never the agent) and flips ``cloud_job`` ``UPLOADING -> UPLOADED`` with a
  rowcount guard (a duplicate/late callback is an idempotent 200 that does NOT re-complete).
- ``POST /api/internal/agent/s3/{file_id}/failed`` -- the agent reports an upload failure; under
  the re-drive cap control re-drives (``cloud_staging.redrive_upload``) and increments the ledger
  attempt counter (cleared=False); at/over the cap control sets ``cloud_job`` FAILED (marking its
  ``attempts`` spent, >= cloud_submit_max_attempts), aborts the multipart, deletes the staged object,
  clears the ledger, and SPILLS the file back to ``AWAITING_CLOUD`` (Phase 69, SCHED-03/D-04) so the
  next drain tick routes it to local -- ANALYSIS_FAILED now comes only from a LOCAL analysis failure
  (cleared=True, KSTAGE-04).

S3 SDK + re-drive calls are monkeypatched (AsyncMocks) so the contract is exercised without a live
S3 backend; the FakeTaskRouter stands in for the per-agent queue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from phaze.config import ControlSettings
from phaze.database import get_session
from phaze.main import create_app
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_s3 import router as agent_s3_router
from phaze.services import cloud_staging, s3_staging
from phaze.services.enqueue_router import NoActiveAgentError
from phaze.services.scheduling_ledger import upsert_ledger_entry
from phaze.tasks.submit_cloud_job import submit_cloud_job_key
from tests._queue_fakes import FakeQueue, FakeTaskRouter


if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


# Phase 67 (REG-04): the S3 callbacks now read ``settings.active_cloud_kind`` (the registry-derived
# transitional accessor), NOT the flat ``cloud_target``. Each test builds a REAL ``ControlSettings``
# from a backends.toml via the shared ``backends_toml_env`` conftest fixture so the accessor is
# exercised end-to-end. A one-kueue registry → ``active_cloud_kind == "kueue"`` (the post-staging
# seam fires); a compute registry → ``active_cloud_kind == "compute"`` (the non-kueue preservation
# case, mirroring the former a1 target).
_KUEUE_REGISTRY = """
    [[backends]]
    kind = "local"
    id = "local"
    rank = 99
    cap = 1

    [[backends]]
    kind = "kueue"
    id = "kueue-cluster"
    rank = 10
    cap = 4
    buckets = ["shared-bucket"]

    [backends.kube]
    api_url = "https://kube.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq"

    [[buckets]]
    id = "shared-bucket"
    scope = "shared"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-staging"
"""

_COMPUTE_REGISTRY = """
    [[backends]]
    kind = "local"
    id = "local"
    rank = 99
    cap = 1

    [[backends]]
    kind = "compute"
    id = "oci-a1"
    rank = 10
    cap = 2
    agent_ref = "compute-agent-01"
    scratch_dir = "/srv/scratch"
    push_host = "oci-a1.push.example"

    [[buckets]]
    id = "shared-bucket"
    scope = "shared"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-staging"
"""


# MKUE-01: two Kueue backends (the literal N-cluster scenario) sharing one bucket. Before Phase 70
# the ``resolved_non_local_kind`` >1-non-local raise 500'd report_uploaded here; the generalized helper
# returns "kueue" so the post-staging seam still fires. Both backends carry their own [kube] cluster.
_TWO_KUEUE_REGISTRY = """
    [[backends]]
    kind = "local"
    id = "local"
    rank = 99
    cap = 1

    [[backends]]
    kind = "kueue"
    id = "kueue-a"
    rank = 10
    cap = 4
    buckets = ["shared-bucket"]

    [backends.kube]
    api_url = "https://kube-a.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq-a"

    [[backends]]
    kind = "kueue"
    id = "kueue-b"
    rank = 20
    cap = 4
    buckets = ["shared-bucket"]

    [backends.kube]
    api_url = "https://kube-b.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq-b"

    [[buckets]]
    id = "shared-bucket"
    scope = "shared"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-staging"
"""


def _patch_settings(monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any, *, kind: str = "kueue") -> None:
    """Build a real ControlSettings off a registry and pin the router's get_settings.

    ``kind="kueue"`` → the S3 post-staging seam fires (``active_cloud_kind == "kueue"``);
    ``kind="two_kueue"`` → the N-Kueue scenario (MKUE-01): the seam still fires, no >1-non-local 500;
    ``kind="compute"`` → the defensive non-kueue guard preserves the cloud_job-only flow.
    """
    registry = {"kueue": _KUEUE_REGISTRY, "two_kueue": _TWO_KUEUE_REGISTRY, "compute": _COMPUTE_REGISTRY}[kind]
    backends_toml_env(registry)
    settings = ControlSettings()
    monkeypatch.setattr("phaze.routers.agent_s3.get_settings", lambda: settings)


def _make_client(
    session: AsyncSession,
    task_router: FakeTaskRouter,
    token: str | None = None,
    *,
    controller_queue: FakeQueue | None = None,
) -> AsyncClient:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_s3_router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.task_router = task_router
    # The k8s report_uploaded path routes submit_cloud_job onto the controller queue via
    # enqueue_router.resolve_queue_for_task (CONTROLLER_TASKS), which reads app.state.controller_queue.
    app.state.controller_queue = controller_queue if controller_queue is not None else FakeQueue("controller")
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
        )
    )
    await session.commit()
    return file_id


async def _seed_cloud_job(
    session: AsyncSession,
    file_id: uuid.UUID,
    *,
    status: CloudJobStatus = CloudJobStatus.UPLOADING,
    cloud_phase: str | None = None,
    staging_bucket: str | None = "shared-bucket",
) -> None:
    # MKUE-02: stamp the recorded staging_bucket so the callbacks (complete / at-cap abort+delete)
    # resolve it to its BucketConfig and act on exactly that bucket.
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file_id,
            s3_key=f"phaze-staging/{file_id}",
            status=status.value,
            upload_id="upload-xyz",
            cloud_phase=cloud_phase,
            staging_bucket=staging_bucket,
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
    backends_toml_env: Any,
) -> None:
    """uploaded -> control completes the multipart + cloud_job UPLOADING -> UPLOADED, 200."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
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
    backends_toml_env: Any,
) -> None:
    """A duplicate /uploaded (cloud_job already UPLOADED) is an idempotent 200 that does NOT re-complete."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
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


async def test_uploaded_unresolvable_staging_bucket_returns_409(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """An UPLOADING row whose recorded staging_bucket does not resolve to a registry BucketConfig -> 409 (never a dead complete)."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id)
    # staging_bucket=None -> resolve_bucket_config returns None -> the 409 guard fires.
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING, staging_bucket=None)
    complete = AsyncMock()
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", complete)

    body = {"parts": [{"part_number": 1, "etag": '"etag-1"'}]}
    async with _make_client(session, FakeTaskRouter(), raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    assert r.status_code == 409, r.text
    complete.assert_not_awaited()  # never complete against an unresolvable bucket


async def test_uploaded_lost_flip_race_is_idempotent_noop(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """The status pre-check passes but the rowcount-guarded UPLOADING->UPLOADED flip matches 0 rows (lost race) -> idempotent 200."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)

    # Simulate the concurrent winner: complete_multipart_upload flips the row to UPLOADED on the shared
    # session, so the handler's own guarded UPDATE ... WHERE status==UPLOADING then matches 0 rows.
    async def _flip_then_return(*_args: Any, **_kwargs: Any) -> None:
        await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.UPLOADED.value))

    monkeypatch.setattr(s3_staging, "complete_multipart_upload", _flip_then_return)

    body = {"parts": [{"part_number": 1, "etag": '"etag-1"'}]}
    async with _make_client(session, FakeTaskRouter(), raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["file_id"] == str(file_id)
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADED.value  # the winner's flip stands


async def test_uploaded_rejects_identity_in_body(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """extra=forbid: a body smuggling file_id/agent_id is a 422 (AUTH-01: file_id is path-only)."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
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


# --- Phase 55 (D-01b): k8s post-staging -- PUSHING->PUSHED flip + routed submit_cloud_job ----


async def test_uploaded_k8s_enqueues_submit_no_state_flip(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """kueue first /uploaded: cloud_job UPLOADING->UPLOADED + ONE routed submit_cloud_job (deterministic key).

    Phase 90 (D-09): the FileRecord PUSHING->PUSHED flip was removed from this seam -- files.state is no
    longer written or read; the cloud_job sidecar is the sole derived authority. The enqueue behaviour is
    unchanged.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env, kind="kueue")
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", AsyncMock())

    controller_queue = FakeQueue("controller")
    body = {"parts": [{"part_number": 1, "etag": '"e1"'}]}
    async with _make_client(session, FakeTaskRouter(), raw_token, controller_queue=controller_queue) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    assert r.status_code == 200, r.text
    # Exactly one submit_cloud_job routed onto the CONTROLLER queue with the deterministic key.
    assert controller_queue.captured == [("submit_cloud_job", {"file_id": str(file_id)})]
    assert controller_queue.captured_policy[0]["key"] == submit_cloud_job_key(file_id)
    # The cloud_job UPLOADING -> UPLOADED flip (now the sole authority) still happened.
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADED.value


async def test_uploaded_two_kueue_enqueues_submit_no_state_flip(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """MKUE-01: with TWO Kueue backends declared, report_uploaded enqueues submit (no state flip).

    The literal N-cluster scenario: before Phase 70 the ``resolved_non_local_kind`` >1-non-local raise
    500'd this hot-path callback (stalling every Kueue file's upload completion). The generalized
    (any-kueue -> "kueue") helper makes it degrade gracefully -- no 500 / no ValueError. Phase 90
    (D-09): the FileRecord PUSHING->PUSHED flip was removed; only the cloud_job UPLOADED flip + submit
    enqueue remain.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env, kind="two_kueue")
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", AsyncMock())

    controller_queue = FakeQueue("controller")
    body = {"parts": [{"part_number": 1, "etag": '"e1"'}]}
    async with _make_client(session, FakeTaskRouter(), raw_token, controller_queue=controller_queue) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    assert r.status_code == 200, r.text  # NOT a 500 despite 2 Kueue backends
    assert controller_queue.captured == [("submit_cloud_job", {"file_id": str(file_id)})]
    assert controller_queue.captured_policy[0]["key"] == submit_cloud_job_key(file_id)
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADED.value


async def test_s3_push_status_transition_idempotent_after_cas_removal(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Phase 90 (D-09, CAS-guard clarification): the FileRecord PUSHING->PUSHED CAS flip was removed
    from the kueue post-staging seam (the ``.where(state==PUSHING)`` READ + ``.values(state=PUSHED)``
    WRITE deleted ATOMICALLY in PR-B). This test PROVES the removal preserved idempotency by driving the
    PUSHING->PUSHED transition endpoint TWICE with the same payload:

    - 1st /uploaded: cloud_job UPLOADING -> UPLOADED (the sole surviving CAS) + ONE routed submit_cloud_job.
    - 2nd /uploaded: the cloud_job is already UPLOADED, so the status pre-check short-circuits to an
      idempotent 200 BEFORE the (now state-flip-free) kueue enqueue block -> NO second submit, no error.

    The removed FileRecord PUSHING guard was therefore NOT load-bearing: the cloud_job sidecar (the
    marker PR-A reads) plus the deterministic submit key are the idempotency authority.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env, kind="kueue")
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", AsyncMock())

    controller_queue = FakeQueue("controller")
    body = {"parts": [{"part_number": 1, "etag": '"e1"'}]}
    async with _make_client(session, FakeTaskRouter(), raw_token, controller_queue=controller_queue) as ac:
        r1 = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)
        r2 = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    # Both calls succeed; the SECOND is an idempotent no-op (no error), not a duplicate effect.
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    # Exactly ONE submit_cloud_job routed across the double call -- the second short-circuited at the
    # cloud_job status pre-check before reaching the enqueue block.
    assert controller_queue.captured == [("submit_cloud_job", {"file_id": str(file_id)})]
    assert controller_queue.captured_policy[0]["key"] == submit_cloud_job_key(file_id)
    # The cloud_job sidecar (the sole derived authority) is terminal exactly once.
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADED.value


async def test_uploaded_non_k8s_preserves_cloud_job_only_behavior(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Non-kueue target (compute): the defensive guard preserves today's cloud_job-only flow (no PUSHED, no submit)."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env, kind="compute")
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", AsyncMock())

    controller_queue = FakeQueue("controller")
    body = {"parts": [{"part_number": 1, "etag": '"e1"'}]}
    async with _make_client(session, FakeTaskRouter(), raw_token, controller_queue=controller_queue) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    assert r.status_code == 200, r.text
    # No FileRecord PUSHED flip, no submit enqueue -- only the cloud_job UPLOADED flip.
    assert controller_queue.captured == []
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADED.value


# ---------------------------------------------------------------------------
# /failed
# ---------------------------------------------------------------------------


async def test_failed_under_cap_redrives_and_increments_counter(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Under the cap: redrive_upload called, cloud_job stays uploading, attempt counter ++ -> cleared=False."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
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


async def test_failed_under_cap_redrive_no_fileserver_holds_uploading(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Under the cap, if no fileserver is online the re-drive raises NoActiveAgentError -> clean 200 hold, cleared=False, cloud_job stays UPLOADING."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    await _seed_ledger(session, file_id, attempt=0)

    async def _raise_no_agent(*_args: Any, **_kwargs: Any) -> None:
        raise NoActiveAgentError("no fileserver online")

    monkeypatch.setattr(cloud_staging, "redrive_upload", _raise_no_agent)

    async with _make_client(session, FakeTaskRouter(), raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/failed", json={"detail": "transfer error"})

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is False  # held for a later re-drive, not cleared
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value  # slot kept


async def test_failed_under_cap_redrive_keeps_fresh_part_urls(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """After a re-drive the ledger retains the FRESH part_urls redrive committed, plus attempt++ (WR-02).

    redrive_upload -> stage_file_to_s3 commits a fresh payload (new presigned part_urls) to the same
    ledger row. The attempt-stamp UPDATE must be built on top of that FRESH payload, not the stale
    snapshot read at the top of the handler -- else recovery replay re-enqueues expired URLs.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    # Seed the ledger with the STALE (prior-attempt) part_urls.
    await upsert_ledger_entry(
        session,
        key=f"s3_upload:{file_id}",
        function="s3_upload",
        kwargs={"file_id": str(file_id), "s3_upload_attempt": 0, "part_urls": ["https://s3.test/stale?partNumber=1"]},
    )
    await session.commit()

    fresh_urls = ["https://s3.test/fresh?partNumber=1"]

    async def _fake_redrive(sess: AsyncSession, _file: FileRecord, _router: FakeTaskRouter) -> None:
        # Mimic stage_file_to_s3's enqueue hook: commit a FRESH payload to the same ledger row.
        await upsert_ledger_entry(
            sess,
            key=f"s3_upload:{file_id}",
            function="s3_upload",
            kwargs={"file_id": str(file_id), "part_urls": fresh_urls},
        )
        await sess.commit()

    monkeypatch.setattr(cloud_staging, "redrive_upload", AsyncMock(side_effect=_fake_redrive))

    async with _make_client(session, FakeTaskRouter(), raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/failed", json={"detail": "transfer error"})

    assert r.status_code == 200, r.text
    row = await _ledger_row(session, f"s3_upload:{file_id}")
    assert row is not None
    # The fresh part_urls survive (NOT clobbered by the stale snapshot) and the counter incremented.
    assert row.payload.get("part_urls") == fresh_urls
    assert row.payload.get("s3_upload_attempt") == 1


async def test_upload_failed_at_cap_spills_to_awaiting_cloud_and_cleans_up(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """At the cap: cloud_job re-stamped to AWAITING (budget spent) + abort + delete + ledger cleared + SPILL to AWAITING_CLOUD.

    Phase 69 (SCHED-03/D-04): a kueue upload that exhausts its re-drive cap no longer hard-fails. The
    file spills back to AWAITING_CLOUD with its cloud budget marked spent (``attempts >=
    cloud_submit_max_attempts``) so the next drain tick routes it to LOCAL -- ANALYSIS_FAILED now comes
    only from a LOCAL analysis failure. Phase 83 (D-03/D-09): the over-cap CAS re-stamps the cloud_job row
    from ``uploading``/``uploaded`` to ``status='awaiting'`` (NOT ``failed``) so the hard shadow invariant
    ``AWAITING_CLOUD => status='awaiting'`` holds. The cleanup (abort multipart + delete staged object +
    ledger clear) and WR-01 cloud_phase=None are PRESERVED; ``cleared`` stays True.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    settings = ControlSettings()  # push_max_attempts=3, cloud_submit_max_attempts=3 (router defaults)
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING, cloud_phase="running")
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
    redrive.assert_not_awaited()  # terminal for this backend, not re-driven
    abort.assert_awaited_once()  # cleanup PRESERVED on the spill path
    delete.assert_awaited_once()
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.AWAITING.value  # D-03: re-stamped to awaiting (NOT failed) so AWAITING_CLOUD => status='awaiting'
    assert job.cloud_phase is None  # WR-01: no longer counts toward the "Running" tile
    assert job.attempts >= settings.cloud_submit_max_attempts  # SCHED-03/D-04: cloud budget spent -> local next tick
    # Phase 90 (D-09): the files.state = AWAITING_CLOUD dual-write was removed; the cloud_job re-stamp
    # to 'awaiting' above is the sole derived spill authority.
    assert await _ledger_row(session, f"s3_upload:{file_id}") is None  # ledger cleared


# --- Phase 83 (SC#2 / T-83-01): the over-cap CAS must NOT clobber an already-advanced cloud_job ------


async def test_upload_failed_cas_noop_on_advanced_cloud_job(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """SC#2 (T-83-01): a late/duplicate over-cap /upload-failed on an already-advanced cloud_job is a FULL no-op.

    The named bug (``agent_s3.py:195``): the over-cap branch wrote ``cloud_job=FAILED`` +
    ``FileRecord.state=AWAITING_CLOUD`` UNGUARDED. An already-``ANALYZED`` file whose cloud_job has
    advanced to ``running``/``succeeded`` (a live/finished Kueue job) must NOT be clobbered back to
    ``AWAITING_CLOUD``. D-09 anchors the CAS on ``cloud_job.status IN ('uploading','uploaded')`` so an
    advanced row matches 0 rows; D-10 makes rowcount==0 a FULL no-op -- NO cloud_job write, NO FileRecord
    write, NO multipart abort, NO ``delete_staged_object`` (which would kill a live download), NO ledger
    clear -- commit and return ``cleared=False``.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    for advanced in (CloudJobStatus.RUNNING, CloudJobStatus.SUCCEEDED):
        file_id = await _seed_file(session, agent.id)
        await _seed_cloud_job(session, file_id, status=advanced)
        await _seed_ledger(session, file_id, attempt=3)  # next attempt (4) exceeds push_max_attempts=3 -> over-cap branch
        abort = AsyncMock()
        delete = AsyncMock()
        redrive = AsyncMock()
        monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
        monkeypatch.setattr(s3_staging, "delete_staged_object", delete)
        monkeypatch.setattr(cloud_staging, "redrive_upload", redrive)

        async with _make_client(session, FakeTaskRouter(), raw_token) as ac:
            r = await ac.post(f"/api/internal/agent/s3/{file_id}/failed", json={"detail": "late/duplicate"})

        assert r.status_code == 200, r.text
        assert r.json()["cleared"] is False  # D-10: full no-op reports nothing cleared
        job = await _cloud_job(session, file_id)
        assert job is not None
        assert job.status == advanced.value, "the advanced cloud_job must be UNCHANGED (CAS matched 0 rows)"
        abort.assert_not_awaited()  # D-10: no multipart abort on the no-op
        delete.assert_not_awaited()  # D-10: no delete_staged_object -- a live Kueue job may be mid-download
        redrive.assert_not_awaited()
        assert await _ledger_row(session, f"s3_upload:{file_id}") is not None, "D-10: the ledger row must NOT be cleared on the no-op"


async def test_upload_failed_over_cap_null_guard_no_file_is_full_noop(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """NULL-GUARD (83-07): an over-cap /failed whose FileRecord is absent is a FULL no-op, not a 500.

    The over-cap spill routes through ``hold_awaiting_cloud``, whose CAS dereferences ``file.id``. If the
    FileRecord load returns ``None`` (unreachable in practice -- ``cloud_job.file_id`` FKs ``files.id`` --
    but the conservative guard is required), the handler must NOT call the helper (which would raise
    ``AttributeError`` -> 500) and instead take the FULL no-op branch: ``cleared=False``, 200.
    """
    _agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    # No file, no cloud_job seeded -- only the ledger at the cap so the over-cap branch is entered.
    file_id = uuid.uuid4()
    await _seed_ledger(session, file_id, attempt=3)  # next attempt (4) exceeds push_max_attempts=3
    abort = AsyncMock()
    delete = AsyncMock()
    redrive = AsyncMock()
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)
    monkeypatch.setattr(cloud_staging, "redrive_upload", redrive)

    async with _make_client(session, FakeTaskRouter(), raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/failed", json={"detail": "no file"})

    assert r.status_code == 200, r.text  # NULL-GUARD: full no-op, never a 500/AttributeError
    assert r.json()["cleared"] is False
    abort.assert_not_awaited()
    delete.assert_not_awaited()
    redrive.assert_not_awaited()
    assert await _ledger_row(session, f"s3_upload:{file_id}") is not None  # no ledger clear on the no-op


# NOTE (92-04): test_failed_concurrent_under_cap_no_lost_update moved to
# tests/integration/test_agent_s3_concurrency.py — its advisory-locked RMW needs two INDEPENDENT
# committed-visible connections, which the hermetic single-connection create_savepoint `session`
# fixture cannot provide.


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
    paths = set(app.openapi()["paths"])
    assert "/api/internal/agent/s3/{file_id}/uploaded" in paths
    assert "/api/internal/agent/s3/{file_id}/failed" in paths
