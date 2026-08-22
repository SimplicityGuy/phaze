"""``ComputeAgentBackend`` -- the rsync/push-over-Tailscale lane and its stranded-SUBMITTED reaper.

Extracted verbatim from the former single-module ``services/backends.py`` (phaze-dr9df) -- no body
was rewritten. :meth:`ComputeAgentBackend._reap_stranded_submitted` deliberately keeps its inline
per-row loop: its twin :meth:`phaze.services.backends.kueue.KueueBackend._reap_stranded_staging` had
to shed one level (its post-commit S3 cleanup moved to a helper) to reach the nesting budget, but
this method has no such tail and already sits inside it. Structural symmetry with the twin is NOT a
reason to churn a reaper with a four-bug-fix history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services.backends.admission import (
    _build_push_file_enqueue_kwargs,
    _park_push_file_enqueue,
    _PendingPushFileEnqueue,
    hold_awaiting_cloud,
)
from phaze.services.backends.base import _BaseBackend
from phaze.services.enqueue_router import NoActiveAgentError, lane_for_task, select_active_agent, select_agent_by_id
from phaze.services.pipeline import get_live_job_keys
from phaze.services.scheduling_ledger import clear_ledger_entry
from phaze.tasks.release_awaiting_cloud import _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)


class ComputeAgentBackend(_BaseBackend):
    """Cloud-compute (rsync/push over Tailscale) backend -- re-homes the ``push_file`` control-side enqueue leg.

    ``is_available`` re-homes GATE-1 (``release_awaiting_cloud`` L145-150): True iff a compute agent is
    online. ``dispatch`` owns a NEW in-txn ``cloud_job`` write (Pitfall 1 / D-03) then PARKS the
    ``push_file`` enqueue (phaze-s5sz, :func:`_park_push_file_enqueue`). Compute terminalization is
    PRIMARILY the existing ``/pushed``/``/mismatch``/``/failed`` callback path (§4.2, D-08); ``reconcile``
    (phaze-j7m18) is the age-bounded safety net for a row those callbacks never reach --
    :meth:`_reap_stranded_submitted`, the compute twin of ``KueueBackend._reap_stranded_staging``.
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
        """Upsert the ``cloud_job`` row, THEN PARK the ``push_file`` enqueue -- one txn, no commit.

        D-03 write ordering: the ``cloud_job`` upsert (``backend_id`` set, ``s3_key`` NULL -- compute
        carries no S3 object, ``status=SUBMITTED``) lands in the caller-passed session; the fileserver
        gate runs first so an absent agent is a clean hold with nothing mutated. NEVER commits -- the
        drain owns the single post-loop commit so the ``pg_advisory_xact_lock`` survives the tick
        (Landmine L1). Always returns ``True`` (a genuine stage) -- mirrors ``KueueBackend.dispatch``.

        phaze-s5sz (supersedes phaze-uciu.3): the ``push_file`` enqueue is PARKED
        (:func:`_park_push_file_enqueue`), not fired inline. SAQ's ``PostgresQueue`` enqueue commits the
        job durably + immediately on its OWN psycopg pool, independent of THIS asyncpg session's commit
        boundary -- an inline enqueue made the job (and a FAST rsync push's own ``/pushed`` callback)
        worker-visible BEFORE the drain's post-loop commit landed this SUBMITTED row. ``report_pushed``'s
        ONLY guard is ``cloud_job.status == 'submitted'`` (SC#1/D-12); under READ COMMITTED that fast
        callback saw the row's PREVIOUS committed status (typically 'awaiting'), matched 0 rows, and took
        the idempotent-no-op hold FOREVER -- at the time nothing else owned recovery for an in-flight
        cloud_job (``ComputeAgentBackend.reconcile`` was a documented no-op; ``recover_orphaned_work``
        excludes any file carrying an in-flight ``cloud_job`` row on the premise that the ``/pushed``
        callback owns it -- phaze-j7m18 later added ``reconcile``'s age-bounded
        :meth:`_reap_stranded_submitted` as the backstop for exactly this class of lost callback).
        Parking the enqueue removes it from the transaction entirely, so the drain fires it via
        ``flush_pending_push_file_enqueues`` ONLY AFTER the single post-loop commit -- the worker-visible
        job can never precede the committed row it reads. Because the enqueue is no longer in the
        transaction, the ``session.begin_nested()`` SAVEPOINT phaze-uciu.3 added to protect it is dead
        weight and is dropped here (mirrors the kueue twin, phaze-grzo).
        """
        # Gate on the fileserver agent (the push initiator) BEFORE mutating: absent -> clean hold, nothing written.
        fileserver_agent = await select_active_agent(session, kind="fileserver")

        # D-03: upsert the cloud_job row in the caller's session. Phase 90 (D-09): the paired PUSHING
        # files.state dual-write was removed; the cloud_job (status=SUBMITTED) is authority.
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
            set_={
                "backend_id": stmt.excluded.backend_id,
                "status": stmt.excluded.status,
                # phaze-7634: CloudJob.updated_at is a client-side ``onupdate=func.now()``
                # (TimestampMixin) that SQLAlchemy does NOT inject into an ON CONFLICT SET (and
                # there is no DB trigger), so a re-dispatch would otherwise leave updated_at
                # frozen at the row's PREVIOUS write (same defect class as the hold-mode upsert
                # above / phaze-c8nz). Stamp it explicitly so a re-dispatch bumps the clock.
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)

        # D-02: stamp THIS backend's destination onto the push payload (record-don't-rederive
        # originates at dispatch; NO re-lookup via resolve_compute_backend here -- the bound
        # self.config already holds it). PARK the enqueue rather than firing it (phaze-s5sz).
        push_host, scratch_dir, ssh_user = self._destination()
        push_queue = task_router.queue_for(fileserver_agent.id, lane_for_task("push_file"))
        enqueue_kwargs = _build_push_file_enqueue_kwargs(
            file,
            fileserver_agent.id,
            dest_host=push_host,
            dest_scratch_dir=scratch_dir,
            dest_ssh_user=ssh_user,
        )
        _park_push_file_enqueue(session, _PendingPushFileEnqueue(queue=push_queue, enqueue_kwargs=enqueue_kwargs))
        return True

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int]:  # noqa: ARG002 -- protocol signature; the cloud_job read itself is compute's only cron work
        """Reap THIS backend's age-stranded SUBMITTED rows (phaze-j7m18); compute has no Job/Workload to reconcile.

        Compute's ONLY in-flight status is SUBMITTED (D-08, D-10) and it is terminalized SOLELY by the
        ``/pushed``/``/mismatch``/``/failed`` agent HTTP callbacks -- there is no Kueue Job/Workload for
        this cron to read against, unlike :meth:`KueueBackend.reconcile`. That made this method a
        documented no-op, on the premise the callback path always eventually fires. It does not: a dead
        fileserver agent host mid-rsync, or an enqueue failure in
        :func:`flush_pending_push_file_enqueues` (its own docstring names the gap), leaves the row
        SUBMITTED forever with no callback ever coming -- permanently consuming a lane cap slot, the
        same "N/N busy with zero real workloads" failure :meth:`KueueBackend._reap_stranded_staging`
        (phaze-ul2v) fixed for the staging half. :meth:`_reap_stranded_submitted` is that mechanism's
        compute twin.
        """
        tally = {"submitted_reaped": 0}
        await self._reap_stranded_submitted(session, tally)
        return tally

    async def _reap_stranded_submitted(self, session: AsyncSession, tally: dict[str, int]) -> None:
        """Spill THIS backend's age-stranded SUBMITTED ``cloud_job`` rows back to awaiting (phaze-j7m18).

        Mirrors :meth:`KueueBackend._reap_stranded_staging` exactly, narrowed to compute's single
        in-flight status and its owning callback:

        0. **The broker-liveness gate, checked FIRST.** Skip any row whose ``push_file:<file_id>``
           broker key is still ``queued``/``active`` in ``saq_jobs`` (:func:`get_live_job_keys`, the
           same probe recovery uses). A live key means the ``push_file`` SAQ job (and its eventual
           ``/pushed``/``/mismatch``/``/failed`` callback) still owns the row regardless of age -- a
           multi-GB rsync over a slow link legitimately runs for hours and bumps no timestamp while it
           transfers, so age alone cannot distinguish a live push from a lost one.
        1. **The age bound** (:attr:`ControlSettings.cloud_submitted_stale_after_sec`). A row is a
           candidate only once ``now - updated_at`` exceeds it. The coarse backstop for when even the
           broker row is gone (a lost/swept job, or an enqueue that never landed one at all).
        2. **The CAS.** The spill goes through the single awaiting writer (:func:`hold_awaiting_cloud`)
           in SPILL mode with ``expect_status=('submitted',)``. A callback that lands between our read
           and our update advances the row out of SUBMITTED, the CAS matches 0 rows, and the reaper
           takes a FULL no-op -- the happy path always wins the race, by construction.

        ``clear_cloud_phase`` stays ``False``: ``cloud_phase`` is a Kueue-only field (queued_behind_quota
        / admitted / running / finished, D-05) that ``ComputeAgentBackend.dispatch`` never sets, so there
        is nothing to clear (mirrors the push-spill branches in ``routers/agent_push.py``, which also
        leave it untouched). No S3 cleanup either -- compute's ``cloud_job`` carries no S3 object
        (``s3_key`` is NULL, D-08); the only durable side effect to undo is the ``push_file:<file_id>``
        scheduling-ledger row, cleared exactly like ``report_push_failed``'s spill.

        Re-drive is bounded exactly like the staging reaper: each reap increments ``cloud_job.attempts``
        (capped at ``cloud_submit_max_attempts``), so a file that strands repeatedly reaches a spent
        budget and ``select_backend`` routes it to local instead of re-stranding on compute.

        Per-row discipline mirrors :meth:`KueueBackend._reap_stranded_staging`: the drain's
        ``pg_advisory_xact_lock`` is taken at the top of each row's unit of work, each row commits on its
        own, and a per-row ``except`` rolls back so one bad row never aborts the tick.
        """
        cfg = cast("ControlSettings", get_settings())
        now = datetime.now(UTC)
        rows = (
            (
                await session.execute(
                    select(CloudJob).where(
                        CloudJob.status == CloudJobStatus.SUBMITTED.value,
                        CloudJob.backend_id == self.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        # Capture primitive ids only: the per-row rollback expires the ORM identity map, so every row is
        # re-read fresh INSIDE the loop against the fresh state (mirrors the staging reaper).
        cloud_job_ids = [row.id for row in rows]

        # phaze-vu88k.2: batch-prefetch every candidate row's FileRecord in ONE query, keyed by the
        # file_id captured from THIS snapshot. Unlike ``cloud_job`` above, this is safe to prefetch
        # rather than re-read per row: ``file_id`` is an immutable FK on ``cloud_job`` (never
        # re-pointed after creation) and ``hold_awaiting_cloud`` reads nothing off ``file`` except
        # ``file.id`` (verified against its body) -- so this carries none of the per-row freshness
        # requirement the ``cloud_job`` re-read exists for. Replaces N per-row
        # ``SELECT FileRecord WHERE id = :file_id`` calls with 1 (0 if the sweep found nothing).
        file_ids = {row.file_id for row in rows}
        files_by_id: dict[uuid.UUID, FileRecord] = (
            {file.id: file for file in (await session.execute(select(FileRecord).where(FileRecord.id.in_(file_ids)))).scalars().all()}
            if file_ids
            else {}
        )

        # phaze-j7m18 (mirrors phaze-31q3): snapshot the live-broker key set ONCE per sweep (degrade-safe
        # -- an empty set on any read failure falls the reaper back to age-only, never raising).
        live_keys = await get_live_job_keys(session)

        for cloud_job_id in cloud_job_ids:
            try:
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
                # phaze-7lpb discipline: force a real re-read under the lock rather than the sweep-start
                # identity-mapped object (the sessionmaker is expire_on_commit=False).
                cloud_job = await session.get(CloudJob, cloud_job_id, populate_existing=True)
                if cloud_job is None or cloud_job.status != CloudJobStatus.SUBMITTED.value:
                    # A callback terminalized/advanced it since the snapshot -- nothing to reap.
                    await session.rollback()
                    continue
                file_id = cloud_job.file_id
                live_key = f"push_file:{file_id}"
                if live_key in live_keys:
                    # A queued/active push_file job (and its eventual callback) still owns this row.
                    await session.rollback()
                    continue
                # ``updated_at`` is TIMESTAMP WITHOUT TIME ZONE, so asyncpg hands it back NAIVE in
                # production; assume-UTC before subtracting (mirrors the staging reaper's coercion).
                ref = cloud_job.updated_at or cloud_job.created_at
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=UTC)
                age_sec = (now - ref).total_seconds()
                if age_sec < cfg.cloud_submitted_stale_after_sec:
                    # YOUNGER THAN THE BOUND: the callback owns this row. Never fire here.
                    await session.rollback()
                    continue
                # Bounded re-drive: each reap spends one attempt; at the cap select_backend routes local.
                attempts = min(cloud_job.attempts + 1, cfg.cloud_submit_max_attempts)
                # phaze-vu88k.2: dict lookup against the batch prefetch above, not a per-row SELECT.
                file = files_by_id.get(file_id)
                spilled = file is not None and await hold_awaiting_cloud(
                    session,
                    file,
                    attempts=attempts,
                    expect_status=(CloudJobStatus.SUBMITTED.value,),
                )
                if not spilled:
                    # Lost the race to a live callback (or the FK file vanished): FULL no-op.
                    await session.rollback()
                    continue
                await clear_ledger_entry(session, f"push_file:{file_id}")
                await session.commit()
                tally["submitted_reaped"] += 1
                logger.warning(
                    "ComputeAgentBackend.reconcile: stranded SUBMITTED cloud_job reaped -> spilled back to awaiting (lost agent callback)",
                    cloud_job_id=str(cloud_job_id),
                    file_id=str(file_id),
                    backend_id=self.id,
                    age_sec=int(age_sec),
                    bound_sec=cfg.cloud_submitted_stale_after_sec,
                    attempts=attempts,
                )
            except Exception:
                # Per-row guard (reaper loop, mirrors KueueBackend._reap_stranded_staging's per-row except):
                # one bad row's unexpected failure must never abort the whole sweep, so this stays broad
                # by design.
                await session.rollback()
                logger.warning(
                    "ComputeAgentBackend.reconcile: stranded SUBMITTED reap failed; continuing", cloud_job_id=str(cloud_job_id), exc_info=True
                )
