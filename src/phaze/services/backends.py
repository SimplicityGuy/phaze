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
* **D-05** -- ``KueueBackend`` calls ``_stage_file_to_s3`` / ``kube_staging`` threaded THIS backend's
  own ``KubeConfig`` + D-06 bucket (Phase 70 MKUE-01/02 retired the ``active_kube`` / ``active_bucket``
  module-global reads: one control plane dispatches to N distinct clusters/buckets).
* **D-07** -- the raise-on-``>1``-non-local guard is Phase-69-retired from :func:`resolve_backends` (N
  non-local backends now resolve; SCHED-01). Phase 70 (MKUE-01) further generalizes
  :func:`resolved_non_local_kind` to return ``"kueue"`` for ANY-kueue registry (N Kueue backends are the
  literal MKUE-01 scenario), retaining the fail-fast only for the ambiguous compute-only ``>1`` case;
  ``cloud_enabled`` stays in config as the registry on/off gate.
* **D-10** -- the in-flight status set is ``{UPLOADING, UPLOADED, SUBMITTED, RUNNING}``.

Cron no-op discipline (T-68-05): ``is_available`` / ``dispatch`` / ``reconcile`` degrade to a clean hold
(return ``False`` / a no-op) on an absent agent or a probe failure -- they never raise out to a cron.
Secret hygiene (T-68-04): this module logs only ``{id, kind, rank, cap}``-level fields, never a
``SecretStr`` / ``*_file`` value or a kube SA token.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol, cast
import uuid

from sqlalchemy import CursorResult, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.schemas.agent_tasks import PushFilePayload
from phaze.services import kube_staging, s3_staging
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.cloud_staging import _stage_file_to_s3
from phaze.services.enqueue_router import LANES, NoActiveAgentError, lane_for_task, select_active_agent, select_agent_by_id
from phaze.services.route_control import get_route_control
from phaze.tasks.push import PUSH_FILE_SAQ_RETRIES, push_file_saq_timeout_sec
from phaze.tasks.reconcile_cloud_jobs import _reconcile_one
from phaze.tasks.release_awaiting_cloud import _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY, push_file_job_key


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.config_backends import BackendConfig, ComputeBackend, KubeConfig
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


async def hold_awaiting_cloud(
    session: AsyncSession,
    file: FileRecord,
    *,
    attempts: int = 0,
    expect_status: Sequence[str] | None = None,
    clear_cloud_phase: bool = False,
) -> bool:
    """The SINGLE go-forward writer of ``cloud_job.status='awaiting'`` (D-01/D-02). NEVER commits.

    Shared by the hold path (``trigger_analysis``) and both over-cap spill paths
    (``report_upload_failed`` / ``report_push_mismatch``) so the hard shadow invariant
    ``AWAITING_CLOUD => cloud_job(status='awaiting')`` (verified pre-Phase-90 by the now-retired
    ``shadow_compare.py`` migration-verification script) holds for every go-forward hold instead of
    three hand-copied writers. ``expect_status`` selects one of two modes:

    * **Hold mode** (``expect_status is None``): the unconditional upsert. Phase 90 (D-09) removed the
      former AWAITING_CLOUD files.state dual-write, so this upserts ONLY the sidecar row keyed on ``file_id``
      (``uq_cloud_job_file_id``) INSERTing ``status='awaiting'`` / ``attempts=0`` (or ``on_conflict``
      re-stamping an existing row). Always returns ``True`` (the hold always writes).
    * **Spill mode** (``expect_status`` a non-empty status set): a rowcount-guarded CAS ONLY. UPDATEs the
      ``cloud_job`` row back to ``status='awaiting'`` iff its CURRENT status is in ``expect_status``,
      taking ``attempts`` from the argument so the spill caller retains
      ``attempts=cloud_submit_max_attempts`` as the budget-spent marker ``select_backend`` reads to route
      to local (D-03), and clearing ``cloud_phase`` iff ``clear_cloud_phase`` (the s3 spill sets it, the
      push spill must NOT touch it -- D-12). Returns ``res.rowcount > 0``: a ``False`` return means a
      late/duplicate callback matched an already-advanced row (0 rows), and the CALLER keeps its FULL
      no-op (no FileRecord write, no cleanup, no ledger clear -- D-10). This mode does NOT write
      ``file.state`` and does NOT touch the FileRecord: the caller owns the gated dual-write behind the
      returned bool.

    ``'awaiting'`` is deliberately OUT of :data:`IN_FLIGHT`, so a held/re-stamped row never inflates any
    backend's ``in_flight_count`` (D-03). NEVER commits in EITHER mode -- the caller owns the commit
    boundary (the dispatch discipline at :meth:`Backend.dispatch`; a commit here would drop the tick's
    ``pg_advisory_xact_lock`` and re-open the over-stage class, Landmine L1).
    """
    if expect_status is None:
        # Hold mode: the unconditional cloud_job upsert; always writes -> return True.
        # Phase 90 (D-09): the AWAITING_CLOUD files.state dual-write was removed; the cloud_job row
        # (status='awaiting') is the sole derived authority PR-A reads.
        stmt = pg_insert(CloudJob).values(
            # Stamp the PK explicitly (CR-01 defensive; mirrors ComputeAgentBackend.dispatch).
            id=uuid.uuid4(),
            file_id=file.id,
            status=CloudJobStatus.AWAITING.value,
            attempts=attempts,
        )
        stmt = stmt.on_conflict_do_update(
            # uq_cloud_job_file_id -> a plain INSERT is unsafe on the spill re-stamp case; upsert on file_id.
            index_elements=["file_id"],
            set_={"status": stmt.excluded.status, "attempts": stmt.excluded.attempts},
        )
        await session.execute(stmt)
        return True

    # Spill mode: rowcount-guarded CAS ONLY. Preserve the shipped D-09/D-10 guard -- an unconditional
    # upsert here would clobber an already-advanced row back to AWAITING_CLOUD (T-83-01/T-83-PUSH-CLOBBER).
    # Build the values so ``cloud_phase`` is ABSENT unless the caller asked to clear it (D-12): the s3 spill
    # clears it (WR-01, off the "Running" tile), the push spill must NOT touch it.
    values: dict[str, Any] = {"status": CloudJobStatus.AWAITING.value, "attempts": attempts}
    if clear_cloud_phase:
        values["cloud_phase"] = None
    res = cast(
        "CursorResult[Any]",
        await session.execute(update(CloudJob).where(CloudJob.file_id == file.id, CloudJob.status.in_(expect_status)).values(**values)),
    )
    return res.rowcount > 0


async def _enqueue_push_file(
    queue: Any,
    file: FileRecord,
    agent_id: str,
    *,
    dest_host: str,
    dest_scratch_dir: str,
    dest_ssh_user: str | None,
) -> Any:
    """Enqueue ONE ``push_file`` job with the deterministic key + the complete PushFilePayload.

    Relocated from ``release_awaiting_cloud`` in Wave 3 (68-04): this control-side enqueue leg is the
    ``ComputeAgentBackend.dispatch`` body, so it now lives with the backend that owns it (the drain no
    longer references it). Builds the four push-initiation ``PushFilePayload`` fields (the FileRecord's
    ``id`` / ``original_path`` / ``file_type`` plus the resolved fileserver ``agent_id``) AND stamps the
    Phase-73 per-file destination (``dest_host`` / ``dest_scratch_dir`` / ``dest_ssh_user``, D-02: the
    dispatch-side record-don't-rederive stamp), then serializes via ``model_dump(mode="json")`` so the
    UUID round-trips as a string under ``extra="forbid"``. Returns whatever ``queue.enqueue`` returns --
    a ``saq.Job`` normally, or ``None`` when SAQ deduped the deterministic key (the file is already being
    pushed) so the caller counts a ``None`` as skipped.
    """
    payload = PushFilePayload(
        file_id=file.id,
        original_path=file.original_path,
        file_type=file.file_type,
        agent_id=agent_id,
        dest_host=dest_host,
        dest_scratch_dir=dest_scratch_dir,
        dest_ssh_user=dest_ssh_user,
    )
    # Phase 36: the PostgresQueue broker pool is built open=False; connect() is idempotent.
    await queue.connect()
    # WR-03: stamp an explicit SAQ job-net timeout strictly above the agent's asyncio outer guard so
    # a job-net cancellation can never fire before the guard reaps the rsync child. phaze-2qpn: scale
    # it with the file size (the guard is size-derived on the agent) so a healthy multi-GB push is not
    # cancelled by a fixed cap, and allow retries so a killed push resumes via rsync --partial.
    return await queue.enqueue(
        "push_file",
        key=push_file_job_key(file.id),
        timeout=push_file_saq_timeout_sec(file.file_size),
        retries=PUSH_FILE_SAQ_RETRIES,
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
        BEFORE the enqueue, the file is enqueued for local analysis in the caller-passed session. Phase 90
        (D-09) removed the former LOCAL_ANALYZING files.state flip; the file leaves the cloud-staging
        candidate set via its ``process_file:<id>`` scheduling-ledger row (the derived inflight source), so a
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
        # Phase 90 (D-09): the LOCAL_ANALYZING files.state dual-write was removed. The file leaves the
        # AWAITING_CLOUD candidate set via the process_file:<id> scheduling-ledger row that
        # enqueue_process_file's before_enqueue hook writes (the derived inflight_clause source PR-A reads).
        queue = task_router.queue_for(agent.id, lane_for_task("process_file"))
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

    Phase 72 (MCOMP-01/D-02): ``is_available`` resolves THIS backend's bound ``agent_ref``
    (``self._agent_ref()``) against ``Agent.id`` per-call -- the record-don't-rederive twin of
    ``KueueBackend._kube()`` -- replacing the retired ``select_active_agent(kind="compute")``
    single-active-compute pick. Each compute entry gates on ITS bound agent, not "the single active
    compute agent" (Phase 73 builds dispatch/push/reconcile on this per-agent binding).
    """

    def _agent_ref(self) -> str:
        """Return THIS backend's bound ``agent_ref`` (the Phase-67 compute entry's dispatch node, D-02).

        ``self.config`` is the ``ComputeBackend`` submodel bound in ``resolve_backends``; its
        ``agent_ref`` is the ``Agent.id`` this backend dispatches to. Fail-loud (``ValueError`` naming
        ``self.id``) if a compute backend somehow has no ``agent_ref`` bound -- the
        ``_require_dispatch_fields`` validator already guarantees it non-empty at construction, so this
        is defense-in-depth (mirrors ``KueueBackend._kube()``).
        """
        agent_ref = getattr(self.config, "agent_ref", None)
        if not agent_ref:
            raise ValueError(f"compute backend {self.id!r} has no agent_ref bound")
        return cast("str", agent_ref)

    def _destination(self) -> tuple[str, str, str | None]:
        """Return THIS backend's push destination ``(push_host, scratch_dir, ssh_user)`` (D-02).

        ``self.config`` is typed ``BackendConfig | None`` (the discriminated union), so DIRECT attribute
        access fails mypy on the union -- read via the union-safe ``getattr`` idiom (mirrors
        ``_agent_ref()`` / ``KueueBackend._kube()``). ``push_host`` and ``scratch_dir`` are guaranteed
        non-empty at construction by ``ComputeBackend._require_dispatch_fields``, so a missing value here
        is a bound-config invariant break -- fail loud (``ValueError`` naming ``self.id``) rather than
        silently stamping a ``"None:..."`` remote spec. ``ssh_user`` stays optional (``None`` allowed).
        """
        push_host = getattr(self.config, "push_host", None)
        scratch_dir = getattr(self.config, "scratch_dir", None)
        if not push_host or not scratch_dir:
            raise ValueError(f"compute backend {self.id!r} has no push_host/scratch_dir bound")
        ssh_user = getattr(self.config, "ssh_user", None)
        return cast("str", push_host), cast("str", scratch_dir), cast("str | None", ssh_user)

    async def is_available(self, session: AsyncSession) -> bool:
        """D-02: True iff THIS backend's bound ``agent_ref`` names an ONLINE compute agent; False when absent.

        Resolves the per-entry binding (``self._agent_ref()`` -> ``Agent.id``) via
        :func:`select_agent_by_id`, reading ``self.config.agent_ref`` per-call (record-don't-rederive).
        An absent / unregistered / offline bound agent degrades to a hold (``NoActiveAgentError`` ->
        ``False``), preserving the cron no-op discipline (T-68-05, D-05). A backend with no ``agent_ref``
        bound fails loud via ``_agent_ref()`` (defense-in-depth) rather than silently holding.
        """
        try:
            await select_agent_by_id(session, self._agent_ref(), kind="compute")
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

        phaze-uciu.3: the upsert + enqueue run inside a ``session.begin_nested()`` SAVEPOINT. SAQ's
        ``PostgresQueue`` enqueue uses its OWN psycopg pool -- an enqueue failure raises WITHOUT
        poisoning this asyncpg session/transaction, so a bare (un-savepointed) upsert-then-raise would
        leave the ``status=SUBMITTED``/``backend_id``-stamped row intact for the drain's post-loop
        commit: a stranded row (unrecoverable -- reconcile/orphan-recovery both scope away from
        in-flight cloud_jobs) that permanently consumes an ``in_flight_count`` cap slot. The SAVEPOINT
        rolls back ONLY this upsert on a raise, restoring the row's prior (pre-dispatch) state --
        typically ``status='awaiting'`` (D-01) -- while the outer transaction (and its
        ``pg_advisory_xact_lock``) stays alive so the tick's other candidates are unaffected. The raise
        itself still propagates to the caller (the drain's per-candidate ``except`` clauses).
        """
        # Gate on the fileserver agent (the push initiator) BEFORE mutating: absent -> clean hold, nothing written.
        fileserver_agent = await select_active_agent(session, kind="fileserver")

        async with session.begin_nested():
            # D-03: upsert the cloud_job row in the SAME session, before the enqueue. Phase 90 (D-09):
            # the paired PUSHING files.state dual-write was removed; the cloud_job (status=SUBMITTED) is
            # authority.
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

            # Re-home the compute enqueue leg (_enqueue_push_file, now local to this module) + D-02:
            # stamp THIS backend's destination onto the push payload (record-don't-rederive originates at
            # dispatch; NO re-lookup via resolve_compute_backend here -- the bound self.config already
            # holds it). A raise here (SAQ's own pool, e.g. connect()/enqueue() blowing up) rolls back
            # ONLY the upsert above (phaze-uciu.3).
            push_host, scratch_dir, ssh_user = self._destination()
            push_queue = task_router.queue_for(fileserver_agent.id, lane_for_task("push_file"))
            job = await _enqueue_push_file(
                push_queue,
                file,
                fileserver_agent.id,
                dest_host=push_host,
                dest_scratch_dir=scratch_dir,
                dest_ssh_user=ssh_user,
            )
        # A deterministic-key dedup returns None (the file is already being pushed) -> the drain counts
        # it as skipped, not staged (T-50-double-enqueue); a genuine enqueue returns a saq.Job -> staged.
        return job is not None

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:  # noqa: ARG002 -- protocol signature; compute terminalizes via the /pushed callback
        """No-op: compute terminalization is the existing ``/pushed`` callback path (§4.2, D-08), not a cron read."""
        return None


class KueueBackend(_BaseBackend):
    """Kueue-cluster backend -- re-homes today's single-cluster S3-staging + kube submit/reconcile (D-05).

    ``is_available`` probes THIS backend's Kueue LocalQueue with NO compute-agent dependency (D-01a);
    ``dispatch`` picks the D-06 bucket and runs the no-commit S3-staging core; ``reconcile`` re-homes the
    ``reconcile_cloud_jobs`` cron body, made ``backend_id``-aware, under a per-row advisory lock.
    ``in_flight_count`` is inherited from :class:`_BaseBackend` (the D-02 substrate).

    Phase 70 (MKUE-01/D-04): every ``kube_staging`` call is threaded THIS backend's own
    ``KubeConfig`` (``self._kube()``) -- one control plane dispatches to N distinct clusters, each with
    its own constructor-time-authed kr8s client (the module-global ``active_kube`` read is retired).
    """

    def _kube(self) -> KubeConfig:
        """Return THIS backend's ``KubeConfig`` (the bound kueue registry entry's ``[kube]`` table, D-04).

        ``self.config`` is the Phase-67 ``KueueBackend`` submodel bound in ``resolve_backends``; its
        ``kube`` field is the per-cluster connection surface every ``kube_staging`` verb now takes.
        Fail-loud (``KubeStagingError``) if a kueue backend somehow has no ``[kube]`` bound -- the
        config validator already guards this, so this is defense-in-depth.
        """
        kube = getattr(self.config, "kube", None)
        if kube is None:
            raise kube_staging.KubeStagingError(f"kueue backend {self.id!r} has no [kube] config bound")
        return cast("KubeConfig", kube)

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature; kueue probes the cluster, not a DB agent
        """Probe THIS backend's Kueue LocalQueue -- True iff reachable; False (never raises) on any probe failure (D-01a).

        Re-homes the ``kube_staging.get_local_queue`` reachability probe, now threaded THIS backend's
        ``KubeConfig`` (MKUE-01/03). Deliberately has NO compute-agent dependency (D-01a asymmetry):
        ephemeral Kueue pods have no persistent compute agent. A ``NotFoundError`` (mis-named queue) or
        transient ``ServerError`` (or an unconfigured ``[kube]``) degrades to False rather than raising
        (mirrors the controller's non-fatal catch), preserving the cron no-op discipline.
        """
        try:
            local_queue = await kube_staging.get_local_queue(self._kube())
        except Exception:  # any kube/mesh failure degrades to "unavailable" (T-68-05 no-op discipline)
            logger.info("KueueBackend.is_available: LocalQueue probe failed -> unavailable", backend_id=self.id)
            return False
        return local_queue is not None

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
        """Pick the D-06 bucket, run the no-commit S3-staging core, THEN flip ``file`` to PUSHING (MKUE-02).

        Phase 70 (MKUE-02/D-06): pick the file's staging bucket deterministically over this backend's
        bound bucket set (``self.config.buckets``), resolve its ``BucketConfig``, thread it into the
        shared ``_stage_file_to_s3`` core (which stamps ``staging_bucket`` on the upsert), and RECORD both
        ``backend_id`` AND ``staging_bucket`` in the SAME uncommitted session so this backend's
        ``in_flight_count`` (COUNT WHERE backend_id == self.id) counts the row and every downstream
        presign/cleanup READS the recorded bucket (never re-derives). NEVER commits (the drain owns the
        single post-loop commit -- Landmine L1). Always a genuine stage on the kueue path, so returns ``True``.

        CR-01 (gate-before-mutate, Pitfall 4 limbo guard): the ``FileState -> PUSHING`` flip lands ONLY
        AFTER ``_stage_file_to_s3`` returns successfully -- NOT before it, as ``LocalBackend`` /
        ``ComputeAgentBackend`` also gate their fileserver-agent check before any state mutation.
        ``_stage_file_to_s3`` resolves the fileserver agent FIRST (``select_active_agent(kind="fileserver")``)
        and reads NOTHING from ``file.state``, so a ``NoActiveAgentError`` (or any pre-upsert S3 raise)
        leaves ``file`` completely untouched. Were the flip to precede the call, SQLAlchemy's default
        ``autoflush`` would flush the pending PUSHING change as a side effect of that gate's ``SELECT``,
        and the drain's single post-loop commit would then persist a PUSHING file with no ``cloud_job``
        row -- the exact "limbo row" this ordering forbids.

        phaze-uciu.3: ``_stage_file_to_s3`` itself wraps its ``cloud_job`` upsert + ``s3_upload`` enqueue
        in a ``session.begin_nested()`` SAVEPOINT, so a failed enqueue (SAQ's own psycopg pool, not this
        asyncpg session) rolls back ONLY that upsert -- restoring the row's prior ``status`` (typically
        ``awaiting``) -- and re-raises out of this ``dispatch`` to the caller, leaving the outer
        transaction (and the drain's ``pg_advisory_xact_lock``) alive.
        """
        cfg = cast("ControlSettings", get_settings())
        # D-06: deterministic per-file bucket over this backend's bound set; the returned id is authoritative.
        # Pure/no-DB: pick + resolve mutate nothing, so a resolution failure here is also mutation-free.
        bucket_ids = list(getattr(self.config, "buckets", []) or [])
        bucket_id = s3_staging.pick_bucket(file.id, bucket_ids)
        bucket = s3_staging.resolve_bucket_config(cfg, bucket_id)
        if bucket is None:
            raise s3_staging.S3StagingError(f"kueue backend {self.id!r} bucket {bucket_id!r} is not in the resolved registry")
        # Gate (fileserver agent) + stage BEFORE the state flip: _stage_file_to_s3 reads no file.state, so a
        # NoActiveAgentError / pre-upsert S3 raise touches nothing (CR-01 Pitfall 4 limbo guard).
        await _stage_file_to_s3(session, file, task_router, bucket)
        # Phase 90 (D-09): the PUSHING files.state dual-write was removed; the cloud_job row (updated
        # below with backend_id + staging_bucket) is the sole derived authority now that staging succeeded.
        # Record backend_id + the D-06 staging_bucket in the SAME uncommitted session (MKUE-02/D-01):
        # in_flight_count is backend_id-scoped, and presign/cleanup read staging_bucket authoritatively.
        await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(backend_id=self.id, staging_bucket=bucket_id))
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
                # MKUE-01/D-04: thread THIS backend's KubeConfig so every get_job/get_workload_for/
                # delete_job inside reconcile targets the file's own cluster.
                await _reconcile_one(reconcile_ctx, session, cloud_job, cap, tally, self._kube())
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


def resolve_compute_backend(cfg: ControlSettings, backend_id: str | None) -> ComputeBackend | None:
    """Resolve a recorded ``cloud_job.backend_id`` to its ``ComputeBackend`` registry entry (D-06).

    The single AUTHORITATIVE inverse of ``ComputeAgentBackend.dispatch``'s ``backend_id`` stamp: every
    downstream scratch / terminalization reader (the Plan-02 rsync destination, the Plan-03 ``/pushed`` +
    ``/mismatch`` callbacks) resolves the value RECORDED on ``cloud_job.backend_id`` through here rather
    than re-deriving it. Mirrors ``s3_staging.resolve_bucket_config`` exactly: pure + ORM-free, reads only
    ``cfg.backends``.

    Returns ``None`` when ``backend_id`` is ``None`` (an all-local / unstamped row), or when the id names
    no ``kind == "compute"`` entry (a kueue/local id, or an operator-removed backend) so the caller can
    skip the compute op cleanly.
    """
    if backend_id is None:
        return None
    return {backend.id: backend for backend in cfg.backends if backend.kind == "compute"}.get(backend_id)


def resolved_non_local_kind(settings: ControlSettings) -> str:
    """Return the registry-derived cloud-lane kind: ``"local"`` when all-local, else the non-local kind.

    The single seam the non-drain single-kind callers use (the S3-upload-complete callback
    ``agent_s3.report_uploaded``, the ``/pipeline/stats`` poll ``build_dashboard_context``, and the
    backfill route): they only ask "is the cloud lane kueue?". ``"local"`` when ``cloud_enabled`` is
    False.

    Phase 70 (MKUE-01, sibling of the Pitfall-1 ``active_compute_scratch_dir`` fix): the callers 500'd
    the moment a 2nd Kueue backend was declared, because the old ``>1``-non-local blanket raise fired on
    the literal MKUE-01 scenario. Generalize: when ANY non-local backend is ``"kueue"``, return
    ``"kueue"`` -- this tolerates N Kueue backends AND a local + N-Kueue + 1-compute registry (the
    callers degrade gracefully by construction, no per-site try/except needed). Phase 72 (MCOMP-01,
    D-03) retires the compute-only ``>1`` fail-fast too: the compute-only branch now returns ``"compute"``
    for N compute backends (per-agent dispatch attribution lands in Phase 73). All-local -> ``"local"``,
    single-kueue -> ``"kueue"``, single-compute -> ``"compute"`` stay byte-identical.
    """
    if not settings.cloud_enabled:
        return "local"
    non_local = [backend for backend in settings.backends if backend.kind != "local"]
    if any(backend.kind == "kueue" for backend in non_local):
        return "kueue"
    # No kueue backend -> compute-only. Phase 72 (D-03) retired the ambiguous >1-compute fail-fast; the
    # compute-only branch returns "compute" for any N compute (per-agent attribution lands in Phase 73).
    return non_local[0].kind


# --- Phase 71 (BEUI-01): read-only backend-lane snapshot for the N-lane UI poll -----------
#
# A pure read over the Phase-67 registry + the ``cloud_job`` in-flight/admission substrate that feeds
# the BEUI-01 N-lane grid on the existing 5s ``/pipeline/stats`` poll (Plan 03 seeds + renders it). Every
# leg is degrade-safe -- a DB hiccup or a hung Kueue probe can NEVER raise into the hot poll (T-71-03) --
# and secret-free: only ``{id, kind, rank, cap, in_flight, available, quota_wait, inadmissible}`` ever
# leaves this module; a probe-failure log carries ``backend_id`` ONLY, never a SecretStr / kube SA token
# / S3 key (SP-5, T-71-01). This plan builds the DATA path only -- no template, no context wiring.


# D-02/A2: the per-probe availability timeout -- well under the 5s poll, yet tolerant of a slow-healthy
# kr8s ``LocalQueue`` RTT. A hung Kueue cluster times out to "offline" for THAT lane alone (T-71-02).
_PROBE_TIMEOUT_SEC = 1.5

# The zero-admission fallback merged into a lane with no attributed ``cloud_job`` rows (idle/local lanes).
_ZERO_ADMISSION: dict[str, int] = {"quota_wait": 0, "inadmissible": 0}


async def _admission_by_backend_id(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Return per-``backend_id`` admission counts ``{quota_wait, inadmissible}`` via one ``GROUP BY`` (D-03).

    Generalizes the GLOBAL ``pipeline.get_cloud_phase_counts`` (``cloud_phase == QUEUED_BEHIND_QUOTA``) +
    ``pipeline.get_inadmissible_count`` (``inadmissible`` AND ``status IN {SUBMITTED, RUNNING}``) predicates
    to a per-``backend_id`` ``GROUP BY`` so each Kueue lane owns its OWN quota-wait-vs-Inadmissible counts.
    ``cloud_phase`` is NULL for local/compute rows, so they contribute 0 to ``quota_wait``; ``backend_id``
    -NULL (legacy / unattributed) rows are excluded entirely (they belong to no lane). Degrades to ``{}``
    on any DB error with a guarded rollback (mirrors ``pipeline._safe_count``) so it never raises into the
    hot 5s poll (T-71-03).
    """
    try:
        stmt = (
            select(
                CloudJob.backend_id,
                func.count().filter(CloudJob.cloud_phase == CloudPhase.QUEUED_BEHIND_QUOTA.value).label("quota_wait"),
                func.count()
                .filter(
                    CloudJob.inadmissible.is_(True),
                    CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
                )
                .label("inadmissible"),
            )
            .where(CloudJob.backend_id.is_not(None))
            .group_by(CloudJob.backend_id)
        )
        rows = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("backend_lane_admission_degraded", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("backend_lane_admission_rollback_failed", exc_info=True)
        return {}
    return {backend_id: {"quota_wait": int(quota_wait or 0), "inadmissible": int(inadmissible or 0)} for backend_id, quota_wait, inadmissible in rows}


async def _probe_one(session: AsyncSession, backend: Backend) -> tuple[str, bool]:
    """Probe ONE backend's live availability, bounded + degrade-safe -> ``(backend_id, available)`` (D-02).

    A :class:`LocalBackend` is short-circuited to ``True`` with NO I/O (local dispatch never depends on a
    remote agent). Every other backend's ``is_available`` is awaited under an ``asyncio.wait_for`` bounded
    by ``_PROBE_TIMEOUT_SEC``; a timeout OR any probe exception degrades THAT lane to offline and logs the
    ``backend_id`` ONLY (never a SecretStr / kube token, T-71-01). A single hung Kueue cluster can
    therefore never stall the shared read (T-71-02).
    """
    if isinstance(backend, LocalBackend):
        return (backend.id, True)
    try:
        available = await asyncio.wait_for(backend.is_available(session), _PROBE_TIMEOUT_SEC)
    except Exception:
        logger.info("backend_lane_probe_offline", backend_id=backend.id)
        return (backend.id, False)
    return (backend.id, bool(available))


async def _probe_availability(session: AsyncSession, backends: list[Backend]) -> dict[str, bool]:
    """Probe every backend SEQUENTIALLY on the one shared session -> ``{backend_id: available}`` (D-02).

    The probes run one at a time in a plain ``for`` loop: each ``_probe_one`` is fully awaited before the
    next begins, so there is NEVER concurrent use of the shared ``AsyncSession`` -- Session-safety
    (Pitfall 1) holds by CONSTRUCTION, guaranteed by the serial control flow. Since Phase 72 (MCOMP-01) retired the
    single-active-compute assumption, N≥2 compute backends are legal and each compute probe touches the
    shared ``session`` via ``select_agent_by_id`` (``session.execute``); serializing the fan-out guarantees
    those ``session.execute`` calls can never overlap (SQLAlchemy forbids concurrent operations on one
    session). Each ``_probe_one`` is individually capped by ``asyncio.wait_for(..., _PROBE_TIMEOUT_SEC)``;
    because the probes now run one at a time the worst-case aggregate wait is ``N x _PROBE_TIMEOUT_SEC``
    (not the old ``asyncio.gather`` ~1x bound) -- a deliberate D-01 trade-off, acceptable because N is small
    (registry-declared local + N-Kueue + N-compute) and session-safety takes priority over probe latency on
    the 5s ``/pipeline/stats`` poll. The post-fan-out ``session.rollback`` in
    :func:`get_backend_lane_snapshot` clears any single-probe DB poison before the ``in_flight_count``
    reads. Kueue probes ignore the session (kr8s I/O) and local is short-circuited (no I/O).
    """
    results: dict[str, bool] = {}
    for backend in backends:
        backend_id, available = await _probe_one(session, backend)
        results[backend_id] = available
    return results


def _kind_of(backend: Backend) -> str:
    """Derive the lane ``kind`` ("local"/"compute"/"kueue") from the impl class (mirrors resolve_backends)."""
    if isinstance(backend, LocalBackend):
        return "local"
    if isinstance(backend, ComputeAgentBackend):
        return "compute"
    if isinstance(backend, KueueBackend):
        return "kueue"
    return "unknown"


async def get_backend_lane_snapshot(session: AsyncSession) -> list[dict[str, Any]]:
    """Return one rank-ascending, secret-free lane dict per registry backend for the BEUI-01 grid.

    Resolves the Phase-67 registry, then composes one lane per backend from three degrade-safe reads:
    ``_admission_by_backend_id`` (per-``backend_id`` quota_wait/inadmissible, D-03), ``_probe_availability``
    (live bounded is_available probes, D-02) and each backend's ``in_flight_count`` (the D-02 cloud_job
    substrate). Lanes are sorted rank-ascending, tie-broken by ``id`` (D-06), so the Plan-03 template loops
    them verbatim. A :class:`LocalBackend` lane always shows ``in_flight`` 0 and ``available`` True.

    Every lane carries ONLY ``{id, kind, rank, cap, in_flight, available, quota_wait, inadmissible}`` -- no
    ``config``, no ``SecretStr``, no kube/S3 token (T-71-01). Any top-level exception degrades to ``[]``
    with a guarded rollback so it can NEVER raise into the hot 5s ``/pipeline/stats`` poll (SP-1, T-71-03).
    """
    try:
        backends = resolve_backends(cast("ControlSettings", get_settings()))
        admission = await _admission_by_backend_id(session)
        availability = await _probe_availability(session, backends)
        # T-71-02 per-lane isolation: a compute ``is_available`` probe can fail at the DB layer
        # (not just time out), poisoning the shared session. Clear it after the fan-out -- the
        # snapshot does no writes, so a rollback here is safe -- so one bad lane degrades to
        # ``available=False`` (via ``_probe_one``) instead of poisoning the subsequent
        # ``in_flight_count`` reads and collapsing the WHOLE grid to the ``[]`` degrade panel.
        await session.rollback()
        lanes: list[dict[str, Any]] = []
        for backend in backends:
            lanes.append(
                {
                    "id": backend.id,
                    "kind": _kind_of(backend),
                    "rank": backend.rank,
                    "cap": backend.cap,
                    "in_flight": await backend.in_flight_count(session),
                    "available": availability.get(backend.id, False),
                    **admission.get(backend.id, _ZERO_ADMISSION),
                }
            )
        lanes.sort(key=lambda lane: (lane["rank"], lane["id"]))
    except Exception:
        logger.warning("backend_lane_snapshot_degraded", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("backend_lane_snapshot_rollback_failed", exc_info=True)
        return []
    return lanes


# The neutral, no-causal-claim copy any unexpected error in derive_cloud_hold_reason degrades to --
# the card must never assert a specific blocker it has not actually confirmed.
_HOLD_REASON_DEGRADED = "held"


async def derive_cloud_hold_reason(session: AsyncSession) -> str:
    """Return the Cloud Routing card's truthful sub-caption, mirroring the drain's own gate order.

    Lives next to :func:`get_backend_lane_snapshot` so the two can never drift apart -- this walks
    the SAME per-lane ``{available, cap, in_flight}`` shape that function already composes, and
    checks the SAME gates ``release_awaiting_cloud.stage_cloud_window`` checks, in the SAME order:
    cloud disabled -> :func:`get_route_control` force-local -> no lane reachable -> every reachable
    lane full -> no fileserver agent online -> else genuinely queued with free capacity. A prior
    incarnation of this card hardcoded "no compute agent online" regardless of the real blocker
    (T-83-hold-reason-bug); this derivation can only ever name a gate it has actually observed.

    Every read here is individually degrade-safe (``get_route_control`` / ``get_backend_lane_snapshot``
    never raise), but the surrounding ``try/except`` is the belt: ANY unexpected exception -- including
    one from ``get_settings()`` or the fileserver probe -- collapses to the neutral
    :data:`_HOLD_REASON_DEGRADED` copy with NO causal claim, so the hot 5s poll can never 500 on a
    hiccup here (mirrors the T-71-03 idiom this module already applies throughout).
    """
    try:
        cfg = cast("ControlSettings", get_settings())
        if not cfg.cloud_enabled:
            return "cloud routing disabled"
        if await get_route_control(session):
            return "held — cloud routing paused (force-local)"

        lanes = await get_backend_lane_snapshot(session)
        available_lanes = [lane for lane in lanes if lane["available"]]
        if not available_lanes:
            return "held — no cloud backend reachable"

        total_cap = sum(lane["cap"] for lane in available_lanes)
        total_in_flight = sum(lane["in_flight"] for lane in available_lanes)
        free_slots = sum(max(0, lane["cap"] - lane["in_flight"]) for lane in available_lanes)
        if free_slots <= 0:
            return f"held — all lanes at capacity ({total_in_flight}/{total_cap} slots busy)"

        try:
            await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            return "held — no fileserver agent online"

        return f"queued — {free_slots} free slots, dispatching on next drain tick (~5 min)"
    except Exception:
        logger.warning("cloud_hold_reason_degraded", exc_info=True)
        return _HOLD_REASON_DEGRADED


# --- Phase 88 (88-02, DRILL-01): degrade-safe lane-detail data helpers ---------------------
#
# Two bounded, read-only, secret-free reads that feed the `GET /pipeline/lanes/{backend_id}` body
# (_lane_detail.html). Both degrade to [] / 0 on any error so they can NEVER 500 the drill-in pane's
# own 5s tick (D-00b / PERF-01). Neither exposes any config/SecretStr/kube token -- only the CloudJob
# status/timestamp/file_id scalars and the broker depth counts.

# D-07: the fixed last-N cap on the recent-completions list -- predictable render cost under any
# throughput (no whole-corpus scan per poll). Newest-first, bounded by this LIMIT.
LANE_RECENT_N = 20


async def get_lane_recent_completions(session: AsyncSession, backend_id: str, kind: str, limit: int = LANE_RECENT_N) -> list[CloudJob]:
    """Return up to ``limit`` most-recent succeeded ``CloudJob`` rows for a compute/kueue lane (D-07).

    For a ``local`` lane returns ``[]`` unconditionally: a :class:`LocalBackend` writes NO ``cloud_job``
    row (``in_flight_count`` is always 0, the dispatch short-circuit), so it has no cloud completions to
    show -- the template renders the "No completions in the last N" empty state instead (Open Question 1
    resolution: omit, don't fabricate). For compute/kueue lanes the query is bounded by ``updated_at``
    DESC + ``LIMIT limit`` (D-07); any query error degrades to ``[]`` with a guarded rollback so it can
    never raise into the hot 5s tick (D-00b). Secret-free: only the CloudJob row scalars leave here.
    """
    if kind == "local":
        return []
    try:
        stmt = (
            select(CloudJob)
            .where(
                CloudJob.backend_id == backend_id,
                CloudJob.status == CloudJobStatus.SUCCEEDED.value,
            )
            .order_by(CloudJob.updated_at.desc())
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())
    except Exception:
        logger.warning("lane_recent_completions_degraded", backend_id=backend_id, exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("lane_recent_completions_rollback_failed", backend_id=backend_id, exc_info=True)
        return []
    return rows


async def get_lane_queue_depths(app_state: Any, backend_id: str) -> dict[str, int]:
    """Return per-lane-tier queue depth ``{analyze, fingerprint, meta, io}`` for a lane's backing agent.

    Mirrors the ``get_queue_activity`` idiom (services/pipeline.py): each tier's depth is
    ``count("queued") + count("active")`` on the ``phaze-agent-<backend_id>-<lane>`` Queue. Only the
    ``queued`` / ``active`` kinds are read (scheduled/cron jobs excluded). Every tier is isolated in its
    own ``try/except -> 0`` so a missing ``app.state.task_router`` (the test lifespan-skip) or a broker
    hiccup degrades that tier to 0 and NEVER 500s the 5s tick (D-00b). Bounded: one count pair per tier,
    no corpus scan. A kueue/unattributed lane whose id names no live agent queue simply reads 0s.
    """
    depths: dict[str, int] = dict.fromkeys(LANES, 0)
    for lane in LANES:
        try:
            queue = app_state.task_router.queue_for(backend_id, lane)
            depths[lane] = await queue.count("queued") + await queue.count("active")
        except Exception:
            depths[lane] = 0
            logger.warning("lane_queue_depth_degraded", backend_id=backend_id, lane=lane, exc_info=True)
    return depths
