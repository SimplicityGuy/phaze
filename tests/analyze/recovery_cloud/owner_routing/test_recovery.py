"""Owner-specific fileserver, compute, and controller routing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import event

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.scheduling_ledger import upsert_ledger_entry
from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS
from phaze.tasks.reenqueue import (
    _plan_owner_groups,
    _replay_agent_rows_by_owner,
    recover_orphaned_work,
)
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


_MODELS_PATH = "/models"


class _StubCfg:
    """Minimal stand-in for the control settings recover_orphaned_work reads."""

    def __init__(self, *, models_path: str = _MODELS_PATH, llm_batch_size: int = 10) -> None:
        self.models_path = models_path
        self.llm_batch_size = llm_batch_size


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, llm_batch_size: int = 10) -> None:
    """Pin recover_orphaned_work's get_settings() deterministically (models_path + llm_batch_size)."""
    monkeypatch.setattr("phaze.tasks.reenqueue.get_settings", lambda: _StubCfg(llm_batch_size=llm_batch_size))


def _patch_inflight(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    """Stub the saq_jobs queue-loss detector to report ``value`` in-flight jobs."""

    async def _fake(_session: AsyncSession) -> int:
        return value

    monkeypatch.setattr("phaze.tasks.reenqueue.count_inflight_jobs", _fake)


def _patch_live_keys(monkeypatch: pytest.MonkeyPatch, keys: set[str]) -> None:
    """Stub get_live_job_keys to return a fixed set of live (queued/active) saq_jobs keys."""

    async def _fake(_session: AsyncSession) -> set[str]:
        return set(keys)

    monkeypatch.setattr("phaze.tasks.reenqueue.get_live_job_keys", _fake)


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter, controller_queue: DedupFakeQueue) -> dict[str, Any]:
    """Build a controller-shaped ctx: async_session + controller queue + per-agent dedup router.

    92-04 (CLEAN-02): ``async_session`` is sourced from ``phaze.database.async_session`` -- monkeypatched by the
    ``session`` fixture's ``_route_stats_fanout`` to a factory BOUND to the per-test ``_db_connection``
    (create_savepoint), exactly as the production controller wires ``ctx["async_session"]``. This lets the task
    SEE seeded rows and makes its commits visible to sibling reads under create_savepoint isolation.
    """
    from phaze.database import async_session

    return {"async_session": async_session, "queue": controller_queue, "task_router": router}


def _make_file(*, file_type: str = "mp3") -> FileRecord:
    """Build a fully-populated FileRecord row for the recovery seed."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
    )


def _agent_payload(function: str, file_id: uuid.UUID) -> dict[str, Any]:
    """Build a minimal stored payload an agent-routed ledger row would carry for ``function``."""
    return {
        "file_id": str(file_id),
        "original_path": f"/music/{file_id}.mp3",
        "file_type": "mp3",
        "agent_id": "nox",
    }


async def _seed_ledger(
    session: AsyncSession,
    *,
    function: str,
    file_id: uuid.UUID,
    payload: dict[str, Any] | None = None,
    timeout: int | None = None,
    retries: int | None = None,
) -> str:
    """Upsert one ledger row for ``<function>:<file_id>`` and return its deterministic key."""
    builder = _KEY_BUILDERS[function]
    pay = payload if payload is not None else _agent_payload(function, file_id)
    key = f"{function}:{builder(pay)}"
    await upsert_ledger_entry(session, key=key, function=function, kwargs=pay, timeout=timeout, retries=retries)
    await session.commit()
    return key


async def _seed_awaiting_cloud_job(session: AsyncSession, file_id: uuid.UUID) -> None:
    """Seed a ``cloud_job(status='awaiting')`` sidecar row -- the Phase-83 representation of a parked file."""
    session.add(CloudJob(id=uuid.uuid4(), file_id=file_id, backend_id=None, s3_key=None, status=CloudJobStatus.AWAITING.value))
    await session.commit()


@pytest.mark.asyncio
async def test_orphaned_controller_row_replays_on_controller_queue(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A controller-routed orphaned ledger row replays on ctx["queue"], never an agent queue."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    tl_id = uuid.uuid4()
    await _seed_ledger(session, function="match_tracklist_to_discogs", file_id=tl_id, payload={"tracklist_id": str(tl_id)})

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["match_tracklist_to_discogs"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert [t for t, _ in controller_queue.captured] == ["match_tracklist_to_discogs"]
    assert router.queue_for_calls == []  # never asked for an agent queue


@pytest.mark.asyncio
async def test_controller_row_is_live_keys_only(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A controller row has NO domain predicate: an ANALYZED file (a "done"-looking state) still replays.

    ``submit_cloud_job`` is live-keys-only -- its ledger row is cleared by Plan 01's after_process on
    every terminal outcome, so any row that reaches recovery IS orphaned. The domain-completed
    predicate must NOT apply to it (no scalar-state/pending-set exclusion), so even an ANALYZED file
    replays. (Authored against ``scan_live_set``, moved to its sibling live-keys-only stage when
    phaze-0jpe removed the fingerprint-scan task; the classification under test is identical.)
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()  # would be "done" for analyze, but irrelevant to the controller stage
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="submit_cloud_job", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["submit_cloud_job"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}


@pytest.mark.asyncio
async def test_held_awaiting_cloud_file_is_not_recovered_with_only_a_fileserver(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """83-06 (CLOUDROUTE-02): a held AWAITING_CLOUD file is NEVER re-driven onto a fileserver for local analysis.

    A held long file carries an ``awaiting`` cloud_job (+ possibly a LEGACY process_file ledger row). The
    drain owns it, so recovery EXCLUDES it -- it must never land on the fileserver's analyze lane.
    MUTATION: dropping the ``awaiting_cloud`` exclusion in ``recover_orphaned_work`` lets the legacy row
    fall into ``other_agent_rows`` -> ``nox-analyze`` -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")  # only a fileserver online
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_awaiting_cloud_job(session, f.id)
    await _seed_ledger(session, function="process_file", file_id=f.id)  # a legacy pre-83-06 held row

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # Held awaiting-cloud file -> owned by the drain, excluded from recovery, NEVER analyzed locally.
    assert "nox-analyze" not in router.queues
    assert result["stages"]["process_file"]["reenqueued"] == 0


@pytest.mark.asyncio
async def test_held_awaiting_cloud_file_is_not_recovered_even_with_a_compute_agent(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """83-06 (reverses D-09): a held AWAITING_CLOUD file is NOT recovered even when a COMPUTE agent is online.

    Under the old D-09 model recovery re-drove the held file onto the compute agent; 83-06 makes the
    ``stage_cloud_window`` drain the SINGLE owner, so recovery excludes the held file regardless of which
    agent kind is online (the drain, not the ledger, dispatches it). Companion lock to the fileserver case.
    MUTATION: dropping the ``awaiting_cloud`` exclusion re-drives the legacy row onto ``cloud-analyze`` ->
    a second owner -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="cloud", kind="compute")  # a compute agent online
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_awaiting_cloud_job(session, f.id)
    await _seed_ledger(session, function="process_file", file_id=f.id)  # a legacy pre-83-06 held row

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # The drain owns the held file: recovery re-drives NOTHING (no second owner, no compute re-enqueue).
    assert "cloud-analyze" not in router.queues
    assert result["stages"]["process_file"]["reenqueued"] == 0


@pytest.mark.asyncio
async def test_non_held_process_file_row_still_routes_to_any_agent(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-01 guard: a NON-held process_file row still recovers to any online agent (no over-restrict).

    A normal lost analyze of a short (not-AWAITING_CLOUD) file must keep recovering through the
    kind-agnostic path -- the compute-only restriction applies ONLY to held files.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")  # only a fileserver online
    normal = _make_file()  # not AWAITING_CLOUD, not analyze-done
    session.add(normal)
    await session.commit()
    await _seed_ledger(session, function="process_file", file_id=normal.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # The short file recovers normally onto the only online agent (the fileserver).
    assert "nox-analyze" in router.queues
    assert [str(normal.id)] == [payload["file_id"] for _name, payload in router.queues["nox-analyze"].captured]
    assert result["stages"]["process_file"]["reenqueued"] == 1


@pytest.mark.asyncio
async def test_orphaned_agent_rows_route_to_fileserver_not_a_racing_compute_agent(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-mits: fileserver-local agent rows pin to the fileserver even when a compute agent is more-recently-seen.

    Mixed deployment: a fileserver AND a compute agent are both online, with the compute agent the single
    most-recently-seen non-revoked agent (an unscoped ``last_seen_at DESC`` pick would choose it). The
    ``other_agent_rows`` partition (here extract_file_metadata + process_file, both fileserver-local) must
    route to the FILESERVER's lanes regardless of the heartbeat race -- never onto the media-less compute
    agent's consumer-less ``meta`` lane or its FileNotFound-ing ``analyze`` lane.
    MUTATION: dropping the ``kind="fileserver"`` scope re-routes onto ``cloud-*`` -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    # A fileserver, then a STRICTLY more-recently-seen compute agent (wins an unscoped last_seen_at pick).
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    compute = await seed_active_agent(session, agent_id="cloud", kind="compute")
    compute.last_seen_at = datetime.now(UTC) + timedelta(minutes=1)
    session.add(compute)
    await session.commit()

    meta_file = _make_file()
    proc_file = _make_file()
    session.add_all([meta_file, proc_file])
    await session.commit()
    await _seed_ledger(session, function="extract_file_metadata", file_id=meta_file.id)
    await _seed_ledger(session, function="process_file", file_id=proc_file.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # Both fileserver-local rows land on the FILESERVER's lanes; the compute agent is NEVER addressed.
    assert "nox-meta" in router.queues
    assert "nox-analyze" in router.queues
    assert not any(name.startswith("cloud") for name in router.queues)
    assert result["stages"]["extract_file_metadata"]["reenqueued"] == 1
    assert result["stages"]["process_file"]["reenqueued"] == 1


@pytest.mark.asyncio
async def test_orphaned_agent_rows_skip_when_only_a_compute_agent_is_online(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-mits: with NO fileserver online, fileserver-local rows skip with a WARNING (never a compute misroute).

    The kind-scoped pick raises ``NoActiveAgentError`` when only a compute agent heartbeats, so the rows
    are left for a later recovery once a fileserver returns -- exactly the cold-boot skip posture, never a
    silent enqueue onto a consumer-less compute lane.
    MUTATION: dropping the ``kind="fileserver"`` scope enqueues onto ``cloud-meta`` -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="cloud", kind="compute")  # ONLY a compute agent online
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="extract_file_metadata", file_id=f.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # No fileserver -> the row is skipped (no queue touched), never routed to the compute agent.
    assert router.queues == {}
    assert result["stages"]["extract_file_metadata"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}


def _agent_payload_owned_by(function: str, file_id: uuid.UUID, *, agent_id: str) -> dict[str, Any]:
    """A stored agent-row payload whose OWNING ``agent_id`` is ``agent_id`` (phaze-fjii routing key)."""
    payload = _agent_payload(function, file_id)
    payload["agent_id"] = agent_id
    return payload


@pytest.mark.asyncio
async def test_agent_rows_route_to_each_rows_owning_fileserver(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-fjii: with TWO owning fileservers online, each orphaned row lands on ITS OWNER's lanes.

    Two live fileservers (``fs-a``, ``fs-b``); one orphaned ``process_file`` row owned by ``fs-a`` and one
    orphaned ``extract_file_metadata`` row owned by ``fs-b``. Each must replay onto its OWNER's lane -- the
    process_file onto ``fs-a-analyze`` and the metadata onto ``fs-b-meta`` -- and NEITHER may
    cross-route onto the other owner (whose media mount lacks the path).
    MUTATION: reverting to one shared ``select_active_agent(kind="fileserver")`` pick lands BOTH rows on the
    single most-recently-seen fileserver (``fs-b``) -> ``fs-b-analyze`` present, ``fs-a-analyze`` absent -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    # Two fileservers; fs-b is strictly more-recently-seen (the shared-pick winner an old recovery used).
    await seed_active_agent(session, agent_id="fs-a", kind="fileserver")
    fs_b = await seed_active_agent(session, agent_id="fs-b", kind="fileserver")
    fs_b.last_seen_at = datetime.now(UTC) + timedelta(minutes=1)
    session.add(fs_b)
    await session.commit()

    file_a = _make_file()
    file_b = _make_file()
    session.add_all([file_a, file_b])
    await session.commit()
    await _seed_ledger(
        session, function="process_file", file_id=file_a.id, payload=_agent_payload_owned_by("process_file", file_a.id, agent_id="fs-a")
    )
    await _seed_ledger(
        session,
        function="extract_file_metadata",
        file_id=file_b.id,
        payload=_agent_payload_owned_by("extract_file_metadata", file_b.id, agent_id="fs-b"),
    )

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # Each row lands on ITS OWNER's lane; neither cross-routes onto the other fileserver.
    assert "fs-a-analyze" in router.queues
    assert "fs-b-meta" in router.queues
    assert "fs-b-analyze" not in router.queues  # fs-a's process_file never lands on fs-b
    assert "fs-a-meta" not in router.queues  # fs-b's metadata never lands on fs-a
    assert result["stages"]["process_file"]["reenqueued"] == 1
    assert result["stages"]["extract_file_metadata"]["reenqueued"] == 1
    # Exactly one enqueue landed on each owner's queue.
    assert [payload["file_id"] for _, payload in router.queues["fs-a-analyze"].captured] == [str(file_a.id)]
    assert [payload["file_id"] for _, payload in router.queues["fs-b-meta"].captured] == [str(file_b.id)]


@pytest.mark.asyncio
async def test_agent_row_with_offline_owner_is_skipped_not_rerouted(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-fjii: a row whose owner is OFFLINE is skipped -- never rerouted onto another live fileserver.

    Only ``fs-a`` is online; one orphaned ``process_file`` row is owned by ``fs-a`` (recovers) and one by the
    OFFLINE ``fs-b`` (never seeded). The ``fs-b`` row must be SKIPPED -- left for a later recovery once its
    owner returns -- and must NOT be misrouted onto ``fs-a`` (whose mount lacks fs-b's path, the exact
    misroute phaze-c9w9 established the skip contract to prevent).
    MUTATION: reverting to one shared pick reroutes the fs-b-owned row onto ``fs-a`` -> two enqueues on
    ``fs-a-analyze`` -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="fs-a", kind="fileserver")  # fs-b is NOT online

    file_a = _make_file()
    file_b = _make_file()
    session.add_all([file_a, file_b])
    await session.commit()
    await _seed_ledger(
        session, function="process_file", file_id=file_a.id, payload=_agent_payload_owned_by("process_file", file_a.id, agent_id="fs-a")
    )
    await _seed_ledger(
        session, function="process_file", file_id=file_b.id, payload=_agent_payload_owned_by("process_file", file_b.id, agent_id="fs-b")
    )

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # fs-a's row recovers onto fs-a; fs-b's row is skipped -- fs-b's file NEVER lands on fs-a.
    assert "fs-a-analyze" in router.queues
    assert "fs-b-analyze" not in router.queues  # fs-b offline -> its lane is never created
    assert [payload["file_id"] for _, payload in router.queues["fs-a-analyze"].captured] == [str(file_a.id)]
    assert str(file_b.id) not in [payload["file_id"] for _, payload in router.queues["fs-a-analyze"].captured]
    # One recovered (fs-a), one left orphaned (fs-b) -- the skipped row is NOT counted as reenqueued.
    assert result["stages"]["process_file"]["reenqueued"] == 1


@pytest.mark.asyncio
async def test_agent_rows_skip_when_no_active_agent_controller_rows_replay(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No active agent -> agent-routed rows skip (WARNING) while controller-routed rows still replay."""
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    # NO active agent seeded -> select_active_agent raises NoActiveAgentError.
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="process_file", file_id=f.id)  # agent-routed
    tl_id = uuid.uuid4()
    await _seed_ledger(session, function="submit_cloud_job", file_id=tl_id, payload={"file_id": str(tl_id)})  # controller-routed

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # Agent-routed row skipped (zero), controller-routed row replayed.
    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert result["stages"]["submit_cloud_job"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}
    assert router.queue_for_calls == []
    # phaze-fjii: the owning fileserver ("nox") is offline -> its rows skip with a WARNING, never rerouted.
    assert "offline -- rows skipped, not rerouted" in caplog.text.lower()


async def _seed_cloud_job(
    session: AsyncSession,
    file_id: uuid.UUID,
    *,
    status: CloudJobStatus = CloudJobStatus.SUBMITTED,
    backend_id: str = "oci-a1",
) -> None:
    """Seed the compute cloud_job sidecar row ComputeAgentBackend.dispatch writes (backend_id set, s3_key NULL)."""
    session.add(CloudJob(id=uuid.uuid4(), file_id=file_id, backend_id=backend_id, s3_key=None, status=status.value))
    await session.commit()


@pytest.mark.asyncio
async def test_single_owner_in_flight_cloud_job_skips_ledger_recovery(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHED-05: a compute file with an in-flight cloud_job + a process_file ledger row is NOT re-enqueued.

    The backend reconcile / `/pushed` callback is the single owner for any file with a live cloud_job
    row; recovery must exclude it so the file gains no second recovery path (the 44.5k over-enqueue
    incident class). Even with a compute agent online (so the held path COULD otherwise route it),
    the in-flight cloud_job exclusion wins.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)  # genuine queue-loss
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="cloud", kind="compute")
    burst = _make_file()
    session.add(burst)
    await session.commit()
    await _seed_cloud_job(session, burst.id, status=CloudJobStatus.SUBMITTED)  # in-flight -> owned by its callback
    await _seed_ledger(session, function="process_file", file_id=burst.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # The in-flight cloud_job file must NOT be recovered by the ledger -- its callback/reconcile owns it.
    assert "cloud" not in router.queues
    assert result["stages"]["process_file"] == {"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 0}


@pytest.mark.asyncio
async def test_single_owner_terminal_cloud_job_does_not_block_recovery(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHED-05 guard: a file whose cloud_job is TERMINAL (FAILED) is NOT in the in-flight set.

    Only {UPLOADING, UPLOADED, SUBMITTED, RUNNING} are in-flight; a spilled/terminal FAILED row means
    no backend owns the re-drive anymore, so a still-orphaned process_file row must recover (here to the
    online FILESERVER). The exclusion is by in-flight status, not by row presence. phaze-mits: the
    fileserver-local process_file row routes to the FILESERVER (not a compute agent) -- the online agent is
    seeded as kind="fileserver" so this exercises the cloud_job status exclusion, not the agent-kind scope.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = _make_file()
    session.add(held)
    await session.commit()
    await _seed_cloud_job(session, held.id, status=CloudJobStatus.FAILED)  # terminal -> NOT in-flight
    await _seed_ledger(session, function="process_file", file_id=held.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    # Terminal cloud_job -> no backend owner -> the process_file row recovers onto the online fileserver.
    assert "nox-analyze" in router.queues
    assert result["stages"]["process_file"]["reenqueued"] == 1


@pytest.mark.asyncio
async def test_single_owner_no_cloud_job_keeps_held_recovery_path(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHED-05 guard: an orphaned process_file row with NO cloud_job still recovers -- no regression.

    A genuinely-orphaned file (no cloud_job row was ever written) is not owned by any backend callback,
    so recovery must still re-drive its process_file row (here to the online FILESERVER) -- no regression.
    phaze-mits: the fileserver-local process_file row routes to the FILESERVER agent.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = _make_file()
    session.add(held)
    await session.commit()
    # No cloud_job row seeded -- genuinely orphaned.
    await _seed_ledger(session, function="process_file", file_id=held.id)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert "nox-analyze" in router.queues
    assert result["stages"]["process_file"]["reenqueued"] == 1


class _FlakyOnceQueue(DedupFakeQueue):
    """A :class:`DedupFakeQueue` whose ``enqueue`` raises for exactly one deterministic ``key``.

    Models a transient ``queue.enqueue``/Postgres round-trip failure for one row in an otherwise
    healthy replay batch (phaze-o1xx's failure scenario), while every other key enqueues normally.
    """

    def __init__(self, name: str, *, fail_key: str) -> None:
        super().__init__(name)
        self._fail_key = fail_key
        self.enqueue_attempts: list[str | None] = []

    async def enqueue(self, task_name: str, **kwargs: Any) -> Any:
        key = kwargs.get("key")
        self.enqueue_attempts.append(key)
        if key == self._fail_key:
            raise RuntimeError("transient postgres error during enqueue round-trip")
        return await super().enqueue(task_name, **kwargs)


@pytest.mark.asyncio
async def test_controller_row_replay_failure_is_isolated_and_other_rows_still_replay(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One controller row's enqueue raising is tallied as ``errored``; the run does not abort.

    Pre-fix, the second row (a genuinely orphaned, healthy row) would NEVER be reached because the
    unguarded loop propagated the first row's exception straight out of ``recover_orphaned_work``.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")

    # phaze-2akf: the failing row was a ``scrape_and_store_tracklist``, which went with the legacy
    # scrape path. Any OTHER controller-routed function proves the same isolation, so it is now a
    # ``submit_cloud_job`` -- the two rows must stay DIFFERENT functions, since the assertions below
    # read two distinct ``stages`` entries.
    failing_fid = uuid.uuid4()
    failing_key = await _seed_ledger(session, function="submit_cloud_job", file_id=failing_fid, payload={"file_id": str(failing_fid)})

    healthy_tl_id = uuid.uuid4()
    healthy_key = await _seed_ledger(
        session, function="match_tracklist_to_discogs", file_id=healthy_tl_id, payload={"tracklist_id": str(healthy_tl_id)}
    )

    router = DedupFakeTaskRouter()
    controller_queue = _FlakyOnceQueue("controller", fail_key=failing_key)
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["submit_cloud_job"] == {"reenqueued": 0, "skipped": 0, "errored": 1, "unreplayable": 0}
    assert result["stages"]["match_tracklist_to_discogs"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}
    # BOTH rows were attempted -- the failure of the first did not short-circuit the second.
    assert set(controller_queue.enqueue_attempts) == {failing_key, healthy_key}
    assert [t for t, _ in controller_queue.captured] == ["match_tracklist_to_discogs"]


@pytest.mark.asyncio
async def test_agent_row_lane_routing_failure_is_isolated(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy agent-routed row whose function has no lane mapping is tallied ``errored``, not fatal.

    Models the verifier-flagged latent shape: a future function rename/removal leaves a ledger row
    behind whose ``function`` no longer resolves via ``lane_for_task`` (currently unreachable through
    the public API -- ``upsert_ledger_entry`` validates via ``routing_for_function`` -- so the row is
    inserted directly, the same way an old row would survive a rename in production). The row's
    routing failure must not abort recovery for any other row.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")

    fid = uuid.uuid4()
    session.add(
        SchedulingLedger(
            key=f"legacy_removed_task:{fid}",
            function="legacy_removed_task",
            routing="agent",
            payload={"file_id": str(fid), "agent_id": "nox"},
        )
    )
    # A second, healthy orphaned row in the SAME run must still replay.
    other_fid = uuid.uuid4()
    await _seed_ledger(session, function="submit_cloud_job", file_id=other_fid, payload=_agent_payload("submit_cloud_job", other_fid))
    await session.commit()

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["legacy_removed_task"] == {"reenqueued": 0, "skipped": 0, "errored": 1, "unreplayable": 0}
    assert result["stages"]["submit_cloud_job"] == {"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}


@pytest.mark.asyncio
async def test_replay_agent_rows_by_owner_skips_rows_with_no_owning_agent_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A row whose payload carries no ``agent_id`` is skipped with a WARNING, never rerouted (phaze-fjii).

    Its true owner is unknowable, so blind-routing it onto some other live agent would repeat the
    exact misroute phaze-fjii fixed. Session/task_router are never touched on this path -- the function
    must return before either is used, proven here by passing sentinels that would blow up on any I/O.
    """
    orphan_row = SchedulingLedger(
        key="process_file:orphan",
        function="process_file",
        routing="agent",
        payload={"file_id": str(uuid.uuid4())},
    )
    stages: dict[str, dict[str, int]] = {}

    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        await _replay_agent_rows_by_owner(None, None, [orphan_row], stages, required_kind=None)  # type: ignore[arg-type]

    assert stages == {}
    assert "no owning agent_id" in caplog.text


def _agents_selects(statements: list[str]) -> list[str]:
    """The subset of captured SQL that READS the ``agents`` table (the N+1 under measurement)."""
    return [s for s in statements if s.lstrip().lower().startswith("select") and "from agents" in s.lower()]


@pytest.mark.asyncio
async def test_owner_resolution_is_one_query_for_many_owners_not_one_per_owner(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THREE owners with TWO orphaned rows each resolve in exactly ONE ``agents`` SELECT for the whole run.

    This is the acceptance criterion measured directly rather than inferred: count the SELECTs against
    ``agents`` that ``recover_orphaned_work`` itself issues (the agents are seeded before the listener
    attaches, and ``count_inflight_jobs`` / ``get_live_job_keys`` are stubbed, so every captured
    ``agents`` read belongs to owner resolution).
    MUTATION: reverting to a ``select_agent_by_id`` call per distinct owner issues THREE -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    owner_ids = ["fs-a", "fs-b", "fs-c"]
    for owner_id in owner_ids:
        await seed_active_agent(session, agent_id=owner_id, kind="fileserver")

    files: dict[str, list[FileRecord]] = {}
    for owner_id in owner_ids:
        owned = [_make_file(), _make_file()]
        session.add_all(owned)
        await session.commit()
        files[owner_id] = owned
        for f in owned:
            await _seed_ledger(
                session, function="process_file", file_id=f.id, payload=_agent_payload_owned_by("process_file", f.id, agent_id=owner_id)
            )

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        captured.append(statement)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    event.listen(async_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", _capture)

    # ONE batched lookup for all three owners -- not one per owner, and not one per row.
    assert len(_agents_selects(captured)) == 1, f"expected a single batched agents lookup, saw: {_agents_selects(captured)}"
    # ...and every row still landed on ITS OWN owner's lane, six replays across three lanes.
    assert result["stages"]["process_file"]["reenqueued"] == 6
    for owner_id in owner_ids:
        lane = f"{owner_id}-analyze"
        assert [payload["file_id"] for _, payload in router.queues[lane].captured] == [str(f.id) for f in files[owner_id]]


def test_plan_owner_groups_preserves_first_encounter_order() -> None:
    """Groups appear in FIRST-ENCOUNTER order and each group's rows keep their own encounter order.

    Both orderings are what makes a replay reproducible run to run, and both are easy to lose to an
    innocent-looking ``sorted()``. The input interleaves owners (b, a, b, c, a) so a sort by owner id
    and a sort by row key are each distinguishable from the contract.
    MUTATION: sorting the owner ids yields ``["a", "b", "c"]`` -> RED; sorting each group's rows yields
    ``["b1", "b2"]`` for the b group instead of the encounter order asserted below -> RED.
    """
    rows = [
        SchedulingLedger(key="process_file:b2", function="process_file", routing="agent", payload={"agent_id": "b"}),
        SchedulingLedger(key="process_file:a1", function="process_file", routing="agent", payload={"agent_id": "a"}),
        SchedulingLedger(key="process_file:b1", function="process_file", routing="agent", payload={"agent_id": "b"}),
        SchedulingLedger(key="process_file:orphan", function="process_file", routing="agent", payload={"file_id": "x"}),
        SchedulingLedger(key="process_file:c1", function="process_file", routing="agent", payload={"agent_id": "c"}),
        SchedulingLedger(key="process_file:a2", function="process_file", routing="agent", payload={"agent_id": "a"}),
    ]

    plan = _plan_owner_groups(rows)

    assert [group.owner_id for group in plan.groups] == ["b", "a", "c"]
    assert [[r.key for r in group.rows] for group in plan.groups] == [
        ["process_file:b2", "process_file:b1"],
        ["process_file:a1", "process_file:a2"],
        ["process_file:c1"],
    ]
    # The owner-less row is partitioned out entirely -- never a ``None`` group that could be routed.
    assert [r.key for r in plan.ownerless] == ["process_file:orphan"]


@pytest.mark.asyncio
async def test_batched_replay_visits_owners_and_rows_in_encounter_order(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batched lookup does not reorder the replay: enqueues follow the rows' encounter order exactly.

    Driven through :func:`_replay_agent_rows_by_owner` with an explicitly-ordered row list, because
    ``get_ledger_rows`` issues no ``ORDER BY`` and therefore cannot pin an order end-to-end. The rows
    interleave two owners so "grouped by owner, groups in first-encounter order" is distinguishable
    from both "row order" and "sorted owner order".
    MUTATION: resolving owners from a set (or sorting the batch result) reorders the shared capture
    list to fs-a-first -> RED.
    """
    _patch_settings(monkeypatch)
    await seed_active_agent(session, agent_id="fs-a", kind="fileserver")
    await seed_active_agent(session, agent_id="fs-b", kind="fileserver")

    def _row(owner: str, tag: str) -> SchedulingLedger:
        return SchedulingLedger(
            key=f"process_file:{tag}",
            function="process_file",
            routing="agent",
            payload={"file_id": tag, "agent_id": owner},
        )

    rows = [_row("fs-b", "b1"), _row("fs-a", "a1"), _row("fs-b", "b2"), _row("fs-a", "a2")]
    router = DedupFakeTaskRouter()
    stages: dict[str, dict[str, int]] = {}

    await _replay_agent_rows_by_owner(session, router, rows, stages, required_kind=None)

    # fs-b was encountered first, so its whole group replays first -- each group in row order.
    assert [(queue_name, payload["file_id"]) for queue_name, _, payload in router.captures] == [
        ("phaze-agent-fs-b-analyze", "b1"),
        ("phaze-agent-fs-b-analyze", "b2"),
        ("phaze-agent-fs-a-analyze", "a1"),
        ("phaze-agent-fs-a-analyze", "a2"),
    ]
    assert stages["process_file"]["reenqueued"] == 4


@pytest.mark.asyncio
async def test_revoked_owner_is_skipped_and_never_reassigned(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A REVOKED owner's rows are skipped with a WARNING -- never rerouted onto the live fileserver.

    ``revoked_at IS NULL`` is half of the liveness filter the batch lookup shares with
    ``select_agent_by_id``; a revoked agent is registered and recently-seen, so it is precisely the case
    an id-only batch lookup would wrongly resolve.
    MUTATION: dropping ``revoked_at IS NULL`` from ``select_agents_by_ids`` routes fs-b's row onto
    ``fs-b-analyze`` (a revoked agent's consumer-less lane) instead of skipping -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="fs-a", kind="fileserver")
    revoked = await seed_active_agent(session, agent_id="fs-b", kind="fileserver")
    revoked.revoked_at = datetime.now(UTC)
    session.add(revoked)
    await session.commit()

    file_a = _make_file()
    file_b = _make_file()
    session.add_all([file_a, file_b])
    await session.commit()
    await _seed_ledger(
        session, function="process_file", file_id=file_a.id, payload=_agent_payload_owned_by("process_file", file_a.id, agent_id="fs-a")
    )
    await _seed_ledger(
        session, function="process_file", file_id=file_b.id, payload=_agent_payload_owned_by("process_file", file_b.id, agent_id="fs-b")
    )

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert "fs-b-analyze" not in router.queues  # the revoked owner's lane is never created
    assert [payload["file_id"] for _, payload in router.queues["fs-a-analyze"].captured] == [str(file_a.id)]
    assert result["stages"]["process_file"]["reenqueued"] == 1
    assert "owning agent offline" in caplog.text


@pytest.mark.asyncio
async def test_push_file_row_owned_by_a_live_compute_agent_is_skipped_not_reassigned(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``push_file`` row whose stored owner is a live COMPUTE agent is skipped -- the WRONG-KIND refusal.

    Phase 50 / D-10: a re-driven push reads the media mount, so it MUST land on the rsync-initiating
    FILESERVER. The owner here is online and healthy, so ONLY the ``kind`` scope on the batched lookup
    stands between the row and a compute lane that would never serve it. A second push row owned by the
    live fileserver proves the partition still recovers what it should.
    MUTATION: dropping ``kind`` from ``select_agents_by_ids`` (or passing ``required_kind=None`` for the
    push partition) enqueues onto ``cloud-io`` -> RED.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="fs-a", kind="fileserver")
    await seed_active_agent(session, agent_id="cloud", kind="compute")  # live, but the WRONG kind for push_file

    file_fs = _make_file()
    file_compute = _make_file()
    session.add_all([file_fs, file_compute])
    await session.commit()
    await _seed_ledger(session, function="push_file", file_id=file_fs.id, payload=_agent_payload_owned_by("push_file", file_fs.id, agent_id="fs-a"))
    await _seed_ledger(
        session, function="push_file", file_id=file_compute.id, payload=_agent_payload_owned_by("push_file", file_compute.id, agent_id="cloud")
    )

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert "cloud-io" not in router.queues  # the compute owner never receives a push_file re-drive
    assert [payload["file_id"] for _, payload in router.queues["fs-a-io"].captured] == [str(file_fs.id)]
    assert result["stages"]["push_file"]["reenqueued"] == 1
