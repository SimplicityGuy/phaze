"""``KueueBackend`` -- the S3-staging + k8s-Job lane, its Job/Workload reconcile, and its staging reaper.

Extracted from the former single-module ``services/backends.py`` (phaze-dr9df). Every body is
verbatim except for ONE mechanical extract-method, which is the whole reason this file could meet the
package's nesting budget:

:meth:`KueueBackend._reap_stranded_staging`'s **post-commit S3 cleanup tail** now lives in
:meth:`KueueBackend._cleanup_reaped_staging_object`. That block (phaze-jwz0 / phaze-wa9x /
phaze-a6un6) was the single deepest thing in the whole 1,543-NLOC module -- ``for`` -> ``try`` ->
``if bucket is not None`` -> ``try`` -> ``if upload_id``. It is also the most obviously separable:
it runs strictly AFTER ``session.commit()`` has already made the spill durable, it takes only
primitives the loop has already read into locals, and it returns nothing. Semantics are unchanged in
both directions:

* it is still CALLED from inside the per-row ``try``, so a raise out of ``resolve_bucket_config``
  (which sat OUTSIDE the inner ``try`` before, and still sits outside it now) still lands in the
  per-row ``except`` -> rollback + "stranded staging reap failed" warning; and
* the inner ``try``/``except Exception`` that swallows S3 failures, and the ``try``/``finally``
  around the pre-delete generation probe, moved with the block unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, text, update
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services import kube_staging, s3_staging
from phaze.services.backends.admission import hold_awaiting_cloud
from phaze.services.backends.base import STAGING, _BaseBackend
from phaze.services.cloud_staging import _stage_file_to_s3
from phaze.services.pipeline import get_live_job_keys
from phaze.services.scheduling_ledger import clear_ledger_entry
from phaze.tasks.reconcile_cloud_jobs import _reconcile_one
from phaze.tasks.release_awaiting_cloud import _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.config_backends import BucketConfig, KubeConfig
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)


def _seconds_since_last_staging_write(cloud_job: CloudJob, now: datetime) -> float:
    """How long ago the live path last wrote this staging row -- the age the reaper's bound is spent against.

    ``updated_at`` moves on EVERY live-path write (the initial stage stamp, and ``redrive_upload``'s
    re-stage -- phaze-2hv9), so this is "silence since the last thing that touched the row", not "age
    since creation"; ``created_at`` stands in only for a row that has never been updated.

    ``updated_at`` is TIMESTAMP WITHOUT TIME ZONE, so asyncpg hands it back NAIVE in production;
    assume-UTC before subtracting (a bare naive-minus-aware raises TypeError and would abort the whole
    sweep -- mirrors reenqueue.py's CR-02 coercion).
    """
    ref = cloud_job.updated_at or cloud_job.created_at
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return (now - ref).total_seconds()


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

        phaze-grzo (supersedes phaze-uciu.3): ``_stage_file_to_s3`` no longer fires the ``s3_upload``
        enqueue inline -- it PARKS it on the session and the drain fires it via
        ``flush_pending_s3_enqueues`` AFTER the single post-loop commit. SAQ's ``PostgresQueue`` commits
        the job on its OWN psycopg pool immediately, so an inline enqueue made the job (and its
        ``report_uploaded`` callback) worker-visible BEFORE this asyncpg session committed the UPLOADING
        row -- a fast callback then saw no UPLOADING row and no-op'd, stranding the file. Parking the
        enqueue removes it from the transaction entirely, so this ``dispatch`` no longer raises on an
        enqueue failure (there is no SAVEPOINT to roll back); the drain drops parked enqueues on a tick
        rollback and a post-commit flush failure leaves the committed UPLOADING row for the age-bounded
        ``_reap_stranded_staging`` reaper (phaze-ul2v).
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

    def _staging_stale_bound_sec(self, cfg: ControlSettings, status: str) -> int:
        """Return the staleness bound (seconds) for a staging ``status`` -- the reaper's never-fire-before line."""
        if status == CloudJobStatus.UPLOADING.value:
            return cfg.cloud_uploading_stale_after_sec
        return cfg.cloud_uploaded_stale_after_sec

    async def _reap_stranded_staging(self, session: AsyncSession, tally: dict[str, int]) -> None:
        """Spill THIS backend's AGE-STRANDED {UPLOADING, UPLOADED} ``cloud_job`` rows back to awaiting (phaze-ul2v).

        The missing safety net. ``in_flight_count`` counts {UPLOADING, UPLOADED, SUBMITTED, RUNNING}
        (D-10), so a staging row holds a burst-lane cap slot -- but :meth:`reconcile` only ever selected
        {SUBMITTED, RUNNING}. The staging half is terminalized SOLELY by the two agent HTTP callbacks
        (``report_uploaded`` flips UPLOADING -> UPLOADED + enqueues the submit; ``report_upload_failed``
        re-drives or spills at the cap). When the fileserver agent dies mid-upload, or the ``s3_upload``
        SAQ job is lost after the row was stamped, NEITHER callback ever fires: the row sits UPLOADING
        forever, reconcile scopes away from it, orphan recovery excludes it (it IS in-flight), and the
        slot leaks. Enough leaks and the lane wedges at "N/N in flight" with zero real workloads.

        **The callback path stays PRIMARY -- this only catches LOST callbacks.** Three mechanisms enforce
        that, and all are load-bearing:

        0. **The broker-liveness gate (phaze-31q3, status-keyed since phaze-1k0i).** BEFORE the age bound,
           skip any row whose CURRENT-status-owning broker key is still ``queued``/``active`` in
           ``saq_jobs`` (the same :func:`get_live_job_keys` probe recovery uses). The owning key depends on
           ``observed_status``: an UPLOADING row is owned by ``s3_upload:<file_id>`` (``report_uploaded``'s
           callback flips it to UPLOADED once that job runs); an UPLOADED row is owned by
           ``submit_cloud_job:<file_id>`` -- ``report_uploaded`` enqueues that job in the SAME transaction as
           the UPLOADED flip and the completed ``s3_upload`` job's key is swept from ``saq_jobs``, so checking
           ONLY ``s3_upload:`` (the phaze-31q3 shape) leaves the UPLOADED half of :data:`STAGING` with no live
           check at all: a ``submit_cloud_job`` queued behind a controller-queue backlog (or ``active`` against
           a hung kube API -- kr8s sets no client-side timeout, see ``kube_staging.submit_job``) reads
           UPLOADED with an ``updated_at`` frozen at the flip and gets reaped out from under the live submit,
           deleting the fully staged object it still owns. The age bound alone CANNOT tell a lost callback
           from live work: ``updated_at`` bumps only at dispatch/re-stage, not mid-transfer, and NOT while a
           job waits in a queue backlog -- so a multi-GB upload that legitimately transfers past the bound,
           or a still-queued job whose SAQ clock has not even started, genuinely reads its status with an old
           timestamp and would be reaped mid-flight. A live broker key means the job (and its callback) still
           owns the row regardless of age; never reap it.
        1. **The age bound.** A row is a candidate only when ``now - updated_at`` exceeds its per-status
           bound (:meth:`_staging_stale_bound_sec`). ``updated_at`` moves on EVERY live-path write (the
           initial stage stamp, and ``redrive_upload``'s re-stage -- phaze-2hv9), so an actively-progressing
           burst keeps resetting the clock. It is a coarse backstop for the case where even the broker row is
           gone (a lost/swept job), the liveness gate above being the precise live-vs-lost discriminator.
        2. **The CAS.** The spill goes through the single awaiting writer
           (:func:`hold_awaiting_cloud`) in SPILL mode with ``expect_status`` pinned to the status this
           reaper actually OBSERVED. A callback that lands between our read and our update advances the
           row out of that status, the CAS matches 0 rows, and the reaper takes a FULL no-op (no S3
           cleanup, no ledger clear) -- the happy path always wins the race, by construction.

        The spill mirrors ``report_upload_failed``'s over-cap branch exactly: re-stamp to
        ``status='awaiting'`` (OUT of :data:`IN_FLIGHT`, so the cap slot is released the moment this
        commits), ``clear_cloud_phase=True`` (off the "Running" tile, D-12), abort the multipart + delete
        the staged object so no orphaned upload/object survives (KSTAGE-04), and clear the
        ``s3_upload:<file_id>`` ledger row so the re-drive is not shadowed by a stale entry.

        Re-drive is BOUNDED: each reap increments ``cloud_job.attempts`` (capped at
        ``cloud_submit_max_attempts``). A file that strands repeatedly therefore reaches a SPENT budget,
        at which point ``select_backend`` routes it to local (D-03) instead of stranding on cloud again --
        so this never becomes an infinite cloud re-drive loop.

        Per-row discipline is identical to :meth:`reconcile`: the drain's ``pg_advisory_xact_lock`` is
        taken at the top of each row's unit of work, each row commits on its own, and a per-row
        ``except`` rolls back so one bad row never aborts the tick. Every skip path rolls back too, which
        releases the xact lock rather than holding it across the whole sweep.
        """
        cfg = cast("ControlSettings", get_settings())
        now = datetime.now(UTC)
        rows = (
            (
                await session.execute(
                    select(CloudJob).where(
                        CloudJob.status.in_([status.value for status in STAGING]),
                        CloudJob.backend_id == self.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        # Capture primitive ids only: the per-row rollback expires the ORM identity map (same reason
        # reconcile re-fetches), so every row is re-read fresh INSIDE the loop and the age/CAS decision
        # is made against that fresh read, never a stale snapshot.
        cloud_job_ids = [row.id for row in rows]

        # phaze-vu88k.2: batch-prefetch every candidate row's FileRecord in ONE query, keyed by the
        # file_id captured from THIS snapshot. Unlike ``cloud_job`` above, this is safe to prefetch
        # rather than re-read per row: ``file_id`` is an immutable FK on ``cloud_job`` (never
        # re-pointed after creation) and ``hold_awaiting_cloud`` reads nothing off ``file`` except
        # ``file.id`` (verified against its body) -- so this carries none of the per-row freshness
        # requirement the ``cloud_job`` re-read exists for. Replaces N per-row
        # ``SELECT FileRecord WHERE id = :file_id`` calls with 1 (0 if the sweep found nothing).
        file_ids = {row.file_id for row in rows}
        files_by_id: dict[Any, FileRecord] = (
            {file.id: file for file in (await session.execute(select(FileRecord).where(FileRecord.id.in_(file_ids)))).scalars().all()}
            if file_ids
            else {}
        )

        # phaze-31q3: snapshot the live-broker key set ONCE per sweep (degrade-safe: an empty set on any
        # read failure falls the reaper back to age-only, never raising). A row whose ``s3_upload:<file_id>``
        # key is queued/active is live work the callback path still owns -- reaping it would abort a live
        # multipart mid-transfer AND leave the surviving job to shadow the re-drive enqueue.
        live_keys = await get_live_job_keys(session)

        for cloud_job_id in cloud_job_ids:
            try:
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
                # phaze-7lpb: ``populate_existing=True`` forces a real SELECT under the lock. The snapshot
                # ``select`` above populated the identity map, and the sessionmaker is ``expire_on_commit=False``
                # (database.py), so a plain ``session.get`` for the first row -- and every row after a per-row
                # ``commit`` -- would return the UNEXPIRED cached object WITHOUT emitting SQL, evaluating the
                # age/CAS decision against sweep-start-stale state. A ``report_upload_failed`` -> redrive that
                # re-stages the row back into the SAME status (fresh upload_id + fresh updated_at, phaze-2hv9)
                # would then be judged on the OLD timestamp and reaped live. Re-read fresh at lock acquisition.
                cloud_job = await session.get(CloudJob, cloud_job_id, populate_existing=True)
                if cloud_job is None or cloud_job.status not in {status.value for status in STAGING}:
                    # A callback terminalized/advanced it since the snapshot -- nothing to reap.
                    await session.rollback()
                    continue
                observed_status = cloud_job.status
                # phaze-31q3/phaze-1k0i: broker-liveness gate BEFORE the age bound, status-keyed. An
                # UPLOADING row is owned by ``s3_upload:<file_id>``; an UPLOADED row's ``s3_upload`` job has
                # already completed (its key is swept from saq_jobs) and ownership has moved to
                # ``submit_cloud_job:<file_id>`` -- checking the UPLOADING key for an UPLOADED row would
                # always miss and reap a row a queued/active submit still owns. Either live key means the
                # callback path still owns this row -- never reap it, no matter how old ``updated_at`` looks
                # (a legitimately hours-long transfer, or a job still waiting in a queue backlog, bumps no
                # timestamp). Skip via rollback (releasing the xact lock), exactly like the young-row skip.
                live_key = (
                    f"s3_upload:{cloud_job.file_id}" if observed_status == CloudJobStatus.UPLOADING.value else f"submit_cloud_job:{cloud_job.file_id}"
                )
                if live_key in live_keys:
                    await session.rollback()
                    continue
                age_sec = _seconds_since_last_staging_write(cloud_job, now)
                bound_sec = self._staging_stale_bound_sec(cfg, observed_status)
                if age_sec < bound_sec:
                    # YOUNGER THAN THE BOUND: the callback owns this row. Never fire here.
                    await session.rollback()
                    continue
                file_id = cloud_job.file_id
                upload_id = cloud_job.upload_id
                staging_bucket = cloud_job.staging_bucket
                # Bounded re-drive: each reap spends one attempt; at the cap select_backend routes local.
                attempts = min(cloud_job.attempts + 1, cfg.cloud_submit_max_attempts)
                # phaze-vu88k.2: dict lookup against the batch prefetch above, not a per-row SELECT.
                file = files_by_id.get(file_id)
                spilled = file is not None and await hold_awaiting_cloud(
                    session,
                    file,
                    attempts=attempts,
                    expect_status=(observed_status,),
                    # phaze-wnp51: pin the spill to the generation we actually observed. ``status`` alone
                    # is not a generation identifier -- redrive_upload can re-stage the row back into the
                    # SAME status with a fresh upload_id between our read and this CAS; requiring the
                    # observed upload_id too makes that re-drive fail the CAS instead of losing its work
                    # to a reap that thinks it is spilling the OLD (dead) generation.
                    expect_upload_id=upload_id,
                    clear_cloud_phase=True,
                )
                if not spilled:
                    # Lost the race to a live callback (or the FK file vanished): FULL no-op -- no S3
                    # cleanup (a live burst may still own the object), no ledger clear.
                    await session.rollback()
                    continue
                # phaze-jwz0: CAS hit -> clear the ledger + COMMIT the spill FIRST, which releases the drain's
                # global ``pg_advisory_xact_lock`` and the idle-in-transaction connection BEFORE any S3 network
                # I/O. The prior structure held that single global lock + an open txn across
                # ``abort_multipart_upload`` / ``delete_staged_object`` -- aiobotocore calls subject to botocore's
                # full connect-timeout x retry cycle (minutes against a hung/unreachable bucket) -- so ONE bad
                # endpoint wedged every ``stage_cloud_window`` drain tick and every reconcile row behind it, and
                # ran DESTRUCTIVE cleanup before the commit (a commit failure then left the DB claiming an upload
                # whose S3 substrate was already gone).
                await clear_ledger_entry(session, f"s3_upload:{file_id}")
                if observed_status == CloudJobStatus.UPLOADED.value:
                    # phaze-2iizn: an UPLOADED row is owned by submit_cloud_job:<file_id> (phaze-1k0i's
                    # status-keyed liveness gate above), not s3_upload:<file_id> -- the s3_upload job
                    # already completed and swept its own broker key by the time the row reached
                    # UPLOADED. The lost job here is submit_cloud_job's, and its before_enqueue-written
                    # ledger row survives this reap untouched unless cleared too: recover_orphaned_work
                    # would otherwise replay it against an already-spilled/terminal file and guarantee-fail
                    # with KubeStagingError. Clearing s3_upload:<file_id> stays -- it is unconditionally
                    # load-bearing (the control side never clears it on the success path) -- this ADDS the
                    # second key rather than swapping it.
                    await clear_ledger_entry(session, f"submit_cloud_job:{file_id}")
                await session.commit()
                tally["staging_reaped"] += 1
                logger.warning(
                    "KueueBackend.reconcile: stranded staging cloud_job reaped -> spilled back to awaiting (lost agent callback)",
                    cloud_job_id=str(cloud_job_id),
                    file_id=str(file_id),
                    backend_id=self.id,
                    observed_status=observed_status,
                    age_sec=int(age_sec),
                    bound_sec=bound_sec,
                    attempts=attempts,
                )
                # phaze-jwz0: S3 cleanup runs AFTER the commit, OUTSIDE the txn/lock. Both ops are idempotent
                # (they swallow NoSuchUpload / absent-object) and the row is already 'awaiting' -> a re-drive
                # re-stages a FRESH multipart, so the old object is irrelevant: a crash or a hung bucket here at
                # worst leaks the OLD object until the next reap/spill re-runs the same idempotent cleanup.
                # Failures are isolated locally so a slow bucket can neither hold the released lock nor turn a
                # durable spill into a per-row rollback that undoes it.
                # ``resolve_bucket_config`` stays OUTSIDE ``_cleanup_reaped_staging_object`` (bead phaze-bk9el.2):
                # it is pure/no-I/O, so a raise here still lands in THIS per-row ``except`` below, exactly as
                # before the extraction.
                bucket = s3_staging.resolve_bucket_config(cfg, staging_bucket)
                if bucket is not None:
                    await self._cleanup_reaped_staging_object(session, bucket, cloud_job_id, file_id, upload_id)
            except Exception:
                # Per-row guard (reaper loop, mirrors reconcile()'s per-row except below): one bad row's
                # unexpected failure must never abort the whole sweep, so this stays broad by design.
                await session.rollback()
                logger.warning("KueueBackend.reconcile: stranded staging reap failed; continuing", cloud_job_id=str(cloud_job_id), exc_info=True)

    async def _cleanup_reaped_staging_object(
        self,
        session: AsyncSession,
        bucket: BucketConfig,
        cloud_job_id: uuid.UUID,
        file_id: uuid.UUID,
        upload_id: str | None,
    ) -> None:
        """Post-commit S3 cleanup tail of :meth:`_reap_stranded_staging`, extracted verbatim (bead phaze-bk9el.2).

        Runs strictly AFTER the caller's ``session.commit()`` has already made the spill durable (phaze-jwz0):
        both ops below are idempotent (they swallow ``NoSuchUpload`` / absent-object) and the row is already
        'awaiting', so a crash or a hung bucket here at worst leaks the OLD object until the next reap/spill
        re-runs the same idempotent cleanup. Failures are isolated locally (the ``except`` below) so a slow
        bucket can neither hold the already-released advisory lock nor turn a durable spill into a per-row
        rollback that undoes it -- this method returns nothing and never raises.

        ``bucket``/``cloud_job_id``/``file_id``/``upload_id`` are all primitives the caller already read into
        locals from ITS fresh, lock-held row read -- this method re-derives nothing and takes no further lock.

        phaze-wa9x: re-reads the row IMMEDIATELY before the delete, outside the advisory lock the caller
        already released. ``delete_staged_object`` is keyed only by ``file_id``, and the bucket/key are
        identical for every staging generation of this file -- once the row is 'awaiting', a concurrent drain
        tick can re-dispatch it and stage a FRESH object at the SAME key while this delete is still in flight
        (a stalled-but-eventually-successful S3 DELETE). A new cycle always re-upserts status back to
        UPLOADING with a FRESH ``upload_id`` (``_stage_file_to_s3``), so a row still 'awaiting' with the SAME
        ``upload_id`` we observed proves no new cycle has claimed the key -- only then is the delete safe.

        phaze-a6un6: the pre-delete probe's rollback is in a ``finally``, not after the ``SELECT`` in the
        ``try`` body -- if the ``SELECT`` itself raises (transient DB error), the old placement skipped that
        rollback entirely and the ``except`` below only logged, leaving the session in an aborted/pending
        transaction. The NEXT row's advisory-lock acquire then raised ``PendingRollbackError`` and THAT
        healthy row's reap was skipped for the whole tick, misleadingly blamed instead of this probe.
        """
        try:
            if upload_id:
                await s3_staging.abort_multipart_upload(file_id, upload_id, bucket)
            current = None
            try:
                current = (await session.execute(select(CloudJob.status, CloudJob.upload_id).where(CloudJob.id == cloud_job_id))).one_or_none()
            finally:
                await session.rollback()  # read-only probe; release its implicit tx either way
            if current is not None and current.status == CloudJobStatus.AWAITING.value and current.upload_id == upload_id:
                await s3_staging.delete_staged_object(file_id, bucket)
        except Exception:
            # Broad by design: aiobotocore/botocore can raise a wide range of transport/service errors here,
            # and (per the docstring above) a cleanup failure must never propagate to abort the already-
            # committed spill -- it can only be logged and left for the next idempotent re-run.
            logger.warning(
                "KueueBackend.reconcile: post-commit S3 cleanup of a reaped staging row failed "
                "(row already spilled to awaiting; old object may leak until the re-drive re-stages)",
                cloud_job_id=str(cloud_job_id),
                file_id=str(file_id),
                exc_info=True,
            )

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

        phaze-ul2v: the tick now ALSO runs :meth:`_reap_stranded_staging` first, covering the pre-submit
        {UPLOADING, UPLOADED} half of :data:`IN_FLIGHT` that this Kueue read scopes away from. Those rows
        hold cap slots but have no Kueue object to read, so they are aged out instead. The extra
        ``staging_reaped`` tally key aggregates in the cron via its ``tally.get(key, 0) + value`` fold.
        """
        cfg = cast("ControlSettings", get_settings())
        cap = cfg.cloud_submit_max_attempts
        tally = {"reconciled": 0, "succeeded": 0, "failed": 0, "redriven": 0, "inadmissible": 0, "pending": 0, "running": 0, "staging_reaped": 0}
        reconcile_ctx = ctx if ctx is not None else {}

        # phaze-ul2v: FIRST sweep the pre-submit half ({UPLOADING, UPLOADED}) that the Kueue read below
        # deliberately does not cover. Independent of the Job/Workload read -- a stranded staging row has
        # no Kueue object to reconcile against, only an age bound.
        await self._reap_stranded_staging(session, tally)

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
                # phaze-7lpb: ``populate_existing=True`` forces a real SELECT under the lock (the snapshot
                # ``select`` above populated the identity map and the sessionmaker is ``expire_on_commit=False``,
                # so a plain ``get`` would hand back the sweep-start-stale cached row for the first row and every
                # post-commit row -- and the None-check for a vanished row could never fire on a cached object).
                cloud_job = await session.get(CloudJob, cloud_job_id, populate_existing=True)
                if cloud_job is None:
                    # phaze-c1u7: a concurrent delete_scan cascade (services/scan_deletion.py) can remove
                    # this row between the snapshot above and this fresh read. Mirror
                    # _reap_stranded_staging's identical vanished-row skip (line ~701): roll back to
                    # release the pg_advisory_xact_lock taken above, exactly like every other skip path in
                    # this loop -- a bare ``continue`` leaves the lock held across every subsequent row's
                    # kube I/O (and the rest of the tick, if this was the last row) instead of releasing it
                    # per-row as the design (SCHED-02 / Pitfall 2) relies on.
                    await session.rollback()
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
