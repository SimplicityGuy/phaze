"""The internal ``Backend`` protocol + its three re-homed implementations (Phase 68, BACK-01/03).

This is the phase's center of gravity. It houses one ``typing.Protocol`` (design §4.2 shape --
``is_available`` / ``in_flight_count`` / ``dispatch`` / ``reconcile``) and the three implementations
``LocalBackend`` / ``ComputeAgentBackend`` / ``KueueBackend`` that **re-home** the existing staging /
push / submit / reconcile bodies verbatim -- this is a behavior-preserving refactor, NOT a rewrite.

Phase 68 is a **lay-and-prove** phase (D-02): it defines the protocol + the uniform per-backend
``in_flight_count`` substrate and proves the equivalence invariant ``sum(in_flight_count(b)) ==
get_cloud_window_count()`` for the single-backend case, but it does NOT flip the drain onto per-backend
caps (that is Phase 69 / SCHED-02). This module is **purely additive** this phase -- the live drain
(``release_awaiting_cloud.stage_cloud_window``), the reconcile cron, and the config accessors are NOT
rewired here (Wave 3 / plan 68-04 owns that). The protocol methods are per-backend and unit-tested as
such.

Decisions realized here:

* **D-01a** -- the GATE-1 asymmetry lives in per-kind ``is_available``: compute REQUIRES a live compute
  agent; Kueue deliberately probes the cluster with NO compute-agent dependency; local is always up.
* **D-02** -- ``in_flight_count`` is the uniform ``cloud_job``-derived per-backend count; the equivalence
  invariant is the characterization proof that the new substrate matches the old count.
* **D-03** -- ``dispatch`` owns BOTH the ``FileState -> PUSHING`` flip AND the ``cloud_job`` upsert in the
  SAME caller-passed session, before/with the flip, NEVER after a separate commit (Pitfall 4 limbo guard).
* **D-05** -- ``KueueBackend`` calls today's single-cluster ``_stage_file_to_s3`` / ``kube_staging``
  VERBATIM (reads ``active_kube`` / ``active_bucket``); per-cluster parameterization is Phase 70.
* **D-07** -- the raise-on-``>1``-non-local guard is relocated into :func:`resolve_backends` (fail fast at
  boot); ``cloud_enabled`` stays in config as the registry on/off gate.
* **D-10** -- the in-flight status set is ``{UPLOADING, UPLOADED, SUBMITTED, RUNNING}``.

Cron no-op discipline (T-68-05): ``is_available`` / ``dispatch`` / ``reconcile`` degrade to a clean hold
(return ``False`` / a no-op) on an absent agent or a probe failure -- they never raise out to a cron.
Secret hygiene (T-68-04): this module logs only ``{id, kind, rank, cap}``-level fields, never a
``SecretStr`` / ``*_file`` value or a kube SA token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileState
from phaze.services import kube_staging
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.cloud_staging import _stage_file_to_s3
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.tasks.reconcile_cloud_jobs import _reconcile_one
from phaze.tasks.release_awaiting_cloud import _enqueue_push_file


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.config_backends import BackendConfig
    from phaze.models.file import FileRecord
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)


# D-10 (Q3): the exact non-terminal / in-flight CloudJobStatus set in_flight_count counts. Terminal =
# {SUCCEEDED, FAILED}. Pinned against the live CloudJobStatus members in models/cloud_job.py.
IN_FLIGHT: tuple[CloudJobStatus, ...] = (
    CloudJobStatus.UPLOADING,
    CloudJobStatus.UPLOADED,
    CloudJobStatus.SUBMITTED,
    CloudJobStatus.RUNNING,
)


class Backend(Protocol):
    """The single internal dispatch seam that removes the ``if active_cloud_kind == …`` fork (§4.2).

    Structural (``typing.Protocol``): the three impls below conform by shape, no explicit subclassing.
    ``id`` / ``rank`` / ``cap`` mirror the Phase-67 registry submodel fields (cost-tier rank, concurrency
    cap); the four async methods are the per-backend dispatch lifecycle.
    """

    id: str
    rank: int
    cap: int

    async def is_available(self, session: AsyncSession) -> bool:
        """Whether this backend can accept a dispatch right now (compute: agent gate; kueue: cluster probe)."""
        ...

    async def in_flight_count(self, session: AsyncSession) -> int:
        """COUNT(cloud_job WHERE backend_id == self.id AND status IN {in-flight}) -- the D-02 substrate."""
        ...

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> None:
        """Flip ``file`` into the cloud window + write its ``cloud_job`` row, IN the caller's txn (D-03). Never commits."""
        ...

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> None:
        """Advance this backend's in-flight ``cloud_job`` rows toward terminal (kueue: cron read; local/compute: no-op)."""
        ...


class _BaseBackend:
    """Shared ``id`` / ``rank`` / ``cap`` carrier + the uniform ``cloud_job``-derived ``in_flight_count``.

    Each concrete backend binds to a single Phase-67 registry entry (``config``). The shared
    ``in_flight_count`` is the D-02/D-10 substrate: a pure DB COUNT filtered by ``backend_id`` + the
    in-flight status set (modelled on ``pipeline.get_cloud_window_count``).
    """

    def __init__(self, *, id: str, rank: int, cap: int, config: BackendConfig | None = None) -> None:
        self.id = id
        self.rank = rank
        self.cap = cap
        self.config = config

    async def in_flight_count(self, session: AsyncSession) -> int:
        """Return COUNT(cloud_job WHERE backend_id == self.id AND status IN {UPLOADING,UPLOADED,SUBMITTED,RUNNING})."""
        return int(
            (
                await session.execute(
                    select(func.count(CloudJob.id)).where(
                        CloudJob.backend_id == self.id,
                        CloudJob.status.in_([status.value for status in IN_FLIGHT]),
                    )
                )
            ).scalar()
            or 0
        )


class LocalBackend(_BaseBackend):
    """On-prem/all-local backend -- analysis runs on the fileserver agent via ``process_file`` (no cloud_job).

    ``is_available`` is unconditionally True (local dispatch needs no remote cloud agent);
    ``in_flight_count`` is always 0 (a local burst writes NO ``cloud_job`` row); ``reconcile`` is a no-op
    (local completion is synchronous, no cron read). ``dispatch`` re-homes the ``process_file`` local
    enqueue path (Phase-69 scheduler uses it; unit-tested here, NOT wired into the single-path drain).
    """

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature; local needs no session probe
        """Always True -- local dispatch never depends on a remote cloud agent."""
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- protocol signature; local holds no cloud_job rows
        """Always 0 -- a local burst writes no ``cloud_job`` row, so it holds no in-flight cloud slot."""
        return 0

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> None:
        """Enqueue ``process_file`` on the fileserver agent's queue; a clean hold when no agent is online.

        Re-homes the local ``enqueue_process_file`` producer (``analysis_enqueue``). Writes NO
        ``cloud_job`` row. An absent agent degrades to a clean hold (NoActiveAgentError -> no-op),
        matching the cron no-op discipline -- never a raise.
        """
        cfg = cast("ControlSettings", get_settings())
        try:
            agent = await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            logger.info("LocalBackend.dispatch hold: no fileserver agent online", file_id=str(file.id))
            return
        queue = task_router.queue_for(agent.id)
        await enqueue_process_file(queue, file, agent.id, cfg.models_path)

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> None:  # noqa: ARG002 -- protocol signature; local has no cron read
        """No-op: local analysis completion is synchronous -- there is no cron read to run."""
        return None


class ComputeAgentBackend(_BaseBackend):
    """Cloud-compute (rsync/push over Tailscale) backend -- re-homes the ``push_file`` control-side enqueue leg.

    ``is_available`` re-homes GATE-1 (``release_awaiting_cloud`` L145-150): True iff a compute agent is
    online. ``dispatch`` owns the ``FileState -> PUSHING`` flip AND a NEW in-txn ``cloud_job`` write
    (Pitfall 1 / D-03) then re-homes the ``_enqueue_push_file`` leg. ``reconcile`` is a no-op --
    compute terminalization is the existing ``/pushed`` callback path (§4.2, D-08).
    ``in_flight_count`` is inherited from :class:`_BaseBackend` (the D-02 substrate).
    """

    async def is_available(self, session: AsyncSession) -> bool:
        """GATE-1 (D-01a): True iff a compute agent is online; False (never raises) when absent.

        Re-homes ``stage_cloud_window``'s compute-agent gate. An absent agent degrades to a hold
        (NoActiveAgentError -> False), preserving the cron no-op discipline (T-68-05).
        """
        try:
            await select_active_agent(session, kind="compute")
        except NoActiveAgentError:
            return False
        return True

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> None:
        """Flip ``file`` to PUSHING + upsert its ``cloud_job`` row, THEN enqueue ``push_file`` -- one txn, no commit.

        D-03 write ordering: the ``FileState -> PUSHING`` flip and the ``cloud_job`` upsert
        (``backend_id`` set, ``s3_key`` NULL -- compute carries no S3 object, ``status=SUBMITTED``) land
        in the SAME caller-passed session, before the enqueue, so a rollback leaves no limbo row (a
        committed PUSHING without a reconcilable ``cloud_job`` row would silently strand the file). The
        fileserver gate runs first so an absent agent is a clean hold with nothing mutated. NEVER commits
        -- the drain owns the single post-loop commit so the ``pg_advisory_xact_lock`` survives the tick
        (Landmine L1).
        """
        # Gate on the fileserver agent (the push initiator) BEFORE mutating: absent -> clean hold, nothing written.
        fileserver_agent = await select_active_agent(session, kind="fileserver")

        # D-03: flip PUSHING + upsert the cloud_job row in the SAME session, before/with the flip.
        file.state = FileState.PUSHING
        stmt = pg_insert(CloudJob).values(
            # Stamp the PK explicitly (CR-01 defensive; mirrors cloud_staging.py:109).
            id=uuid.uuid4(),
            file_id=file.id,
            backend_id=self.id,
            s3_key=None,  # compute has no S3 object -> s3_key nullable (D-08)
            status=CloudJobStatus.SUBMITTED.value,  # single compute in-flight status (D-10)
        )
        stmt = stmt.on_conflict_do_update(
            # id is OUT of set_: the PK is immutable, so a re-dispatch keeps the existing row's id.
            index_elements=["file_id"],
            set_={"backend_id": stmt.excluded.backend_id, "status": stmt.excluded.status},
        )
        await session.execute(stmt)

        # Re-home the compute enqueue leg (release_awaiting_cloud._enqueue_push_file), verbatim.
        push_queue = task_router.queue_for(fileserver_agent.id)
        await _enqueue_push_file(push_queue, file, fileserver_agent.id)

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> None:  # noqa: ARG002 -- protocol signature; compute terminalizes via the /pushed callback
        """No-op: compute terminalization is the existing ``/pushed`` callback path (§4.2, D-08), not a cron read."""
        return None


class KueueBackend(_BaseBackend):
    """Kueue-cluster backend -- re-homes today's single-cluster S3-staging + kube submit/reconcile (D-05).

    ``is_available`` probes the Kueue LocalQueue with NO compute-agent dependency (D-01a); ``dispatch``
    calls the no-commit S3-staging core VERBATIM (single-cluster, reads ``active_kube`` / ``active_bucket``);
    ``reconcile`` re-homes the ``reconcile_cloud_jobs`` cron body, made ``backend_id``-aware, with NO
    advisory lock (Pitfall 2 -- deferred to Phase 69). ``in_flight_count`` is inherited from
    :class:`_BaseBackend` (the D-02 substrate).
    """

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature; kueue probes the cluster, not a DB agent
        """Probe the Kueue LocalQueue -- True iff reachable; False (never raises) on any probe failure (D-01a).

        Re-homes the ``kube_staging.get_local_queue`` reachability probe. Deliberately has NO
        compute-agent dependency (D-01a asymmetry): ephemeral Kueue pods have no persistent compute
        agent. A ``NotFoundError`` (mis-named queue) or transient ``ServerError`` degrades to False rather
        than raising (mirrors the controller's non-fatal catch), preserving the cron no-op discipline.
        """
        try:
            local_queue = await kube_staging.get_local_queue()
        except Exception:  # any kube/mesh failure degrades to "unavailable" (T-68-05 no-op discipline)
            logger.info("KueueBackend.is_available: LocalQueue probe failed -> unavailable", backend_id=self.id)
            return False
        return local_queue is not None

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> None:
        """Flip ``file`` to PUSHING then run the no-commit S3-staging core VERBATIM (single-cluster, D-05).

        The S3 core (``cloud_staging._stage_file_to_s3``) already upserts the ``cloud_job`` row
        (``UPLOADING``) + enqueues one ``s3_upload`` in the caller's session -- exactly as today's kueue
        drain branch does -- so this writes NO second ``cloud_job`` row. D-05 keeps it single-cluster
        (reads ``active_kube`` / ``active_bucket``); per-cluster parameterization is Phase 70. NEVER
        commits (the drain owns the single post-loop commit -- Landmine L1).
        """
        # D-03: the drain flips PUSHING before the per-kind fork; own that flip here so dispatch is atomic.
        file.state = FileState.PUSHING
        await _stage_file_to_s3(session, file, task_router)

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> None:
        """Reconcile THIS backend's in-flight ``cloud_job`` rows against their Kueue Job/Workload (backend_id-aware).

        Re-homes ``reconcile_cloud_jobs`` (L282-322): iterate ``cloud_job`` rows in {SUBMITTED, RUNNING}
        scoped to ``backend_id == self.id``, delegate each to the shared ``_reconcile_one`` under the
        per-row ``session.rollback()`` guard so one bad row never aborts the tick. NO advisory lock this
        phase (Pitfall 2 -- the lock change lands with the Phase-69 cap flip). ``ctx`` (carrying the
        re-drive ``queue``) is threaded to ``_reconcile_one``; it defaults to ``{}`` for the lay-and-prove
        unit path where no row reaches a re-drive.
        """
        cfg = cast("ControlSettings", get_settings())
        cap = cfg.cloud_submit_max_attempts
        tally = {"reconciled": 0, "succeeded": 0, "failed": 0, "redriven": 0, "inadmissible": 0, "pending": 0, "running": 0}
        reconcile_ctx = ctx if ctx is not None else {}

        rows = (
            (
                await session.execute(
                    select(CloudJob).where(
                        CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
                        CloudJob.backend_id == self.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        # Capture primitive ids: the per-row guard's rollback expires the ORM identity map, so re-fetch
        # each row fresh inside the loop rather than touching a stale/expired object (verbatim from the cron).
        cloud_job_ids = [row.id for row in rows]

        for cloud_job_id in cloud_job_ids:
            try:
                cloud_job = await session.get(CloudJob, cloud_job_id)
                if cloud_job is None:
                    continue
                tally["reconciled"] += 1
                await _reconcile_one(reconcile_ctx, session, cloud_job, cap, tally)
            except Exception:
                # Per-row guard: a single bad row never aborts the tick; roll back the partial mutation.
                await session.rollback()
                logger.warning("KueueBackend.reconcile: row reconcile failed; continuing", cloud_job_id=str(cloud_job_id), exc_info=True)
        return None


def resolve_backends(settings: ControlSettings) -> list[Backend]:
    """Build one :class:`Backend` impl per registry entry; raise fast at boot if >1 non-local resolves.

    D-07 boot guard (relocated from ``config._single_non_local``): Phase 68 stays single-dispatch-path,
    so the registry must reduce to exactly one non-local backend (+ any locals). A ``>1``-non-local
    registry is a Phase-69 (SCHED) capability -- fail fast here naming the offending ids rather than
    silently picking one. Each impl binds to its Phase-67 discriminated-union submodel (``config``).
    """
    resolved: list[Backend] = []
    for entry in settings.backends:
        if entry.kind == "local":
            resolved.append(LocalBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))
        elif entry.kind == "compute":
            resolved.append(ComputeAgentBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))
        elif entry.kind == "kueue":
            resolved.append(KueueBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))

    non_local = [backend for backend in resolved if not isinstance(backend, LocalBackend)]
    if len(non_local) > 1:
        raise ValueError(
            f"multi-backend dispatch lands in Phase 69 (SCHED): {len(non_local)} non-local backends "
            f"{[backend.id for backend in non_local]} are configured, but Phase 68 resolves only a ≤1-non-local registry"
        )
    return resolved


def resolved_non_local_kind(settings: ControlSettings) -> str:
    """Return the registry-derived active kind: ``"local"`` when all-local, else the single non-local kind.

    The Wave-3 replacement for the deleted ``active_cloud_kind`` accessor (D-07/D-09): ``"local"`` when
    ``cloud_enabled`` is False, otherwise the sole non-local backend's kind (``"compute"`` | ``"kueue"``).
    The single-non-local invariant is enforced by :func:`resolve_backends`'s boot guard.
    """
    if not settings.cloud_enabled:
        return "local"
    non_local = [backend for backend in settings.backends if backend.kind != "local"]
    return non_local[0].kind
