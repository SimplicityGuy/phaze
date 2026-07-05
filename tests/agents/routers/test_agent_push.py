"""Contract tests for the control-side push callbacks (Phase 50, Plan 50-05).

Two endpoints mirror the existing ``put_analysis`` / ``report_analysis_failed``
split (RESEARCH §Critical Finding 1):

- ``POST /api/internal/agent/push/{file_id}/pushed``   — the fileserver agent
  reports a successful rsync; control flips the file ``PUSHING -> PUSHED``,
  clears the ``push_file:<id>`` ledger row, and enqueues exactly one
  ``process_file`` job on the COMPUTE queue carrying the ORM-pinned
  ``expected_sha256`` (D-11) and a ``compute_scratch_dir``-rooted
  ``scratch_path`` — all in one committed transaction.
- ``POST /api/internal/agent/push/{file_id}/mismatch`` — the compute agent
  reports a sha256 mismatch; under ``push_max_attempts`` control re-drives
  ``push_file`` on the FILESERVER queue (keeping the PUSHING slot, Open-Q1) and
  increments the ``push_attempt`` counter in the ledger payload; at/over the cap
  control SPILLS the file back to ``AWAITING_CLOUD`` (Phase 69, SCHED-03/D-04)
  with its cloud budget marked spent (``cloud_job.attempts >= cloud_submit_max_attempts``)
  so the next drain tick routes it to local, and clears the ledger — the terminal
  ``ANALYSIS_FAILED`` now comes only from a LOCAL analysis failure.

Smoke-app pattern (mirrors ``test_agent_analysis.py``): a real DB session via the
``session`` fixture, a ``FakeTaskRouter`` on ``app.state``, and a monkeypatched
``get_settings`` returning a REAL ``ControlSettings`` built off a one-compute-backend
``backends.toml`` (via the shared ``backends_toml_env`` conftest fixture) so
``active_compute_scratch_dir`` / ``models_path`` / ``push_max_attempts`` are deterministic
and the Phase-67 registry accessor is exercised end-to-end (REG-04).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from phaze.config import ControlSettings
from phaze.database import get_session
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_push import router as agent_push_router
from phaze.services.scheduling_ledger import upsert_ledger_entry
from tests._queue_fakes import FakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


_SCRATCH_DIR = "/srv/scratch"
_MODELS_PATH = "/models"  # ControlSettings.models_path default

# Phase 67 (REG-04): the push callback builds ``scratch_path`` from ``settings.active_compute_scratch_dir``
# (the registry-derived transitional accessor) instead of the flat ``compute_scratch_dir``. Drive a
# real ControlSettings off a one-compute-backend registry whose ``scratch_dir`` is the sole non-local
# backend's scratch dir, so the accessor resolves to ``_SCRATCH_DIR`` end-to-end.
_COMPUTE_REGISTRY = f"""
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
    scratch_dir = "{_SCRATCH_DIR}"
    push_host = "oci-a1.push.example"
"""


# MKUE-01 (Pitfall 1): the milestone's target deploy -- local + 2 Kueue + 1 compute. Before Phase 70
# ``active_compute_scratch_dir`` reduced through ``_single_non_local`` which raised on ≥2 non-local
# backends, 500ing the /pushed callback. Re-based on a single-COMPUTE reduction, /pushed must resolve
# the compute scratch_dir cleanly here.
_LOCAL_2KUEUE_COMPUTE_REGISTRY = f"""
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
    buckets = ["bkt-a"]

    [backends.kube]
    api_url = "https://kube-a.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq-a"

    [[backends]]
    kind = "kueue"
    id = "kueue-b"
    rank = 20
    cap = 4
    buckets = ["bkt-b"]

    [backends.kube]
    api_url = "https://kube-b.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq-b"

    [[backends]]
    kind = "compute"
    id = "oci-a1"
    rank = 30
    cap = 2
    agent_ref = "compute-agent-01"
    scratch_dir = "{_SCRATCH_DIR}"
    push_host = "oci-a1.push.example"

    [[buckets]]
    id = "bkt-a"
    scope = "cluster-specific"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-a"

    [[buckets]]
    id = "bkt-b"
    scope = "cluster-specific"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-b"
"""


# MCOMP-03/06 (D-06): two DISTINCT compute backends (distinct agent_ref + scratch_dir). /pushed must
# route process_file to the queue named by the file's RECORDED cloud_job.backend_id -- never
# select_active_agent(kind=compute) -- so a file dispatched to backend A lands on A's agent_ref queue
# with A's scratch_dir even when a different compute agent is the "active" one (Pitfall 4 fix).
_TWO_COMPUTE_REGISTRY = """
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
    agent_ref = "compute-agent-a"
    scratch_dir = "/srv/scratch-a"
    push_host = "a.push.example"

    [[backends]]
    kind = "compute"
    id = "oci-a2"
    rank = 20
    cap = 2
    agent_ref = "compute-agent-b"
    scratch_dir = "/srv/scratch-b"
    push_host = "b.push.example"
"""


# D-07 (/mismatch reporter authorization): the /mismatch reporter IS the compute agent running
# process_file, so agent.id must equal the recorded backend's agent_ref. Pin the sole compute backend's
# agent_ref to the seed_test_agent id ("test-agent-01") so the token-authed reporter passes the D-07 gate
# and the re-drive / spill paths can be exercised end-to-end.
_COMPUTE_REPORTER_REGISTRY = f"""
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
    agent_ref = "test-agent-01"
    scratch_dir = "{_SCRATCH_DIR}"
    push_host = "oci-a1.push.example"
"""


def _patch_settings(monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any, *, registry: str = _COMPUTE_REGISTRY) -> None:
    """Pin the router's ``get_settings()`` to a real ControlSettings off ``registry`` (default one-compute).

    ``active_compute_scratch_dir`` resolves to ``_SCRATCH_DIR``; ``models_path`` / ``push_max_attempts``
    take the ControlSettings defaults (``/models`` / 3), matching the module constants above.
    """
    backends_toml_env(registry)
    settings = ControlSettings()
    monkeypatch.setattr("phaze.routers.agent_push.get_settings", lambda: settings)


def _make_app(session: AsyncSession, task_router: FakeTaskRouter) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_push_router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.task_router = task_router
    return app


def _make_client(session: AsyncSession, task_router: FakeTaskRouter, token: str | None = None) -> AsyncClient:
    app = _make_app(session, task_router)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_file(session: AsyncSession, agent_id: str, *, state: FileState = FileState.PUSHING) -> uuid.UUID:
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


async def _seed_push_ledger(session: AsyncSession, file_id: uuid.UUID, *, push_attempt: int | None = None) -> None:
    payload: dict[str, Any] = {"file_id": str(file_id)}
    if push_attempt is not None:
        payload["push_attempt"] = push_attempt
    await upsert_ledger_entry(session, key=f"push_file:{file_id}", function="push_file", kwargs=payload)
    await session.commit()


async def _seed_cloud_job(session: AsyncSession, file_id: uuid.UUID, *, status: CloudJobStatus = CloudJobStatus.SUBMITTED) -> None:
    """Seed the compute cloud_job sidecar row ComputeAgentBackend.dispatch writes (backend_id set, s3_key NULL)."""
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file_id,
            backend_id="oci-a1",
            s3_key=None,
            status=status.value,
        )
    )
    await session.commit()


async def _cloud_job_row(session: AsyncSession, file_id: uuid.UUID) -> CloudJob | None:
    session.expire_all()
    return (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()


async def _ledger_row(session: AsyncSession, key: str) -> SchedulingLedger | None:
    session.expire_all()
    return (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none()


async def _file_row(session: AsyncSession, file_id: uuid.UUID) -> FileRecord:
    session.expire_all()
    return (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()


# ---------------------------------------------------------------------------
# /pushed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pushed_transitions_clears_ledger_and_enqueues_process_file(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """pushed -> PUSHED + push ledger cleared + ONE process_file with pinned sha256 + scratch_path.

    D-06 (record-don't-rederive): process_file routes to the queue named by the file's RECORDED
    cloud_job.backend_id ("oci-a1" -> agent_ref "compute-agent-01"), NOT select_active_agent, with the
    scratch_path built from that backend's scratch_dir.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id)
    # ComputeAgentBackend.dispatch stamped this row (backend_id="oci-a1") when the file was staged.
    await _seed_cloud_job(session, file_id)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_id"] == str(file_id)
    assert body["status"] == "pushed"

    # State advanced + ledger cleared in one transaction.
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.PUSHED
    sha = file_row.sha256_hash  # read before the next expire_all() to avoid a lazy reload
    assert await _ledger_row(session, f"push_file:{file_id}") is None, "push_file ledger row must be cleared"
    # D-08: the recorded cloud_job is terminalized (SUBMITTED -> SUCCEEDED) in the same transaction.
    cloud_job = await _cloud_job_row(session, file_id)
    assert cloud_job is not None
    assert cloud_job.status == CloudJobStatus.SUCCEEDED.value

    # Exactly one process_file enqueued on the RECORDED backend's agent_ref queue with the pinned payload.
    compute_queue = task_router.queues["compute-agent-01"]
    assert len(compute_queue.captured) == 1
    task_name, payload = compute_queue.captured[0]
    assert task_name == "process_file"
    assert payload["expected_sha256"] == sha == "a" * 64
    assert payload["scratch_path"] == f"{_SCRATCH_DIR}/{file_id}.flac"


@pytest.mark.asyncio
async def test_pushed_scratch_path_resolves_under_local_2kueue_1compute(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Pitfall 1: /pushed resolves the compute scratch_path under local + 2 Kueue + 1 compute (no 500).

    Before Phase 70 the ≥2-non-local registry routed ``active_compute_scratch_dir`` through the raising
    ``_single_non_local`` reduction, 500ing this callback. The single-COMPUTE re-base makes /pushed
    resolve the sole compute backend's scratch_dir cleanly even with N Kueue backends present.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env, registry=_LOCAL_2KUEUE_COMPUTE_REGISTRY)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id)
    await _seed_cloud_job(session, file_id)  # backend_id="oci-a1"

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text  # NOT a 500 despite ≥2 non-local backends
    # scratch_path resolved off the RECORDED compute backend's scratch_dir; routed to its agent_ref queue.
    compute_queue = task_router.queues["compute-agent-01"]
    assert len(compute_queue.captured) == 1
    _task_name, payload = compute_queue.captured[0]
    assert payload["scratch_path"] == f"{_SCRATCH_DIR}/{file_id}.flac"


@pytest.mark.asyncio
async def test_pushed_routes_to_recorded_backend_not_active_agent(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """D-06 (Pitfall 4): with two compute backends, /pushed routes to the RECORDED backend, not the active one.

    The file's cloud_job.backend_id names backend A ("oci-a1" -> agent_ref "compute-agent-a",
    scratch_dir "/srv/scratch-a"). Even with a DIFFERENT compute agent online (backend B's
    "compute-agent-b"), process_file must land on A's agent_ref queue with A's scratch_dir -- never B's
    -- so terminalization/scratch/compute-queue attribute to the agent the file was dispatched to.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env, registry=_TWO_COMPUTE_REGISTRY)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id)
    await _seed_cloud_job(session, file_id)  # backend_id="oci-a1" == backend A
    # A different compute agent is the "active" one (backend B's agent_ref) -- routing must ignore it.
    await seed_active_agent(session, agent_id="compute-agent-b", kind="compute")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
    # Routed to backend A's agent_ref queue with A's scratch_dir -- never B's.
    assert "compute-agent-b" not in task_router.queues, "must not route to the active compute agent"
    compute_queue = task_router.queues["compute-agent-a"]
    assert len(compute_queue.captured) == 1
    _task_name, payload = compute_queue.captured[0]
    assert payload["scratch_path"] == f"/srv/scratch-a/{file_id}.flac"


@pytest.mark.asyncio
async def test_pushed_holds_cleanly_when_no_cloud_job(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """D-06: no cloud_job recorded (unattributed file) -> 200 hold (no 500), state stays PUSHING, ledger intact.

    With no cloud_job the file has no attributed compute backend to resolve, so /pushed cannot route
    process_file: it holds cleanly (mirroring the old no-compute-agent hold) so the staging cron /
    recovery re-drives it once the backend is resolvable.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.PUSHING, "held file must stay PUSHING for the staging cron / recovery"
    assert await _ledger_row(session, f"push_file:{file_id}") is not None, "ledger row must survive a hold"
    assert task_router.queues == {}, "nothing enqueued when no compute backend is attributed"


@pytest.mark.asyncio
async def test_pushed_holds_when_backend_id_unresolvable(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """D-06: a cloud_job whose backend_id names no compute backend (operator-removed) -> clean 200 hold.

    resolve_compute_backend returns None for an unknown/removed backend_id, so /pushed holds without
    mutating state -- it never routes to a phantom backend or 500s.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id)
    # A cloud_job pointing at a backend_id absent from the registry (e.g. an operator removed it).
    session.add(CloudJob(id=uuid.uuid4(), file_id=file_id, backend_id="removed-backend", s3_key=None, status=CloudJobStatus.SUBMITTED.value))
    await session.commit()

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.PUSHING
    assert await _ledger_row(session, f"push_file:{file_id}") is not None
    assert task_router.queues == {}, "nothing enqueued for an unresolvable backend_id"


@pytest.mark.asyncio
async def test_pushed_duplicate_callback_is_idempotent_noop(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """WR-02: a duplicate/late /pushed callback must NOT clobber an already-ANALYZED file.

    A push_file SAQ retry can post /pushed twice; if the first committed and process_file has since
    finished (file now ANALYZED), the second callback must be an idempotent no-op -- it must not
    reset the row to PUSHED nor re-enqueue process_file (which would re-trigger CR-01 stranding).
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    # The file has already advanced all the way to ANALYZED (the first callback + analysis ran).
    file_id = await _seed_file(session, agent.id, state=FileState.ANALYZED)
    # Seed the cloud_job so the backend RESOLVES -- this exercises the WR-02 rowcount==0 idempotent guard
    # (not the no-cloud_job hold): with a resolvable backend the flip is still a no-op because the file
    # is no longer PUSHING.
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.SUBMITTED)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
    # State is untouched (NOT reset to PUSHED).
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.ANALYZED
    # Nothing re-enqueued -- the finished file is not re-analyzed.
    assert task_router.queues == {}
    # WR-02: the cloud_job terminalization is gated behind the rowcount guard, so it stays SUBMITTED
    # (NOT flipped to SUCCEEDED) on the idempotent no-op.
    cloud_job = await _cloud_job_row(session, file_id)
    assert cloud_job is not None
    assert cloud_job.status == CloudJobStatus.SUBMITTED.value


@pytest.mark.asyncio
async def test_pushed_missing_auth_returns_401(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """No Authorization header -> 401 (HTTPBearer auto_error)."""
    agent, _ = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, token=None) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mismatch_under_cap_redrives_and_increments_counter(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Under the cap: push_attempt++ + push_file re-enqueued with the RECORDED destination re-stamped.

    D-07: the reporter (seed_test_agent "test-agent-01") matches the recorded backend's agent_ref, so the
    re-drive proceeds. Landmine 1: the re-driven PushFilePayload carries dest_host / dest_scratch_dir /
    dest_ssh_user stamped from the recorded backend -- never a destination-less payload.
    """
    agent, raw_token = seed_test_agent
    # Reporter registry: the compute backend's agent_ref == the reporting agent id so D-07 passes.
    _patch_settings(monkeypatch, backends_toml_env, registry=_COMPUTE_REPORTER_REGISTRY)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id, push_attempt=0)
    await _seed_cloud_job(session, file_id)  # backend_id="oci-a1", agent_ref="test-agent-01"
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id  # capture before any expire_all()

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_id"] == str(file_id)
    assert body["status"] == "mismatch"
    assert body["cleared"] is False, "under the cap the push is re-driven, not cleared"

    # The file keeps its PUSHING slot (Open-Q1).
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.PUSHING

    # push_attempt incremented in the ledger payload (Pitfall 4 -- counter rides the JSONB).
    row = await _ledger_row(session, f"push_file:{file_id}")
    assert row is not None, "the ledger row must be retained on a re-drive"
    assert row.payload.get("push_attempt") == 1

    # push_file re-enqueued on the FILESERVER queue with the deterministic key + the RE-STAMPED destination.
    fileserver_queue = task_router.queues[fileserver_id]
    assert len(fileserver_queue.captured) == 1
    task_name, payload = fileserver_queue.captured[0]
    assert task_name == "push_file"
    assert payload["file_id"] == str(file_id)
    assert payload["agent_id"] == fileserver_id
    # Landmine 1: the re-drive carries the recorded backend's destination, never a destination-less payload.
    assert payload["dest_host"] == "oci-a1.push.example"
    assert payload["dest_scratch_dir"] == _SCRATCH_DIR
    assert fileserver_queue.captured_policy[0]["key"] == f"push_file:{file_id}"
    # WR-03: the re-driven push carries the explicit SAQ job-net timeout (above the asyncio outer
    # guard), matching the staged-path enqueue so the inner<outer<net kill ordering is deterministic.
    from phaze.tasks.push import PUSH_FILE_SAQ_TIMEOUT_SEC

    assert fileserver_queue.captured_policy[0]["timeout"] == PUSH_FILE_SAQ_TIMEOUT_SEC


@pytest.mark.asyncio
async def test_push_mismatch_over_cap_spills_to_awaiting_cloud_and_clears_ledger(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """At/over the cap: state -> AWAITING_CLOUD (spill, SCHED-03) + ledger cleared, one transaction (no re-drive).

    Phase 69 (D-04): a compute push that exhausts its push_max_attempts re-drives no longer hard-fails.
    The file spills back to AWAITING_CLOUD so the next drain tick can route it to local -- ANALYSIS_FAILED
    now comes only from a LOCAL analysis failure. ``cleared`` stays True (the ledger row is cleared).
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    # Already at the cap: the next attempt (4) exceeds push_max_attempts=3.
    await _seed_push_ledger(session, file_id, push_attempt=3)
    await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cleared"] is True, "over the cap the ledger is cleared even though the file spills (not hard-fails)"

    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.AWAITING_CLOUD, "SCHED-03: spill back to AWAITING_CLOUD, never ANALYSIS_FAILED"
    assert file_row.state != FileState.ANALYSIS_FAILED
    assert await _ledger_row(session, f"push_file:{file_id}") is None, "ledger row must be cleared on spill"
    # No re-drive enqueue happened.
    assert task_router.queues == {}
    # A non-compute file (no cloud_job row) simply spills -- the cloud_job UPDATE is a 0-row no-op.
    assert await _cloud_job_row(session, file_id) is None


@pytest.mark.asyncio
async def test_push_mismatch_over_cap_compute_spill_marks_cloud_budget_spent(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """SCHED-03/D-04 compute_spill: a COMPUTE file hitting the cap spills to AWAITING_CLOUD with cloud budget spent.

    ComputeAgentBackend.dispatch writes the cloud_job row as SUBMITTED (in the D-10 in-flight set). On the
    cap spill the row must be terminalized (FAILED, drained from the in-flight set so in_flight_count stays
    honest) AND its ``attempts`` bumped to >= cloud_submit_max_attempts so select_backend excludes cloud on
    the next drain tick and routes the spilled file to LOCAL (D-04 total-cloud budget).
    """
    agent, raw_token = seed_test_agent
    # Reporter registry: the recorded backend's agent_ref == the reporting compute agent so D-07 passes
    # and the over-cap spill runs (a wrong reporter would 403 before any terminalization).
    _patch_settings(monkeypatch, backends_toml_env, registry=_COMPUTE_REPORTER_REGISTRY)
    settings = ControlSettings()  # same defaults the router sees: push_max_attempts=3, cloud_submit_max_attempts=3
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id, push_attempt=3)  # next attempt (4) exceeds push_max_attempts=3
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.SUBMITTED)
    await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is True

    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.AWAITING_CLOUD  # spill, not ANALYSIS_FAILED
    # The compute cloud_job row is terminalized (drained from the D-10 in-flight set) AND its cloud
    # budget is marked spent so the next drain tick routes the file to local (D-04).
    cloud_job = await _cloud_job_row(session, file_id)
    assert cloud_job is not None
    assert cloud_job.status == CloudJobStatus.FAILED.value
    assert cloud_job.attempts >= settings.cloud_submit_max_attempts, "cloud budget must be marked spent -> select_backend picks local"


@pytest.mark.asyncio
async def test_mismatch_holds_when_no_fileserver_agent(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Under the cap, reporter valid, but no fileserver online -> 200 hold, file stays PUSHING, ledger retained."""
    agent, raw_token = seed_test_agent
    # Reporter registry so the backend resolves + D-07 passes: the ONLY reason to hold is the absent fileserver.
    _patch_settings(monkeypatch, backends_toml_env, registry=_COMPUTE_REPORTER_REGISTRY)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id, push_attempt=0)
    await _seed_cloud_job(session, file_id)  # backend_id="oci-a1", agent_ref="test-agent-01"

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is False
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.PUSHING
    assert await _ledger_row(session, f"push_file:{file_id}") is not None
    assert task_router.queues == {}, "nothing enqueued when no fileserver agent is online"


@pytest.mark.asyncio
async def test_mismatch_wrong_reporter_rejected_403(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """D-07 (T-73-07): a compute agent that is NOT the file's dispatched backend agent is rejected 403.

    The file's cloud_job.backend_id names "oci-a1" whose agent_ref is "compute-agent-01" (the default
    registry), but the reporting agent is seed_test_agent ("test-agent-01"). The mismatch is rejected
    with 403 BEFORE any mutation: the file is NOT terminalized/spilled/re-driven, the cloud_job stays
    SUBMITTED, and the ledger row (push_attempt) is untouched -- reject-don't-terminalize.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)  # default registry: agent_ref="compute-agent-01" != reporter
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id, push_attempt=0)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.SUBMITTED)
    await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 403, r.text
    # No mutation: file still PUSHING, cloud_job still SUBMITTED, ledger counter untouched, nothing enqueued.
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.PUSHING
    cloud_job = await _cloud_job_row(session, file_id)
    assert cloud_job is not None
    assert cloud_job.status == CloudJobStatus.SUBMITTED.value
    row = await _ledger_row(session, f"push_file:{file_id}")
    assert row is not None
    assert row.payload.get("push_attempt") == 0, "the ledger counter must be untouched on a rejected reporter"
    assert task_router.queues == {}, "nothing enqueued for a wrong reporter"


@pytest.mark.asyncio
async def test_mismatch_under_cap_holds_when_backend_unattributed(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Landmine 1: an under-cap file with NO attributed backend cannot be re-driven destination-less -> hold.

    With no cloud_job the backend resolves to None, so there is no destination to re-stamp. Rather than
    enqueue a destination-less push (forbidden -- "never a destination-less payload"), /mismatch holds the
    file PUSHING for the staging cron / recovery, mirroring the no-fileserver hold.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id, push_attempt=0)
    # A fileserver IS online -- the hold is due to the missing destination, not a missing fileserver.
    await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is False
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.PUSHING
    row = await _ledger_row(session, f"push_file:{file_id}")
    assert row is not None
    assert row.payload.get("push_attempt") == 0, "no re-drive -> the counter is not incremented"
    assert task_router.queues == {}, "no destination-less push may be enqueued"


@pytest.mark.asyncio
async def test_mismatch_missing_auth_returns_401(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """No Authorization header -> 401 (HTTPBearer auto_error)."""
    agent, _ = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, token=None) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 401
