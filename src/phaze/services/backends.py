"""The internal ``Backend`` protocol + its three re-homed implementations (Phase 68, BACK-01/03).

This is the phase's center of gravity. It houses one ``typing.Protocol`` (design §4.2 shape --
``is_available`` / ``in_flight_count`` / ``dispatch`` / ``reconcile``) and the three implementations
``LocalBackend`` / ``ComputeAgentBackend`` / ``KueueBackend`` that **re-home** the existing staging /
push / submit / reconcile bodies verbatim -- this is a behavior-preserving refactor, NOT a rewrite.

Phase 68 was a **lay-and-prove** phase (D-02): it defined the protocol + the uniform per-backend
``in_flight_count`` substrate and proved its equivalence to the (now Phase-69-retired) global
FileState ``{PUSHING, PUSHED}`` window for the single-backend case. Phase 69 (SCHED-02) then flipped
the drain (``release_awaiting_cloud.stage_cloud_window``) onto these per-backend caps: it snapshots
each backend's ``in_flight_count`` once per tick and enforces the per-backend ``cap``. The protocol
methods are per-backend and unit-tested as such.

Decisions realized here:

* **D-01a** -- the GATE-1 asymmetry lives in per-kind ``is_available``: compute REQUIRES a live compute
  agent; Kueue deliberately probes the cluster with NO compute-agent dependency; local is always up.
* **D-02** -- ``in_flight_count`` is the uniform ``cloud_job``-derived per-backend count; the equivalence
  invariant is the characterization proof that the new substrate matches the old count.
* **D-03** -- ``dispatch`` owns BOTH the ``FileState -> PUSHING`` flip AND the ``cloud_job`` upsert in the
  SAME caller-passed session, before/with the flip, NEVER after a separate commit (Pitfall 4 limbo guard).
* **D-05** -- ``KueueBackend`` calls today's single-cluster ``_stage_file_to_s3`` / ``kube_staging``
  VERBATIM (reads ``active_kube`` / ``active_bucket``); per-cluster parameterization is Phase 70.
* **D-07** -- the raise-on-``>1``-non-local guard is Phase-69-retired from :func:`resolve_backends` (N
  non-local backends now resolve; SCHED-01) and survives ONLY in :func:`resolved_non_local_kind` for the
  non-drain single-kind callers (WR-01); ``cloud_enabled`` stays in config as the registry on/off gate.
* **D-10** -- the in-flight status set is ``{UPLOADING, UPLOADED, SUBMITTED, RUNNING}``.

Cron no-op discipline (T-68-05): ``is_available`` / ``dispatch`` / ``reconcile`` degrade to a clean hold
(return ``False`` / a no-op) on an absent agent or a probe failure -- they never raise out to a cron.
Secret hygiene (T-68-04): this module logs only ``{id, kind, rank, cap}``-level fields, never a
``SecretStr`` / ``*_file`` value or a kube SA token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast
import uuid

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileState
from phaze.schemas.agent_tasks import PushFilePayload
from phaze.services import kube_staging
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.cloud_staging import _stage_file_to_s3
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.tasks.push import PUSH_FILE_SAQ_TIMEOUT_SEC
from phaze.tasks.reconcile_cloud_jobs import _reconcile_one
from phaze.tasks.release_awaiting_cloud import _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY, push_file_job_key


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


async def _enqueue_push_file(queue: Any, file: FileRecord, agent_id: str) -> Any:
    """Enqueue ONE ``push_file`` job with the deterministic key + the complete PushFilePayload.

    Relocated from ``release_awaiting_cloud`` in Wave 3 (68-04): this control-side enqueue leg is the
    ``ComputeAgentBackend.dispatch`` body, so it now lives with the backend that owns it (the drain no
    longer references it). Builds the four required ``PushFilePayload`` fields (the FileRecord's ``id``
    / ``original_path`` / ``file_type`` plus the resolved fileserver ``agent_id``) and serializes via
    ``model_dump(mode="json")`` so the UUID round-trips as a string under ``extra="forbid"``. Returns
    whatever ``queue.enqueue`` returns -- a ``saq.Job`` normally, or ``None`` when SAQ deduped the
    deterministic key (the file is already being pushed) so the caller counts a ``None`` as skipped.
    """
    payload = PushFilePayload(
        file_id=file.id,
        original_path=file.original_path,
        file_type=file.file_type,
        agent_id=agent_id,
    )
    # Phase 36: the PostgresQueue broker pool is built open=False; connect() is idempotent.
    await queue.connect()
    # WR-03: stamp an explicit SAQ job-net timeout strictly above the agent's asyncio outer guard so
    # a job-net cancellation can never fire before the guard reaps the rsync child.
    return await queue.enqueue(
        "push_file",
        key=push_file_job_key(file.id),
        timeout=PUSH_FILE_SAQ_TIMEOUT_SEC,
        **payload.model_dump(mode="json"),
    )


class Backend(Protocol):
    """The single internal dispatch seam that removes the ``if kind == …`` cloud-target fork (§4.2).

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

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
        """Flip ``file`` into the cloud window + write its ``cloud_job`` row, IN the caller's txn (D-03). Never commits.

        Returns ``True`` when new dispatch work was actually enqueued (a genuine stage) and ``False``
        when the enqueue was a deterministic-key dedup no-op / a clean hold -- the drain counts the
        former as ``staged`` and the latter as ``skipped`` (preserves the Phase-50 tally semantics).
        """
        ...

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:
        """Advance this backend's in-flight ``cloud_job`` rows toward terminal (kueue: cron read; local/compute: no-op).

        Returns a per-backend outcome ``tally`` dict for the cron to aggregate (kueue), or ``None`` for
        the callback-driven no-op backends (local/compute) that own no cron read.
        """
        ...


class _BaseBackend:
    """Shared ``id`` / ``rank`` / ``cap`` carrier + the uniform ``cloud_job``-derived ``in_flight_count``.

    Each concrete backend binds to a single Phase-67 registry entry (``config``). The shared
    ``in_flight_count`` is the D-02/D-10 substrate: a pure DB COUNT filtered by ``backend_id`` + the
    in-flight status set (the per-backend replacement for the Phase-69-retired global window count).
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

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
        """Flip ``file`` to LOCAL_ANALYZING then enqueue ``process_file`` on the fileserver queue -- one txn, no commit.

        Re-homes the local ``enqueue_process_file`` producer (``analysis_enqueue``). Writes NO
        ``cloud_job`` row. An absent agent degrades to a clean hold (NoActiveAgentError -> ``False``),
        matching the cron no-op discipline -- never a raise.

        CR-01 (SCHED-01/03): AFTER the fileserver gate (so an absent agent leaves the file untouched) and
        BEFORE the enqueue, flip ``file.state = FileState.LOCAL_ANALYZING`` in the caller-passed session --
        mirroring ``ComputeAgentBackend``/``KueueBackend``'s ``FileState -> PUSHING`` flip. This removes the
        file from ``get_cloud_staging_candidates`` (which selects ``state == AWAITING_CLOUD``), so a
        locally-spilled file is no longer a drain candidate and can NOT be double-dispatched to a cloud
        backend while its ``process_file`` is in flight (the Backend.dispatch contract: dispatch "removes
        the file from further drain consideration"). NEVER commits -- the drain owns the single post-loop
        commit under the advisory lock, so the flip+enqueue are atomic (a rollback leaves the file
        AWAITING_CLOUD, safe to re-try, never a limbo LOCAL_ANALYZING without a queued job).
        """
        cfg = cast("ControlSettings", get_settings())
        try:
            agent = await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            logger.info("LocalBackend.dispatch hold: no fileserver agent online", file_id=str(file.id))
            return False
        # CR-01: leave the AWAITING_CLOUD candidate set in the SAME session, before the enqueue, no commit.
        file.state = FileState.LOCAL_ANALYZING
        queue = task_router.queue_for(agent.id)
        job = await enqueue_process_file(queue, file, agent.id, cfg.models_path)
        # WR-01: a deterministic-key ``process_file:<id>`` dedup returns None (the file is already being
        # analyzed locally) -> report NOT-newly-staged so the drain's staged tally is honest; a genuine
        # enqueue returns a saq.Job -> staged. Mirrors ComputeAgentBackend/KueueBackend's return contract.
        # The state flip above stands regardless of the dedup outcome (the file has left AWAITING_CLOUD).
        return job is not None

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:  # noqa: ARG002 -- protocol signature; local has no cron read
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

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
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

        # Re-home the compute enqueue leg (_enqueue_push_file, now local to this module), verbatim.
        push_queue = task_router.queue_for(fileserver_agent.id)
        job = await _enqueue_push_file(push_queue, file, fileserver_agent.id)
        # A deterministic-key dedup returns None (the file is already being pushed) -> the drain counts
        # it as skipped, not staged (T-50-double-enqueue); a genuine enqueue returns a saq.Job -> staged.
        return job is not None

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:  # noqa: ARG002 -- protocol signature; compute terminalizes via the /pushed callback
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

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
        """Flip ``file`` to PUSHING then run the no-commit S3-staging core VERBATIM (single-cluster, D-05).

        The S3 core (``cloud_staging._stage_file_to_s3``) already upserts the ``cloud_job`` row
        (``UPLOADING``) + enqueues one ``s3_upload`` in the caller's session -- exactly as today's kueue
        drain branch does -- so this writes NO second ``cloud_job`` row. D-05 keeps it single-cluster
        (reads ``active_kube`` / ``active_bucket``); per-cluster parameterization is Phase 70. NEVER
        commits (the drain owns the single post-loop commit -- Landmine L1). Always a genuine stage on
        the kueue path (the current drain counts every kueue file staged), so returns ``True``.
        """
        # D-03: the drain flips PUSHING before the per-kind fork; own that flip here so dispatch is atomic.
        file.state = FileState.PUSHING
        await _stage_file_to_s3(session, file, task_router)
        # Phase 69 (SCHED-02): the shared ``_stage_file_to_s3`` core upserts the cloud_job row but does
        # NOT stamp ``backend_id`` (it predates the registry). Stamp it here, in the SAME uncommitted
        # session, so this backend's ``in_flight_count`` (COUNT WHERE backend_id == self.id) counts the
        # row -- without it the drain would read kueue in-flight as 0 and overshoot the kueue cap.
        await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(backend_id=self.id))
        return True

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int]:
        """Reconcile THIS backend's in-flight ``cloud_job`` rows against their Kueue Job/Workload (backend_id-aware).

        Re-homes ``reconcile_cloud_jobs`` (L282-322): iterate ``cloud_job`` rows in {SUBMITTED, RUNNING}
        scoped to ``backend_id == self.id``, delegate each to the shared ``_reconcile_one`` under the
        per-row ``session.rollback()`` guard so one bad row never aborts the tick. ``ctx`` (carrying the
        re-drive ``queue``) is threaded to ``_reconcile_one``; it defaults to ``{}`` for the lay-and-prove
        unit path where no row reaches a re-drive.

        SCHED-02: each per-row unit of work FIRST acquires the drain's ``pg_advisory_xact_lock(5_000_504)``
        so a reconcile row-mutation and a ``stage_cloud_window`` snapshot are mutually exclusive per-row.
        ``_reconcile_one`` commits per row, which auto-releases the xact lock -- that per-row granularity
        is REQUIRED (Pitfall 2: a whole-tick lock would break the load-bearing delete-after-record
        ordering, which commits mid-tick). Reconcile only ever DECREMENTS in-flight (it never claims a
        slot), so this single shared drain lock is provably cap-safe (RESEARCH reconcile-only-decrements
        proof).
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
                # SCHED-02: acquire the drain's advisory lock at the TOP of each per-row unit of work
                # (per-row, not whole-tick) so this reconcile row-mutation is mutually exclusive with a
                # ``stage_cloud_window`` snapshot. ``_reconcile_one`` commits per row -> the xact lock
                # auto-releases at that commit, preserving the delete-after-record ordering.
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
                cloud_job = await session.get(CloudJob, cloud_job_id)
                if cloud_job is None:
                    continue
                tally["reconciled"] += 1
                await _reconcile_one(reconcile_ctx, session, cloud_job, cap, tally)
            except Exception:
                # Per-row guard: a single bad row never aborts the tick; roll back the partial mutation.
                await session.rollback()
                logger.warning("KueueBackend.reconcile: row reconcile failed; continuing", cloud_job_id=str(cloud_job_id), exc_info=True)
        # SCHED-05: return the per-backend tally so the cron aggregates it (replaces the old global tally).
        return tally


def resolve_backends(settings: ControlSettings) -> list[Backend]:
    """Build one :class:`Backend` impl per registry entry -- N non-local backends supported (Phase 69, SCHED-01).

    Phase 69 (SCHED-01) removes the Phase-68 ``>1``-non-local boot guard: multi-backend simultaneous
    dispatch is exactly this phase's job, so a registry with N non-local entries now resolves to a full
    ``list[Backend]`` of length N (+ any locals). The tiered drain
    (``release_awaiting_cloud.stage_cloud_window``) iterates this list, snapshots each backend's
    ``is_available`` / ``in_flight_count`` once per tick, and routes each candidate via the pure
    ``select_backend`` policy. Each impl binds to its Phase-67 discriminated-union submodel (``config``).

    The historical ``>1``-non-local defense-in-depth is retained ONLY for the non-drain call sites that
    still assume a single non-local kind (pipeline dashboard / backfill, agent_s3) -- it lives in
    :func:`resolved_non_local_kind` (WR-01), which those callers use; the drain no longer consults it.
    """
    resolved: list[Backend] = []
    for entry in settings.backends:
        if entry.kind == "local":
            resolved.append(LocalBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))
        elif entry.kind == "compute":
            resolved.append(ComputeAgentBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))
        elif entry.kind == "kueue":
            resolved.append(KueueBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))

    return resolved


def resolved_non_local_kind(settings: ControlSettings) -> str:
    """Return the registry-derived active kind: ``"local"`` when all-local, else the single non-local kind.

    The Wave-3 replacement for the deleted config dispatch-selector accessor (D-07/D-09): ``"local"`` when
    ``cloud_enabled`` is False, otherwise the sole non-local backend's kind (``"compute"`` | ``"kueue"``).
    The single-non-local invariant is enforced by :func:`resolve_backends`'s boot guard.
    """
    if not settings.cloud_enabled:
        return "local"
    non_local = [backend for backend in settings.backends if backend.kind != "local"]
    # WR-01: preserve the ≤1-non-local defense-in-depth the retired ``_single_non_local`` accessor gave the
    # three call sites (pipeline dashboard / backfill, agent_s3). Fail fast naming the offending ids rather
    # than silently picking non_local[0] -- mirrors :func:`resolve_backends`'s boot guard (multi-backend
    # dispatch is Phase 69 / SCHED). This keeps the all-local and single-non-local paths byte-identical.
    if len(non_local) > 1:
        raise ValueError(
            f"multi-backend dispatch lands in Phase 69 (SCHED): {len(non_local)} non-local backends "
            f"{[backend.id for backend in non_local]} are configured, but Phase 68 resolves only a ≤1-non-local registry"
        )
    return non_local[0].kind
