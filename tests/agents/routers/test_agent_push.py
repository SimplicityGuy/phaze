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
from phaze.models.file import FileRecord
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
    file_id = await _seed_file(session, agent.id)
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

    # Ledger cleared in one transaction; Phase 90 (D-09) removed the files.state = PUSHED dual-write,
    # so the cloud_job terminalization below is the sole derived "pushed" authority.
    file_row = await _file_row(session, file_id)
    sha = file_row.sha256_hash  # read before the next expire_all() to avoid a lazy reload
    assert await _ledger_row(session, f"push_file:{file_id}") is None, "push_file ledger row must be cleared"
    # D-08: the recorded cloud_job is terminalized (SUBMITTED -> SUCCEEDED) in the same transaction.
    cloud_job = await _cloud_job_row(session, file_id)
    assert cloud_job is not None
    assert cloud_job.status == CloudJobStatus.SUCCEEDED.value

    # Exactly one process_file enqueued on the RECORDED backend's agent_ref queue with the pinned payload.
    compute_queue = task_router.queues["compute-agent-01-analyze"]
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
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id)
    await _seed_cloud_job(session, file_id)  # backend_id="oci-a1"

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text  # NOT a 500 despite ≥2 non-local backends
    # scratch_path resolved off the RECORDED compute backend's scratch_dir; routed to its agent_ref queue.
    compute_queue = task_router.queues["compute-agent-01-analyze"]
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
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id)
    await _seed_cloud_job(session, file_id)  # backend_id="oci-a1" == backend A
    # A different compute agent is the "active" one (backend B's agent_ref) -- routing must ignore it.
    await seed_active_agent(session, agent_id="compute-agent-b", kind="compute")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
    # Routed to backend A's agent_ref queue with A's scratch_dir -- never B's.
    assert not any(k.startswith("compute-agent-b") for k in task_router.queues), "must not route to the active compute agent"
    compute_queue = task_router.queues["compute-agent-a-analyze"]
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
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
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
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id)
    # A cloud_job pointing at a backend_id absent from the registry (e.g. an operator removed it).
    session.add(CloudJob(id=uuid.uuid4(), file_id=file_id, backend_id="removed-backend", s3_key=None, status=CloudJobStatus.SUBMITTED.value))
    await session.commit()

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
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
    Phase 83 (SC#1/D-12): the anchor swap keys the guard on cloud_job.status == 'submitted'. The first
    /pushed already re-stamped the row SUBMITTED -> SUCCEEDED (formerly dual-written with the scalar state), so seed the
    cloud_job at SUCCEEDED -- the second callback's CAS matches 0 rows and is a clean idempotent no-op.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    # The file has already advanced all the way to ANALYZED (the first callback + analysis ran).
    file_id = await _seed_file(session, agent.id)
    # Seed the cloud_job so the backend RESOLVES (not the no-cloud_job hold). SC#1/D-12: the row already
    # advanced to SUCCEEDED on the first /pushed, so the second callback's CAS on status=='submitted' is a
    # 0-row idempotent no-op -- NOT because the file is no longer PUSHING (that routing read is now gone).
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.SUCCEEDED)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
    # State is untouched (NOT reset to PUSHED).
    # Nothing re-enqueued -- the finished file is not re-analyzed.
    assert task_router.queues == {}
    # SC#1/D-12: the CAS matched 0 rows (status already SUCCEEDED, not 'submitted'), so the row is UNCHANGED.
    cloud_job = await _cloud_job_row(session, file_id)
    assert cloud_job is not None
    assert cloud_job.status == CloudJobStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_pushed_does_not_clobber_when_cloud_job_not_submitted(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """SC#1/D-12 (T-83-PUSH-CLOBBER): a late/duplicate /pushed whose cloud_job already advanced cannot clobber.

    After the anchor swap the /pushed CAS keys on ``cloud_job.status == 'submitted'`` (compute's single
    in-flight status), NOT ``FileRecord.state == PUSHING``. A file whose cloud_job has already advanced past
    ``submitted`` (here ``succeeded`` -- the first /pushed already ran) must be a clean idempotent 200 even
    if the dual-written ``FileRecord.state`` still reads ``PUSHING``: the CAS matches 0 rows, so NO
    FileRecord PUSHED write, NO ledger clear, NO second process_file enqueue (which would re-trigger CR-01
    stranding). The old ``state == PUSHING`` guard would (wrongly) fire here.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)  # default registry: backend oci-a1 resolves (agent_ref compute-agent-01)
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id)
    # cloud_job already advanced to SUCCEEDED (the first /pushed committed); the dual-written FileRecord
    # still lags at PUSHING -- the exact shape where a state-anchored guard would clobber but a
    # cloud_job.status-anchored guard must not.
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.SUCCEEDED)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
    cloud_job = await _cloud_job_row(session, file_id)
    assert cloud_job is not None
    assert cloud_job.status == CloudJobStatus.SUCCEEDED.value, "the already-advanced cloud_job must be UNCHANGED"
    assert await _ledger_row(session, f"push_file:{file_id}") is not None, "the push_file ledger row must NOT be cleared on the no-op"
    assert task_router.queues == {}, "no second process_file may be enqueued on the idempotent no-op"


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
    file_id = await _seed_file(session, agent.id)

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
    file_id = await _seed_file(session, agent.id)
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

    # push_attempt incremented in the ledger payload (Pitfall 4 -- counter rides the JSONB).
    row = await _ledger_row(session, f"push_file:{file_id}")
    assert row is not None, "the ledger row must be retained on a re-drive"
    assert row.payload.get("push_attempt") == 1

    # push_file re-enqueued on the FILESERVER queue with the deterministic key + the RE-STAMPED destination.
    fileserver_queue = task_router.queues[f"{fileserver_id}-io"]
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
    """At/over the cap: cloud_job -> awaiting (spill, SCHED-03) + ledger cleared, one transaction (no re-drive).

    Phase 69 (D-04): a compute push that exhausts its push_max_attempts re-drives no longer hard-fails.
    The file spills back to AWAITING_CLOUD so the next drain tick can route it to local -- ANALYSIS_FAILED
    now comes only from a LOCAL analysis failure. ``cleared`` stays True (the ledger row is cleared).
    Phase 83 (SC#1/D-12/D-03): the spill CAS anchors on ``cloud_job.status == 'submitted'`` and re-stamps
    the row to ``status='awaiting'`` (NOT 'failed') so the FileRecord dual-write + ledger clear fire behind
    that rowcount -- a compute cloud_job row (seeded 'submitted') is REQUIRED for the spill to fire.
    """
    agent, raw_token = seed_test_agent
    # Reporter registry: the seeded compute cloud_job's agent_ref == the reporting agent so D-07 passes.
    _patch_settings(monkeypatch, backends_toml_env, registry=_COMPUTE_REPORTER_REGISTRY)
    file_id = await _seed_file(session, agent.id)
    # Already at the cap: the next attempt (4) exceeds push_max_attempts=3.
    await _seed_push_ledger(session, file_id, push_attempt=3)
    # SC#1/D-12: the spill CAS keys on cloud_job.status=='submitted' -- seed the compute sidecar row so it fires.
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.SUBMITTED)
    await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cleared"] is True, "over the cap the ledger is cleared even though the file spills (not hard-fails)"

    assert await _ledger_row(session, f"push_file:{file_id}") is None, "ledger row must be cleared on spill"
    # No re-drive enqueue happened.
    assert task_router.queues == {}
    # Phase 90 (D-09): the files.state = AWAITING_CLOUD dual-write was removed; the cloud_job re-stamp
    # below is the sole derived spill authority.
    # D-03: the cloud_job row is re-stamped submitted -> awaiting (NOT failed) so the spill => status='awaiting'.
    cloud_job = await _cloud_job_row(session, file_id)
    assert cloud_job is not None
    assert cloud_job.status == CloudJobStatus.AWAITING.value


@pytest.mark.asyncio
async def test_push_mismatch_over_cap_does_not_clobber_advanced_file(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """CR-01 regression: an over-cap /mismatch on a file that already advanced past PUSHING is an idempotent no-op.

    The WR-02-symmetric CAS guard on the spill (state == PUSHING) prevents a duplicate/late /mismatch -- or a
    stale/unattributed reporter that skipped the D-07 gate (no cloud_job -> backend is None) -- from clobbering
    an ANALYZED/PROPOSED/EXECUTED file back to AWAITING_CLOUD. Nothing is mutated: state, ledger, cloud_job all
    stay put and ``cleared`` is False.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    # Already advanced past PUSHING (a local file that completed analysis), with no attributed compute
    # backend (no cloud_job) so the D-07 reporter gate is skipped -- the exact CR-01 attack shape.
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id, push_attempt=3)  # next attempt (4) exceeds push_max_attempts=3

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is False, "no-op must not report the ledger as cleared"
    assert await _ledger_row(session, f"push_file:{file_id}") is not None, "ledger row must be retained on the no-op"
    assert task_router.queues == {}


@pytest.mark.asyncio
async def test_push_mismatch_over_cap_compute_spill_marks_cloud_budget_spent(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """SCHED-03/D-04 compute_spill: a COMPUTE file hitting the cap spills to AWAITING_CLOUD with cloud budget spent.

    ComputeAgentBackend.dispatch writes the cloud_job row as SUBMITTED (in the D-10 in-flight set). On the
    cap spill the row is re-stamped submitted -> awaiting (Phase 83 D-03: NOT 'failed', so the hard shadow
    invariant AWAITING_CLOUD => status='awaiting' holds -- 'awaiting' is not in IN_FLIGHT, so in_flight_count
    stays honest) AND its ``attempts`` bumped to >= cloud_submit_max_attempts so select_backend excludes
    cloud on the next drain tick and routes the spilled file to LOCAL (D-04 total-cloud budget).
    """
    agent, raw_token = seed_test_agent
    # Reporter registry: the recorded backend's agent_ref == the reporting compute agent so D-07 passes
    # and the over-cap spill runs (a wrong reporter would 403 before any terminalization).
    _patch_settings(monkeypatch, backends_toml_env, registry=_COMPUTE_REPORTER_REGISTRY)
    settings = ControlSettings()  # same defaults the router sees: push_max_attempts=3, cloud_submit_max_attempts=3
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id, push_attempt=3)  # next attempt (4) exceeds push_max_attempts=3
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.SUBMITTED)
    await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is True

    # Phase 90 (D-09): the files.state = AWAITING_CLOUD dual-write was removed; the cloud_job re-stamp
    # below is the sole derived spill authority.
    # D-03: the compute cloud_job row is re-stamped submitted -> awaiting (drained from the D-10 in-flight
    # set) AND its cloud budget is marked spent so the next drain tick routes the file to local (D-04).
    cloud_job = await _cloud_job_row(session, file_id)
    assert cloud_job is not None
    assert cloud_job.status == CloudJobStatus.AWAITING.value
    assert cloud_job.attempts >= settings.cloud_submit_max_attempts, "cloud budget must be marked spent -> select_backend picks local"


@pytest.mark.asyncio
async def test_push_mismatch_over_cap_spill_restamps_cloud_job_to_awaiting(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """SC#1/D-03/D-12: the over-cap /mismatch spill re-stamps a 'submitted' cloud_job to 'awaiting' (NOT 'failed').

    The spill CAS anchors on ``cloud_job.status == 'submitted'`` (compute's single in-flight status, D-12) and
    re-stamps it to ``status='awaiting'`` with ``attempts = cloud_submit_max_attempts`` in the SAME CAS (D-03),
    so the hard shadow invariant ``AWAITING_CLOUD => status='awaiting'`` holds after the spill (the old code
    terminalized to ``failed``, violating it). The FileRecord dual-write to AWAITING_CLOUD + ledger clear are
    gated behind that rowcount.
    """
    agent, raw_token = seed_test_agent
    # Reporter registry: the recorded backend's agent_ref == the reporting compute agent so D-07 passes.
    _patch_settings(monkeypatch, backends_toml_env, registry=_COMPUTE_REPORTER_REGISTRY)
    settings = ControlSettings()  # push_max_attempts=3, cloud_submit_max_attempts=3 (router defaults)
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id, push_attempt=3)  # next attempt (4) exceeds the cap
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.SUBMITTED)  # backend_id oci-a1, agent_ref test-agent-01

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is True
    cloud_job = await _cloud_job_row(session, file_id)
    assert cloud_job is not None
    assert cloud_job.status == CloudJobStatus.AWAITING.value, "D-03: the spill re-stamps to 'awaiting', NOT 'failed'"
    assert cloud_job.attempts >= settings.cloud_submit_max_attempts, "cloud budget stays spent -> select_backend routes to local"
    # Phase 90 (D-09): the files.state = AWAITING_CLOUD dual-write was removed; cloud_job status is authority.
    assert await _ledger_row(session, f"push_file:{file_id}") is None, "ledger row cleared behind the CAS rowcount"


@pytest.mark.asyncio
async def test_push_mismatch_over_cap_does_not_clobber_when_cloud_job_not_submitted(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """SC#1/D-12 (T-83-PUSH-CLOBBER): a late/duplicate over-cap /mismatch whose cloud_job already advanced cannot spill.

    After the anchor swap the over-cap spill CAS keys on ``cloud_job.status == 'submitted'``, NOT
    ``FileRecord.state == PUSHING``. A file whose cloud_job has already advanced past ``submitted`` (here
    ``succeeded``) must be a FULL no-op even if the dual-written ``FileRecord.state`` still reads ``PUSHING``
    and the reporter passes the D-07 gate: the CAS matches 0 rows, so NO cloud_job re-stamp, NO FileRecord
    AWAITING_CLOUD write, NO ledger clear, ``cleared=False``. The old ``state == PUSHING`` guard would
    (wrongly) spill the already-advanced file.
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env, registry=_COMPUTE_REPORTER_REGISTRY)  # D-07 passes (agent_ref test-agent-01)
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id, push_attempt=3)  # next attempt (4) exceeds the cap
    # cloud_job already advanced to SUCCEEDED; the dual-written FileRecord still lags at PUSHING.
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.SUCCEEDED)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is False, "the CAS matched 0 rows -> nothing cleared"
    cloud_job = await _cloud_job_row(session, file_id)
    assert cloud_job is not None
    assert cloud_job.status == CloudJobStatus.SUCCEEDED.value, "the already-advanced cloud_job must be UNCHANGED"
    assert await _ledger_row(session, f"push_file:{file_id}") is not None, "the ledger row must NOT be cleared on the no-op"


@pytest.mark.asyncio
async def test_push_mismatch_over_cap_null_guard_no_file_is_full_noop(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """NULL-GUARD (83-07): an over-cap /mismatch whose FileRecord is absent is a FULL no-op, not a 500.

    The over-cap spill routes through ``hold_awaiting_cloud``, whose CAS dereferences ``file.id``. If the
    FileRecord load returns ``None`` (unreachable in practice -- ``cloud_job.file_id`` FKs ``files.id`` --
    but the conservative guard is required), the handler must NOT call the helper (which would raise
    ``AttributeError`` -> 500) and instead take the FULL no-op branch: ``cleared=False``, 200. No file /
    cloud_job is seeded (so the D-07 reporter gate is skipped, backend is None), only the ledger at the cap.
    """
    _agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, backends_toml_env)
    file_id = uuid.uuid4()
    await _seed_push_ledger(session, file_id, push_attempt=3)  # next attempt (4) exceeds push_max_attempts=3

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text  # NULL-GUARD: full no-op, never a 500/AttributeError
    assert r.json()["cleared"] is False
    assert await _ledger_row(session, f"push_file:{file_id}") is not None  # no ledger clear on the no-op
    assert task_router.queues == {}


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
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id, push_attempt=0)
    await _seed_cloud_job(session, file_id)  # backend_id="oci-a1", agent_ref="test-agent-01"

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is False
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
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id, push_attempt=0)
    await _seed_cloud_job(session, file_id, status=CloudJobStatus.SUBMITTED)
    await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 403, r.text
    # No mutation: file still PUSHING, cloud_job still SUBMITTED, ledger counter untouched, nothing enqueued.
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
    file_id = await _seed_file(session, agent.id)
    await _seed_push_ledger(session, file_id, push_attempt=0)
    # A fileserver IS online -- the hold is due to the missing destination, not a missing fileserver.
    await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is False
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
    file_id = await _seed_file(session, agent.id)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, token=None) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /mismatch — HARD-02 (D-05/D-06): push_attempt RMW atomicity under concurrency
#
# NOTE (92-04): the two cross-connection concurrency cells that lived here —
# test_mismatch_concurrent_no_lost_update and test_mismatch_real_enqueue_hook_does_not_deadlock,
# plus their _GatedQueue/_GatedTaskRouter/_RealHookQueue/_RealHookTaskRouter helpers — moved to
# tests/integration/test_agent_push_concurrency.py. They need two INDEPENDENT committed-visible
# connections for real advisory-lock RMW serialization, which the hermetic single-connection
# create_savepoint `session` fixture cannot provide. The boundary cell below stays (hermetic).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mismatch_cap_trips_exactly_at_boundary(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """D-06: the push_max_attempts cap trips at the exact boundary -- the row-lock change does not shift it.

    With ``push_max_attempts=3`` the cap is ``next_attempt > 3``: at ``push_attempt=2`` the next attempt
    (3) is still UNDER the cap so /mismatch re-drives (cleared=False, file stays PUSHING); at
    ``push_attempt=3`` the next attempt (4) EXCEEDS the cap so /mismatch spills to AWAITING_CLOUD
    (cleared=True). Both sides are asserted to pin the boundary exactly where it was before D-05.
    """
    agent, raw_token = seed_test_agent
    agent_id = agent.id  # capture before any expire_all() (helpers below expire the fixture agent)
    # Reporter registry so the under-cap re-drive side passes the D-07 gate.
    _patch_settings(monkeypatch, backends_toml_env, registry=_COMPUTE_REPORTER_REGISTRY)

    # Just UNDER the cap (push_attempt=2 -> next 3, not > 3): re-drive, file stays PUSHING.
    under_id = await _seed_file(session, agent_id)
    await _seed_push_ledger(session, under_id, push_attempt=2)
    await _seed_cloud_job(session, under_id)  # backend_id="oci-a1", agent_ref="test-agent-01"
    await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r_under = await ac.post(f"/api/internal/agent/push/{under_id}/mismatch")

    assert r_under.status_code == 200, r_under.text
    assert r_under.json()["cleared"] is False, "push_attempt=2 -> next 3 is still under the cap: re-drive, not spill"
    under_ledger = await _ledger_row(session, f"push_file:{under_id}")
    assert under_ledger is not None
    assert under_ledger.payload.get("push_attempt") == 3, "under-cap re-drive increments the counter to 3"

    # At the cap boundary (push_attempt=3 -> next 4 > 3): spill to AWAITING_CLOUD, ledger cleared.
    over_id = await _seed_file(session, agent_id)
    await _seed_push_ledger(session, over_id, push_attempt=3)
    await _seed_cloud_job(session, over_id)  # backend_id="oci-a1", agent_ref="test-agent-01"

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r_over = await ac.post(f"/api/internal/agent/push/{over_id}/mismatch")

    assert r_over.status_code == 200, r_over.text
    assert r_over.json()["cleared"] is True, "push_attempt=3 -> next 4 exceeds the cap: spill to AWAITING_CLOUD"
    # Phase 90 (D-09): the files.state = AWAITING_CLOUD dual-write was removed; the cloud_job re-stamp
    # (submitted -> awaiting) is the sole derived spill authority.
    over_cloud_job = await _cloud_job_row(session, over_id)
    assert over_cloud_job is not None
    assert over_cloud_job.status == CloudJobStatus.AWAITING.value
    assert await _ledger_row(session, f"push_file:{over_id}") is None, "the ledger row is cleared on the cap spill"
