"""Shared prelude for the split `tests/analyze/services/backends/protocol/test_*.py` suite (phaze-7l8jh).

`tests/analyze/services/backends/test_backends.py` (2,361 lines, 80 tests) is the Layer 3
per-backend `Backend` protocol suite (`is_available` / `in_flight_count` / `dispatch` /
`reconcile` for Local / Compute / Kueue) plus the Layer 2 D-02 cross-backend equivalence
invariant, the D-03 admission (`hold_awaiting_cloud`) tests, and the registry resolution tests.
Its production counterpart, `src/phaze/services/backends.py`, was already split into the
`services/backends/` package (phaze-dr9df: admission.py, base.py, compute_agent.py, kueue.py,
lane_detail.py, lane_metrics.py, lane_snapshot.py, local.py, registry.py) -- this file never
followed (the sibling files already in this directory, e.g. `test_registry_duplicate_ids.py` /
`test_single_awaiting_writer.py`, are the parts of that split that DID happen at the time).

This package finishes it for the remaining monolith, one test file per production submodule this
suite exercises (`lane_detail.py`, `lane_metrics.py` and `lane_snapshot.py` are covered by other,
already-appropriately-named files in this directory -- `test_cloud_staging.py`,
`test_queue_introspection.py`, etc. -- not by this file, so they get no new file here). The seven
`test_select_agent_by_id_*` cells exercise `phaze.services.enqueue_router.select_agent_by_id`
directly (NOT a `services/backends` symbol) but land in `test_compute_agent.py` because it is the
exact helper `ComputeAgentBackend.is_available` depends on -- see that file's own docstring note.

This module holds everything that ISN'T a `def test_*`: the shared imports, the `pytest.importorskip`
Wave-2 guard, and every stub/`_make_*`/`_seed_*` helper the split files import from. Nothing here was
rewritten -- every symbol is a verbatim carry-over from the original file, so no test's behavior
changed as a side effect of the split.
"""

# Carried over from the original test_backends.py module docstring (the "GUARDED SCAFFOLD" framing
# is now historical -- Wave 2 landed long ago and `services.backends` has existed since phaze-dr9df;
# `pytest.importorskip` below is a no-op today, kept only because removing it changes nothing and
# reverifying that is exactly the kind of behavior change this split promises not to make):
#
# The cells are authored correct-by-construction against design §4.2 and the 68-PATTERNS re-home map:
#
# * ``is_available`` -- Local: always True; Compute: True only when a compute agent is online via
#   ``select_active_agent(kind="compute")`` (GATE-1), False (never raises) when absent; Kueue: a kube /
#   LocalQueue probe with NO compute-agent dependency (D-01a), returns bool, never raises.
# * ``in_flight_count`` -- ``COUNT(cloud_job WHERE backend_id == self.id AND status IN
#   {UPLOADING, UPLOADED, SUBMITTED, RUNNING})`` (D-10); Local is always 0 (no cloud_job rows).
# * ``dispatch`` D-03 atomicity -- the ``cloud_job`` upsert lands in the caller-passed session, so there
#   is never a committed in-flight marker without a live non-terminal ``cloud_job`` row (no limbo row).
# * ``reconcile`` -- Kueue cron read; Local/Compute callback-driven (no-op in the unit cells).
#
# Layer 2 (D-02): ``sum(in_flight_count(b) for b in backends)`` equals the derived in-flight window
# count for the single-backend case, over constructed ``cloud_job`` states (Phase 69 / D-05 retired
# the global ``get_cloud_window_count`` helper; the window is counted inline).

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services import kube_staging, s3_staging
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent
from tests.kube_fakes import fake_local_queue


# Wave 2 target -- skip the whole module until it exists (collects clean in Wave 0).
backends = pytest.importorskip("phaze.services.backends")

# phaze-dr9df: ``services.backends`` is a PACKAGE. ``backends`` above is its re-export FACADE, which is
# still the right handle for reading a symbol (``backends.KueueBackend``, ``backends.resolve_backends``)
# but is the WRONG handle for monkeypatching one: a name a submodule imported is resolved out of THAT
# submodule's globals, so rebinding the facade attribute is a silent no-op. The two aliases below are the
# patch targets for the reapers' ``hold_awaiting_cloud`` seam -- both are re-exported by ``__init__``, so
# they exist for free under the ``importorskip`` above.
backends_kueue = backends.kueue
backends_compute_agent = backends.compute_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# D-10 (Q3): the exact non-terminal in-flight status set in_flight_count counts. Terminal =
# {SUCCEEDED, FAILED}. Pinned here so a Wave-2 drift from this set fails these cells loudly.
IN_FLIGHT_STATUSES = (
    CloudJobStatus.UPLOADING,
    CloudJobStatus.UPLOADED,
    CloudJobStatus.SUBMITTED,
    CloudJobStatus.RUNNING,
)
TERMINAL_STATUSES = (CloudJobStatus.SUCCEEDED, CloudJobStatus.FAILED)


# --- backend factories (Wave 2 finalizes the exact constructor signatures) ---------------


def _local(**kw: Any) -> Any:
    """Construct a LocalBackend (id/rank/cap; is_available always True, in_flight_count 0)."""
    return backends.LocalBackend(id=kw.get("id", "local"), rank=kw.get("rank", 0), cap=kw.get("cap", 0))


def _compute(**kw: Any) -> Any:
    """Construct a ComputeAgentBackend bound to a single registry entry.

    Phase 72 (MCOMP-01/D-02): ``is_available`` resolves ``self.config.agent_ref`` against ``Agent.id``,
    so the backend must carry a real ``ComputeBackend`` config. ``agent_ref`` defaults to the backend id
    (the byte-identical single-compute deploy binds agent_ref == the online agent's id); pass
    ``agent_ref=`` to bind a specific / mismatched node, or ``config=None`` to exercise the unbound
    fail-loud accessor path.
    """
    from phaze.config_backends import ComputeBackend as ComputeEntry

    bid = kw.get("id", "compute-a1")
    rank = kw.get("rank", 10)
    cap = kw.get("cap", 2)
    if "config" in kw:
        config = kw["config"]
    else:
        config = ComputeEntry(
            kind="compute",
            id=bid,
            rank=rank,
            cap=cap,
            agent_ref=kw.get("agent_ref", bid),
            scratch_dir=kw.get("scratch_dir", "/srv/scratch"),
            push_host=kw.get("push_host", f"{bid}.push.example"),
            ssh_user=kw.get("ssh_user"),
        )
    return backends.ComputeAgentBackend(id=bid, rank=rank, cap=cap, config=config)


def _kueue(**kw: Any) -> Any:
    """Construct a KueueBackend bound to a registry entry carrying a ``[kube]`` config (MKUE-01/D-04).

    Phase 70: ``is_available`` / ``reconcile`` thread ``self.config.kube`` into every ``kube_staging``
    verb, so the backend must carry a ``config`` with a ``KubeConfig``. The ``kube_staging`` seam is
    monkeypatched in these unit cells, so a minimal KubeConfig (api_url/namespace/local_queue) suffices.
    """
    from phaze.config_backends import KubeConfig, KueueBackend as KueueEntry

    bid = kw.get("id", "kueue-x64")
    rank = kw.get("rank", 20)
    cap = kw.get("cap", 5)
    entry = KueueEntry(
        kind="kueue",
        id=bid,
        rank=rank,
        cap=cap,
        kube=KubeConfig(api_url="https://kube.example.com", namespace="phaze", local_queue="phaze-lq"),
        buckets=list(kw.get("buckets", [])),
    )
    return backends.KueueBackend(id=bid, rank=rank, cap=cap, config=entry)


def _kueue_with_buckets(backends_toml_env: Any, *, bucket_ids: list[str], backend_id: str = "kueue-x64") -> Any:
    """Build a KueueBackend bound to ``bucket_ids`` via a real registry (MKUE-02 dispatch picks a bucket).

    ``KueueBackend.dispatch`` now resolves the D-06 bucket via ``pick_bucket`` over ``self.config.buckets``
    and ``s3_staging.resolve_bucket_config`` over ``get_settings().buckets`` -- so the backend must carry a
    real ``config`` (its bound bucket id-list) AND the process registry must resolve those ids. This helper
    writes a one-kueue backends.toml (via the shared conftest fixture, which points the env + clears the
    get_settings cache) and returns the resolved ``KueueBackend`` whose ``self.config`` is that entry.
    """
    from phaze.config import ControlSettings

    id_array = ", ".join(f'"{bid}"' for bid in bucket_ids)
    bucket_blocks = "".join(
        f"""
        [[buckets]]
        id = "{bid}"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-{bid}"
        """
        for bid in bucket_ids
    )
    backends_toml_env(
        f"""
        [[backends]]
        kind = "kueue"
        id = "{backend_id}"
        rank = 20
        cap = 5
        buckets = [{id_array}]

        [backends.kube]
        api_url = "https://kube.example.com"
        namespace = "phaze"
        local_queue = "phaze-lq"
        {bucket_blocks}
        """
    )
    settings = ControlSettings()
    [backend] = [b for b in backends.resolve_backends(settings) if b.id == backend_id]
    return backend


def _make_file(*, file_type: str = "mp3") -> FileRecord:
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


async def _seed_cloud_job(session: AsyncSession, *, backend_id: str | None, status: CloudJobStatus) -> uuid.UUID:
    """Insert one cloud_job row (with its FK file) at ``status``; return the file id."""
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            backend_id=backend_id,
            s3_key=None if backend_id and "kueue" not in backend_id else f"staging/{file.id}",
            status=status.value,
        )
    )
    await session.commit()
    return file.id


def _stub_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3_staging, "create_multipart_upload", AsyncMock(return_value="upload-xyz"))
    monkeypatch.setattr(s3_staging, "presign_upload_parts", AsyncMock(return_value=["https://s3.test/part?1"]))


def _stub_kube_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kube_staging, "get_local_queue", AsyncMock(return_value=fake_local_queue()))


class _RaisingQueue(DedupFakeQueue):
    """A queue whose ``enqueue`` always raises -- models SAQ's ``PostgresQueue`` (its OWN psycopg
    pool, phaze-uciu.3) blowing up AFTER ``dispatch``/``_stage_file_to_s3`` has already upserted the
    ``cloud_job`` row in THIS test's asyncpg session. ``connect()`` (inherited) still succeeds, so the
    raise fires exactly where the real enqueue failure fires.
    """

    async def enqueue(self, task_name: str, **kwargs: Any) -> Any:  # noqa: ARG002 -- fake signature parity
        raise RuntimeError("saq enqueue blew up")


class _RaisingTaskRouter:
    """A task router whose every ``queue_for`` hands back a :class:`_RaisingQueue`."""

    def __init__(self) -> None:
        self.queue_for_calls: list[str] = []

    def queue_for(self, agent_id: str, lane: str | None = None) -> _RaisingQueue:  # noqa: ARG002 -- fake signature parity
        self.queue_for_calls.append(agent_id)
        return _RaisingQueue(f"raising-{agent_id}")


async def _seed_agent_row(
    session: AsyncSession,
    *,
    agent_id: str,
    name: str | None = None,
    kind: str = "compute",
    online: bool = True,
    revoked: bool = False,
) -> Agent:
    """Insert one Agent row with explicit id / name / liveness so binding-key edge cases are seedable.

    ``seed_active_agent`` always sets ``name == agent_id`` and always-online, so it cannot express the
    name-only-match / revoked / never-seen fixtures the D-01 selector must reject. This helper does.
    """
    now = datetime.now(UTC)
    agent = Agent(
        id=agent_id,
        name=name if name is not None else agent_id,
        token_hash=None,
        kind=kind,
        scan_roots=[],
        last_seen_at=now if online else None,
        revoked_at=(now - timedelta(hours=1)) if revoked else None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


_LOCAL_2KUEUE_HEAD = """
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
"""

_TWO_BUCKETS = """
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


# === phaze-ul2v: the stranded-staging reaper ({UPLOADING, UPLOADED} age bound) ============
#
# in_flight_count counts {UPLOADING, UPLOADED, SUBMITTED, RUNNING} (D-10), but reconcile only ever
# SELECTED {SUBMITTED, RUNNING}. The staging half is terminalized solely by the agent HTTP callbacks
# (report_uploaded / report_upload_failed), so a dead agent or a lost s3_upload SAQ job strands the row
# forever and permanently leaks a burst-lane cap slot. These cells pin the safety net: it fires past the
# age bound (both states), decrements in_flight_count, and NEVER fires on a young row.


async def _seed_staging_cloud_job(
    session: AsyncSession,
    *,
    backend_id: str,
    status: CloudJobStatus,
    age_sec: int,
    staging_bucket: str | None = None,
    upload_id: str | None = None,
) -> uuid.UUID:
    """Insert one staging cloud_job row aged ``age_sec`` into the past; return the file id.

    ``updated_at`` is DB-managed (``onupdate=func.now()``), so the age is forced with a raw UPDATE
    AFTER the insert -- the reaper reads exactly that column.
    """
    from sqlalchemy import text as sa_text

    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            backend_id=backend_id,
            s3_key=f"staging/{file.id}",
            status=status.value,
            upload_id=upload_id,
            staging_bucket=staging_bucket,
        )
    )
    await session.commit()
    await session.execute(
        sa_text("UPDATE cloud_job SET updated_at = now() - make_interval(secs => :age) WHERE file_id = :fid"),
        {"age": age_sec, "fid": file.id},
    )
    await session.commit()
    return file.id


async def _cloud_job_for(session: AsyncSession, file_id: uuid.UUID) -> Any:
    from sqlalchemy import select

    session.expire_all()
    return (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one()


# === phaze-31q3: the reaper consults broker liveness before firing ===================================


async def _seed_live_saq_job(session: AsyncSession, *, key: str, status: str = "active") -> None:
    """Insert a saq_jobs row so :func:`get_live_job_keys` sees ``key`` as queued/active.

    SAQ owns ``saq_jobs`` at runtime (Base.metadata does not), so create-if-absent first -- mirrors the
    aborting_reaper tests. The reaper's liveness probe only reads ``key`` + ``status``.
    """
    from sqlalchemy import text as sa_text

    await session.execute(
        sa_text(
            """
            CREATE TABLE IF NOT EXISTS saq_jobs (
                key TEXT PRIMARY KEY,
                lock_key SERIAL NOT NULL,
                job BYTEA NOT NULL,
                queue TEXT NOT NULL,
                status TEXT NOT NULL,
                priority SMALLINT NOT NULL DEFAULT 0,
                group_key TEXT,
                scheduled BIGINT NOT NULL DEFAULT 0,
                expire_at BIGINT
            )
            """
        )
    )
    await session.execute(
        sa_text("INSERT INTO saq_jobs (key, job, queue, status, scheduled) VALUES (:key, :job, :queue, :status, 0)"),
        {"key": key, "job": b"{}", "queue": "phaze-agent-nox-io", "status": status},
    )
    await session.commit()


# === phaze-j7m18: the compute stranded-SUBMITTED reaper ==================================
#
# Compute's ONLY in-flight status is SUBMITTED (D-08/D-10), terminalized SOLELY by the agent HTTP
# callbacks (/pushed, /mismatch, /failed). A dead fileserver agent host mid-rsync, or an enqueue
# failure in flush_pending_push_file_enqueues, leaves the row SUBMITTED forever with no callback ever
# coming, permanently leaking a compute cap slot -- the exact class of failure phaze-ul2v fixed for the
# kueue staging half. These cells mirror that reaper's test shape for compute's single status.


async def _seed_submitted_cloud_job(session: AsyncSession, *, backend_id: str, age_sec: int) -> uuid.UUID:
    """Insert one compute SUBMITTED cloud_job row aged ``age_sec`` into the past; return the file id."""
    from sqlalchemy import text as sa_text

    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            backend_id=backend_id,
            s3_key=None,
            status=CloudJobStatus.SUBMITTED.value,
        )
    )
    await session.commit()
    await session.execute(
        sa_text("UPDATE cloud_job SET updated_at = now() - make_interval(secs => :age) WHERE file_id = :fid"),
        {"age": age_sec, "fid": file.id},
    )
    await session.commit()
    return file.id


async def _ledger_row_exists(session: AsyncSession, key: str) -> bool:
    from sqlalchemy import select as sa_select

    from phaze.models.scheduling_ledger import SchedulingLedger

    session.expire_all()
    row = (await session.execute(sa_select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none()
    return row is not None


async def _seed_push_file_ledger(session: AsyncSession, *, file_id: uuid.UUID) -> None:
    from phaze.services.scheduling_ledger import upsert_ledger_entry

    await upsert_ledger_entry(session, key=f"push_file:{file_id}", function="push_file", kwargs={"file_id": str(file_id)})
    await session.commit()


__all__ = [
    "IN_FLIGHT_STATUSES",
    "TERMINAL_STATUSES",
    "TYPE_CHECKING",
    "UTC",
    "_LOCAL_2KUEUE_HEAD",
    "_TWO_BUCKETS",
    "Agent",
    "Any",
    "AsyncMock",
    "CloudJob",
    "CloudJobStatus",
    "DedupFakeQueue",
    "DedupFakeTaskRouter",
    "FileRecord",
    "_RaisingQueue",
    "_RaisingTaskRouter",
    "_cloud_job_for",
    "_compute",
    "_kueue",
    "_kueue_with_buckets",
    "_ledger_row_exists",
    "_local",
    "_make_file",
    "_seed_agent_row",
    "_seed_cloud_job",
    "_seed_live_saq_job",
    "_seed_push_file_ledger",
    "_seed_staging_cloud_job",
    "_seed_submitted_cloud_job",
    "_stub_kube_available",
    "_stub_s3",
    "backends",
    "backends_compute_agent",
    "backends_kueue",
    "datetime",
    "fake_local_queue",
    "kube_staging",
    "pytest",
    "s3_staging",
    "seed_active_agent",
    "timedelta",
    "uuid",
]
