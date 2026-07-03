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

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from phaze.config import ControlSettings
from phaze.database import get_session
from phaze.main import create_app
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_s3 import router as agent_s3_router
from phaze.services import cloud_staging, s3_staging
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
"""


def _patch_settings(monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any, *, kind: str = "kueue") -> None:
    """Build a real ControlSettings off a one-backend registry and pin the router's get_settings.

    ``kind="kueue"`` → the S3 post-staging seam fires (``active_cloud_kind == "kueue"``);
    ``kind="compute"`` → the defensive non-kueue guard preserves the cloud_job-only flow.
    """
    backends_toml_env(_KUEUE_REGISTRY if kind == "kueue" else _COMPUTE_REGISTRY)
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


async def _seed_file(session: AsyncSession, agent_id: str, *, state: str = FileState.AWAITING_CLOUD) -> uuid.UUID:
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
            state=state,
        )
    )
    await session.commit()
    return file_id


async def _file_state(session: AsyncSession, file_id: uuid.UUID) -> str:
    stmt = select(FileRecord).where(FileRecord.id == file_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one().state


async def _seed_cloud_job(
    session: AsyncSession,
    file_id: uuid.UUID,
    *,
    status: CloudJobStatus = CloudJobStatus.UPLOADING,
    cloud_phase: str | None = None,
) -> None:
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file_id,
            s3_key=f"phaze-staging/{file_id}",
            status=status.value,
            upload_id="upload-xyz",
            cloud_phase=cloud_phase,
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


async def test_uploaded_k8s_flips_pushed_and_enqueues_submit(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """kueue first /uploaded: FileRecord PUSHING->PUSHED + ONE routed submit_cloud_job (deterministic key)."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env, kind="kueue")
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", AsyncMock())

    controller_queue = FakeQueue("controller")
    body = {"parts": [{"part_number": 1, "etag": '"e1"'}]}
    async with _make_client(session, FakeTaskRouter(), raw_token, controller_queue=controller_queue) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    assert r.status_code == 200, r.text
    # FileRecord advanced PUSHING -> PUSHED (frees a window slot, RESEARCH Pitfall 1).
    assert await _file_state(session, file_id) == FileState.PUSHED
    # Exactly one submit_cloud_job routed onto the CONTROLLER queue with the deterministic key.
    assert controller_queue.captured == [("submit_cloud_job", {"file_id": str(file_id)})]
    assert controller_queue.captured_policy[0]["key"] == submit_cloud_job_key(file_id)
    # The existing cloud_job UPLOADING -> UPLOADED flip still happened.
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADED.value


async def test_uploaded_k8s_duplicate_is_idempotent_no_resubmit(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """A duplicate/late kueue /uploaded (file already PUSHED) is an idempotent no-op -- no second submit."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env, kind="kueue")
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHED)  # already advanced
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", AsyncMock())

    controller_queue = FakeQueue("controller")
    body = {"parts": [{"part_number": 1, "etag": '"e1"'}]}
    async with _make_client(session, FakeTaskRouter(), raw_token, controller_queue=controller_queue) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    assert r.status_code == 200, r.text
    # PUSHED-flip rowcount==0 -> NO re-enqueue.
    assert controller_queue.captured == []
    assert await _file_state(session, file_id) == FileState.PUSHED


async def test_uploaded_non_k8s_preserves_cloud_job_only_behavior(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Non-kueue target (compute): the defensive guard preserves today's cloud_job-only flow (no PUSHED, no submit)."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env, kind="compute")
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.UPLOADING)
    monkeypatch.setattr(s3_staging, "complete_multipart_upload", AsyncMock())

    controller_queue = FakeQueue("controller")
    body = {"parts": [{"part_number": 1, "etag": '"e1"'}]}
    async with _make_client(session, FakeTaskRouter(), raw_token, controller_queue=controller_queue) as ac:
        r = await ac.post(f"/api/internal/agent/s3/{file_id}/uploaded", json=body)

    assert r.status_code == 200, r.text
    # No FileRecord PUSHED flip, no submit enqueue -- only the cloud_job UPLOADED flip.
    assert await _file_state(session, file_id) == FileState.PUSHING
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


async def test_failed_at_cap_fails_terminally_and_cleans_up(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """At the cap: cloud_job FAILED + abort multipart + delete staged object + ledger cleared -> cleared=True.

    CR-01: the terminal branch MUST also exit the FileRecord from PUSHING -> ANALYSIS_FAILED (else the
    file permanently consumes a window slot and is invisible to backfill). WR-01: it MUST clear
    cloud_phase so a FAILED row never inflates the admission-state "Running" tile.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
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
    redrive.assert_not_awaited()  # terminal, not re-driven
    abort.assert_awaited_once()
    delete.assert_awaited_once()
    job = await _cloud_job(session, file_id)
    assert job is not None
    assert job.status == CloudJobStatus.FAILED.value
    assert job.cloud_phase is None  # WR-01: terminal row no longer counts toward the "Running" tile
    assert await _file_state(session, file_id) == FileState.ANALYSIS_FAILED  # CR-01: exits the window
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
    paths = set(app.openapi()["paths"])
    assert "/api/internal/agent/s3/{file_id}/uploaded" in paths
    assert "/api/internal/agent/s3/{file_id}/failed" in paths
