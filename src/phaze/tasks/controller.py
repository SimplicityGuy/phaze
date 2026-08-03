"""SAQ controller settings -- entry point for ``saq phaze.tasks.controller.settings`` (Phase 26 D-01..D-04).

Control role: runs the application server's SAQ worker pool. Fileless tasks only, e.g.:
- generate_proposals (LLM-driven rename suggestions)
- match_tracklist_to_discogs (Discogsography HTTP API)
- drain_tracklists + tracklist_drain_status (the 1001Tracklists drain -- operator-initiated, NO cron)
- refresh_tracklists (operator-initiated re-arm of the drain for specific pages -- NO cron)
- reap_stalled_scans, recover_orphaned_work, stage_cloud_window, submit_cloud_job,
  reconcile_cloud_jobs (added in later phases -- see the ``settings`` dict below for the
  authoritative, current ``functions`` / ``cron_jobs`` list)

This module does NOT import `phaze.tasks.pool` (that belongs to the agent role per Phase 26 D-03). Cross-imports between
controller and agent_worker are forbidden -- the import-boundary test in
Plan 10 enforces the symmetric invariant for agent_worker.

Docker invocation (set by Plan 13's docker-compose.yml update):
    services:
      worker:
        command: uv run saq phaze.tasks.controller.settings
        environment:
          PHAZE_ROLE: control
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import redis.asyncio as redis_async
from saq import CronJob
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
import structlog

from phaze.config import export_llm_api_keys, get_settings
from phaze.logging_config import configure_logging
from phaze.services import kube_staging, s3_staging
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.discogs_matcher import DiscogsographyClient
from phaze.services.proposal import ProposalService, load_prompt_template
from phaze.tasks._shared.deterministic_key import increment_completed
from phaze.tasks._shared.queue_factory import build_pipeline_queue
from phaze.tasks.aborting_reaper import reap_stuck_aborting_jobs
from phaze.tasks.active_reaper import reap_stranded_active_jobs
from phaze.tasks.discogs import match_tracklist_to_discogs
from phaze.tasks.ledger_reaper import reap_resolved_ledger_rows
from phaze.tasks.proposal import generate_proposals
from phaze.tasks.reconcile_cloud_jobs import reconcile_cloud_jobs
from phaze.tasks.reenqueue import backfill_ledger_from_saq_jobs, recover_orphaned_work
from phaze.tasks.release_awaiting_cloud import stage_cloud_window
from phaze.tasks.scan_reaper import reap_stalled_scans
from phaze.tasks.stage_park_reconcile import reconcile_stale_stage_parks
from phaze.tasks.submit_cloud_job import submit_cloud_job
from phaze.tasks.tracklist import refresh_tracklists
from phaze.tasks.tracklist_drain import drain_tracklists, tracklist_drain_status


if TYPE_CHECKING:
    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize shared resources for fileless tasks (SAQ startup hook).

    Does NOT initialize: process pool, models check.
    Those belong to the agent role; the control role's worker never reads files.
    """
    cfg = get_settings()

    # PR3 observability: the control worker is its OWN OS process; configure the
    # central structlog pipeline here BEFORE the first log so its lines render
    # through the same JSON/console pipeline as the api and agent worker.
    configure_logging(level=cfg.log_level, json_logs=cfg.log_json)

    # Bug A (June 2026): litellm reads provider creds from os.environ, never from
    # ControlSettings. The LLM keys arrive via the <VAR>_FILE secret convention as
    # SecretStr fields, so bridge them into ANTHROPIC_API_KEY / OPENAI_API_KEY here --
    # otherwise every generate_proposals acompletion() raises AuthenticationError.
    export_llm_api_keys(anthropic_api_key=cfg.anthropic_api_key, openai_api_key=cfg.openai_api_key)  # type: ignore[attr-defined]

    # D-13 token-preview discipline: never log the full broker/cache DSN (either may carry
    # credentials -- queue_url is a SECRET_FILE_FIELDS member). Report the backend + queue name only.
    logger.info("phaze.controller startup role=control queue=controller backend=postgres")

    # Phase 67 (REG-04): log the resolved backend registry ONCE at boot. The projection is
    # secret-free by construction (id/kind/rank/cap only -- never a SecretStr, kube DSN, or SA
    # token; Plan 02 owns the projection, Pitfall 5). This is the operator's boot-time visibility
    # into which backends are active (implicit-local vs. a configured compute/kueue registry).
    cfg.log_effective_registry()  # type: ignore[attr-defined]

    # Shared async engine pool for all fileless task functions (INFRA-01 from v1.0).
    # quick-260707-ryn: source every pool kwarg from config (cfg == get_settings(), which
    # inherits the BaseSettings db_* knobs). pool_size drops from a hardcoded 10 to the config
    # default 5 and the three hygiene kwargs (pool_timeout / pool_recycle / pool_pre_ping) are
    # NEW. INCIDENT: phaze reaches Postgres through PgBouncer in SESSION mode, where every
    # client connection pins one upstream server connection for its whole lifetime; the shared
    # (phaze,phaze) session pool (cap ~55) deadlocked under normal multi-worker load and /health
    # hung behind the exhausted pool. pool_pre_ping drops dead server conns before checkout,
    # pool_recycle=1800 frees an idle server slot after 30 min instead of pinning it, and
    # pool_timeout=10 bounds the acquire wait so a saturated pool fails fast. Homelab raises the
    # pooler cap to ~80 in parallel, so these app-side reductions are HEADROOM, not a hard fit.
    task_engine = create_async_engine(
        str(cfg.database_url),
        echo=cfg.debug,
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
        pool_timeout=cfg.db_pool_timeout,
        pool_recycle=cfg.db_pool_recycle,
        pool_pre_ping=cfg.db_pool_pre_ping,
    )
    ctx["async_session"] = async_sessionmaker(task_engine, class_=AsyncSession, expire_on_commit=False)
    ctx["task_engine"] = task_engine

    # Phase 19: Discogsography client for Discogs release matching
    ctx["discogs_client"] = DiscogsographyClient(base_url=cfg.discogsography_url)

    # Phase 6: AI proposal generation. We read llm_model / llm_max_rpm via
    # ControlSettings -- safe because PHAZE_ROLE=control ensures get_settings()
    # returns ControlSettings (Plan 01 invariant). If a future caller boots
    # controller.settings under PHAZE_ROLE=agent, the AttributeError below
    # surfaces immediately at startup -- correct fail-fast behavior.
    prompt_template = load_prompt_template()
    ctx["proposal_service"] = ProposalService(
        model=cfg.llm_model,  # type: ignore[attr-defined]
        prompt_template=prompt_template,
        max_rpm=cfg.llm_max_rpm,  # type: ignore[attr-defined]
    )

    # Phase 36: the broker is Postgres now, so `ctx["queue"]` (the PostgresQueue) no longer
    # carries a Redis cache client. Stash a DEDICATED cache-redis handle so the cache-plane
    # readers (generate_proposals rate-limit) read `ctx["redis"]`, NEVER `ctx["queue"].redis`
    # (PostgresQueue has no `.redis`). Mirrors the discogs_client create/close lifecycle:
    # created here, closed in shutdown.
    ctx["redis"] = redis_async.Redis.from_url(cfg.redis_url)

    # The module-level PostgresQueue is still stashed for readers that enqueue follow-on work.
    ctx["queue"] = queue

    # Phase 45 (L-01/L-02): attach the control-side scheduling-ledger sessionmaker to BOTH the
    # module-level controller queue AND every per-agent router queue, so the before_enqueue WRITE
    # hook records each control-side enqueue and the after_process hook clears controller-stage
    # rows on terminal status. The module-level queue is constructed at import time BEFORE the
    # engine exists, so the handle is attached HERE (once the engine + sessionmaker are built).
    # ``ctx["async_session"]`` is the control-side sessionmaker bound to ``task_engine``.
    queue.ledger_sessionmaker = ctx["async_session"]  # type: ignore[attr-defined]

    # Phase 32: per-agent task router for reboot re-enqueue routing. Built ONCE
    # here and reused for the boot-time call + every cron tick (RESEARCH Pitfall 4 --
    # never construct a fresh AgentTaskRouter per call, it would leak pools).
    # Mirrors the discogs_client create/close lifecycle: created in startup, closed
    # in shutdown. Phase 36: takes (queue_url, cache_redis_url) -- Postgres broker + Redis cache.
    # Phase 45: pass the ledger sessionmaker so each per-agent queue the router builds attaches it
    # (the agent-routed recovery/startup enqueues record their ledger rows control-side).
    ctx["task_router"] = AgentTaskRouter(cfg.queue_url, cfg.redis_url, ledger_sessionmaker=ctx["async_session"])

    # Phase 42 DURABILITY REFRAME (D-01/D-02 -- DO NOT "restore" a steady-state re-enqueue cron):
    # Phase 36 moved the SAQ broker from Redis to Postgres (``saq_jobs`` table). Queued/active jobs
    # are now DURABLE across a controller restart -- SAQ re-dequeues the surviving rows itself, so a
    # normal reboot loses NOTHING. The old every-5-min ``reenqueue_discovered`` auto-advance cron and
    # its "Redis is empty after a reboot" premise are therefore OBSOLETE and were removed in Plan
    # 42-02 (steady state now produces ZERO automatic enqueues). The ONLY automatic enqueue is this
    # single gated boot recovery: ``recover_orphaned_work`` runs its ``count_inflight_jobs`` loss
    # detector and no-ops on a durable restart, reconciling ALL stages only on a genuine queue-loss
    # (truncate / restore-from-backup / fresh migration). The manual DAG "Recover" button calls the
    # SAME producer (force=True), so the two paths cannot drift. Boot resilience is non-negotiable: a
    # recovery failure must NEVER abort controller boot (RESEARCH Pitfall 3) -- broad try/except.
    # Phase 45 Plan 04 (L-04/L-05, locked decision #3): ONE-TIME idempotent startup ledger backfill,
    # run BEFORE recovery so the in-flight cohort already in saq_jobs (and any residual incident jobs)
    # is recoverable on first boot -- no blind window between the 022 migration landing and the
    # before_enqueue WRITE hook populating the ledger. This is a CONTROL-SIDE runtime reconcile, NOT
    # an Alembic data step (Alembic must never touch saq_jobs). It is idempotent (ON CONFLICT DO
    # NOTHING) so it stays safe on every boot and becomes a cheap no-op once the transition cohort
    # drains. Wrapped in its OWN try/except so a backfill failure logs and NEVER aborts boot or blocks
    # the subsequent recovery (boot resilience, T-45-14).
    try:
        async with ctx["async_session"]() as session:
            tally = await backfill_ledger_from_saq_jobs(session)
            await session.commit()
        logger.info("phaze.controller startup ledger backfill", inserted=tally["inserted"], skipped=tally["skipped"])
    except Exception:
        logger.exception("ledger backfill on startup failed")

    try:
        result = await recover_orphaned_work(ctx)
        logger.info("phaze.controller startup recovery", detected_loss=result["detected_loss"], stages=result["stages"])
    except Exception:
        logger.exception("recover_orphaned_work on startup failed")

    # Phase 56/70 (KDEPLOY-04, MKUE-01/03, D-05/D-06): PER-CLUSTER LocalQueue-reachability probe. This is
    # a RUNTIME probe, distinct from the fail-fast kube config validators -- for EACH configured Kueue
    # backend it GETs THAT cluster's LocalQueue (threaded the backend's own KubeConfig) and writes a
    # single cross-process flag the dashboard reads. Phase 70 iterates every kueue backend (was a single
    # global probe gated on a ≤1-non-local resolved kind), so N clusters each get their own reachability
    # check; the flag is set iff ANY configured cluster is unreachable (reachable == ALL-reachable). Each
    # probe AND the Redis write is INDEPENDENTLY guarded (its own broad try/except): a transient
    # kube/mesh/Redis blip MUST NEVER abort controller boot (D-05 -- the control plane still boots
    # Postgres/Redis/UI/local-analysis). Warnings name only the config surface; they never interpolate an
    # SA token or kube DSN (T-56-LOG / T-54-07).
    control_cfg = cast("ControlSettings", cfg)
    kueue_kubes = [kube for entry in control_cfg.backends if entry.kind == "kueue" and (kube := getattr(entry, "kube", None)) is not None]
    if kueue_kubes:
        all_reachable = True
        for kube in kueue_kubes:
            try:
                await kube_staging.get_local_queue(kube)
            except Exception:
                all_reachable = False
                logger.warning(
                    "phaze.controller startup: a Kueue LocalQueue is unreachable -- check cluster connectivity "
                    "and the backend's [kube] local_queue configuration; control plane boots regardless (D-05)"
                )
        # Persist the aggregate flag in its OWN guarded step. CR-01: this is the FIRST Redis call in
        # startup (backfill/recovery above use Postgres), so a Redis-down boot would let an unguarded
        # ``.set``/``.delete`` propagate and crash the control worker -- the exact opposite of D-05.
        try:
            if all_reachable:
                await ctx["redis"].delete("phaze:k8s:localqueue_unreachable")
            else:
                await ctx["redis"].set("phaze:k8s:localqueue_unreachable", "1")
        except Exception:
            logger.warning("phaze.controller startup: could not persist LocalQueue-reachability flag; control plane boots regardless (D-05)")
    else:
        # WR-01: no kueue backend configured (all-local or compute-only). The flag lives in long-lived
        # Redis, so a documented revert (drop the kueue backend(s) from backends.toml) must clear any
        # stale flag, else the dashboard shows a perpetual false LocalQueue-unreachable alert.
        # Best-effort + guarded: a Redis blip on a non-kueue boot must not abort the control plane (D-05).
        try:
            await ctx["redis"].delete("phaze:k8s:localqueue_unreachable")
        except Exception:
            logger.warning("phaze.controller startup: could not clear stale LocalQueue-reachability flag; control plane boots regardless (D-05)")

    # phaze-cws5: wire the KSTAGE-04/D-02 lifecycle backstop into production. Every comment in the S3
    # staging pipeline (stage_file_to_s3's phaze-bbwx compensation, the reaper's post-commit cleanup,
    # report_upload_failed's terminal cleanup) names ensure_bucket_lifecycle_ttl as "the eventual
    # backstop" for a missed inline abort/delete -- but nothing ever called it, so it configured ZERO
    # buckets in production (it was vulture-whitelisted as unused). Push it once per configured bucket
    # at boot. Best-effort PER BUCKET (mirrors the LocalQueue probe above, D-05): a transient S3
    # auth/network hiccup must never abort control-plane startup -- a failure here just means this
    # boot's TTL push did not land, and the next restart retries the same idempotent upsert.
    for bucket in control_cfg.buckets:
        try:
            await s3_staging.ensure_bucket_lifecycle_ttl(bucket)
        except Exception:
            logger.warning(
                "phaze.controller startup: could not configure the staging bucket's lifecycle TTL backstop; control plane boots regardless (D-05)",
                bucket_id=bucket.id,
            )


async def shutdown(ctx: dict[str, Any]) -> None:
    """Clean up shared resources (SAQ shutdown hook)."""
    logger.info("phaze.controller shutdown")

    task_engine = ctx.get("task_engine")
    if task_engine is not None:
        await task_engine.dispose()

    discogs_client = ctx.get("discogs_client")
    if discogs_client is not None:
        await discogs_client.close()

    # Phase 36: close the dedicated cache-redis client (mirrors the discogs_client cleanup).
    cache_redis = ctx.get("redis")
    if cache_redis is not None:
        await cache_redis.aclose()

    # Phase 36 (WR-01): also close the factory-attached cache_redis on the module-level queue.
    # The counter hooks read THIS handle (getattr(job.queue, "cache_redis", ...)), and SAQ's
    # Worker.stop() -> queue.disconnect() closes only the psycopg3 pool, leaving it open.
    queue_cache_redis = getattr(queue, "cache_redis", None)
    if queue_cache_redis is not None:
        await queue_cache_redis.aclose()

    # Phase 32: close the per-agent task router (disconnects every cached
    # queue pool; idempotent). Mirrors the discogs_client cleanup above.
    task_router = ctx.get("task_router")
    if task_router is not None:
        await task_router.close()


# Module-level Queue construction. SAQ's `saq <module>.settings` CLI imports
# this module and reads `settings` as a top-level attribute (RESEARCH §A2).
# Phase 36: built via the single `build_pipeline_queue` seam -- a PostgresQueue (broker =
# queue_url) with BOTH before_enqueue hooks (apply_project_job_defaults + apply_deterministic_key)
# already registered and a decoupled `cache_redis` handle attached. Conservative pool sizing
# (2/8) for the control role keeps the per-queue psycopg3 budget under Postgres max_connections
# (RESEARCH Pitfall 4). No registration here -- the factory owns the hook chain.
queue = build_pipeline_queue("controller", get_settings().queue_url, cache_redis_url=get_settings().redis_url, min_size=2, max_size=8)


settings = {
    "queue": queue,
    # Phase 35 (D-02): bump the maintained `completed` counter on each COMPLETE outcome.
    # `after_process` is a Worker constructor kwarg (NOT a register_* call) -- it goes in
    # the settings dict the SAQ CLI hands to Worker.__init__.
    "after_process": increment_completed,
    "functions": [
        generate_proposals,
        match_tracklist_to_discogs,
        # phaze-2akf: the legacy search_tracklist / scrape_and_store_tracklist pair is GONE (its
        # detail-page selectors matched zero nodes and it had no browser to clear Turnstile with).
        # What remains of that module is refresh_tracklists, now an operator-triggered re-arm of the
        # drain for specific pages -- registered here as an enqueueable function with NO CronJob.
        refresh_tracklists,
        # phaze-fq9h.7: one BOUNDED SLICE of the resumable 1001Tracklists drain, plus its
        # request-free status read. Registered as operator-enqueueable functions with NO CronJob,
        # deliberately -- the epic's ethics bound makes the drain operator-initiated rather than a
        # blanket pipeline stage (residential IP, headful browser, a public host's published
        # crawl-delay budget). The admin UI (phaze-fq9h.8) is the intended trigger.
        drain_tracklists,
        tracklist_drain_status,
        reap_stalled_scans,
        # phaze-e57w: every-minute reaper for SAQ rows stuck in status='aborting'; deletes them to
        # release the deterministic key so the blocked file is re-queueable. Cron-only (mirrors
        # reap_stalled_scans), NOT in enqueue_router.CONTROLLER_TASKS.
        reap_stuck_aborting_jobs,
        # phaze-o0n6: the SIBLING of reap_stuck_aborting_jobs for the OTHER status outside SAQ's
        # `_enqueue` overwrite allowlist. A row stranded in 'active' (a claimed-but-buffered row whose
        # worker died) holds process_file:<file_id> hostage exactly as an 'aborting' zombie does.
        # Deletes the saq_jobs row ONLY -- the scheduling_ledger row is the recovery source and is
        # deliberately kept. Cron-only (mirrors reap_stalled_scans), NOT in
        # enqueue_router.CONTROLLER_TASKS.
        reap_stranded_active_jobs,
        # phaze-2u8v.2: the LEDGER-side twin of reap_stuck_aborting_jobs -- clears scheduling_ledger
        # rows whose stage has finished and which are running nowhere (a lost terminal clear). Cron-only
        # (mirrors reap_stalled_scans), NOT in enqueue_router.CONTROLLER_TASKS.
        reap_resolved_ledger_rows,
        recover_orphaned_work,
        # phaze-uqyn: every-minute cross-process retro-heal for a stage backlog row stranded
        # SENTINEL-parked by a stale per-process pause cache after a real resume already ran.
        # Cron-only (mirrors reap_stalled_scans / reap_stuck_aborting_jobs), NOT in
        # enqueue_router.CONTROLLER_TASKS -- it enqueues NOTHING, only un-parks existing rows.
        reconcile_stale_stage_parks,
        stage_cloud_window,
        # Phase 54 (KSUBMIT-02): the fast kube-submit producer is operator/Phase-55-enqueueable on
        # the controller queue. NO CronJob here -- Phase 55 owns the live stage_cloud_window trigger.
        submit_cloud_job,
        # Phase 54 (KSUBMIT-04): the */5 in-flight K8s reconcile cron. Registered in BOTH functions
        # and cron_jobs (mirroring reap_stalled_scans); cron-only, NOT in enqueue_router.CONTROLLER_TASKS.
        reconcile_cloud_jobs,
    ],
    "concurrency": get_settings().worker_max_jobs,
    "cron_jobs": [
        # phaze-2akf: there is deliberately NO refresh_tracklists CronJob any more. The monthly
        # "re-fetch everything older than 90 days" sweep that used to live here contradicted the
        # drain's cache -- which is built on "a published tracklist does not change" and therefore
        # never re-fetches -- and was a second, unbounded consumer of a whole-host budget of ~1
        # request / 8 s. Operator decision (2026-08-03): the drain keeps never re-fetching, and
        # refresh becomes on-demand and targeted. It is registered in `functions` above so the admin
        # UI can enqueue it; nothing schedules it.
        # PR4: every-minute stall reaper (control-only -- needs ctx["async_session"]).
        # 5-field standard cron form.
        CronJob(reap_stalled_scans, cron="* * * * *"),  # type: ignore[type-var]
        # phaze-e57w: every-minute reaper for zombie 'aborting' SAQ rows (control-only -- needs
        # ctx["async_session"]). Same cadence/shape as reap_stalled_scans.
        CronJob(reap_stuck_aborting_jobs, cron="* * * * *"),  # type: ignore[type-var]
        # phaze-o0n6: every-minute reaper for SAQ rows STRANDED in 'active' (control-only -- needs
        # ctx["async_session"]). Same cadence/shape as its 'aborting' sibling above; it enqueues
        # NOTHING (it only frees keys, so the gated recovery path can re-drive the files), which is why
        # it is not the forbidden auto-advance cron.
        CronJob(reap_stranded_active_jobs, cron="* * * * *"),  # type: ignore[type-var]
        # phaze-2u8v.2: every-5-min reconciler for scheduling_ledger rows whose work is FINISHED and
        # running nowhere -- the clear a lost terminal callback owed. This is emphatically NOT the
        # forbidden auto-advance cron below: it enqueues NOTHING, and it is structurally incapable of
        # re-driving work because it only ever touches rows whose stage has already domain-completed.
        # An ORPHANED row (scheduled, no outcome) is deliberately left alone for the gated Recover path.
        CronJob(reap_resolved_ledger_rows, cron="*/5 * * * *"),  # type: ignore[type-var]
        # phaze-uqyn: every-minute sweep that re-runs the sentinel-guarded resume un-park for every
        # stage whose durable control row CURRENTLY reads unpaused. Closes the TOCTOU window between
        # resume_stage's one-shot un-park and the TTL-cached park writers (apply_stage_control /
        # repark_if_stage_paused) in EVERY OTHER process (a worker, the fingerprint-requeue CLI, ...)
        # -- the API process's own window is closed immediately by clear_stage_control_cache() in the
        # pause/resume endpoints themselves. A no-op on a healthy backlog (matches zero rows); never
        # touches a genuinely paused stage's parked backlog or a genuine retry backoff.
        CronJob(reconcile_stale_stage_parks, cron="* * * * *"),  # type: ignore[type-var]
        # Phase 42 (D-01): the every-5-min ``reenqueue_discovered`` auto-advance cron was REMOVED.
        # With the Phase-36 Postgres broker, queued/active jobs survive a restart, so a steady-state
        # re-enqueue loop would only churn the DB and risk re-doubling work. Recovery is now a SINGLE
        # gated boot pass (see ``startup`` -> ``recover_orphaned_work``) plus the manual DAG "Recover"
        # button -- NO periodic auto-advance. DO NOT re-add a ``recover_orphaned_work`` CronJob here.
        #
        # Phase 50 (D-02/D-03, CLOUDPIPE-01): a NARROW cron scoped ONLY to the bounded cloud-window
        # top-up. This REPLACES the deprecated Phase-49 ``release_awaiting_cloud`` drain cron (which
        # drained the WHOLE AWAITING_CLOUD set straight to process_file -- unbounded). It is NOT the
        # deleted general pipeline auto-advance and NOT a ledger replay: it stages ``push_file`` for at
        # most ``cloud_max_in_flight - window`` of the oldest held files, gated on an online COMPUTE
        # agent (and an online fileserver to initiate the push). It advances no other stage, so it
        # respects the Phase-42 "automation only in recovery" principle. Keep this distinct from the
        # deleted reenqueue cron above -- DO NOT re-add a general auto-advance cron here.
        CronJob(stage_cloud_window, cron="*/5 * * * *"),  # type: ignore[type-var]
        # Phase 54 (D-01/D-03, KSUBMIT-04): the fixed */5 in-flight K8s reconcile cron -- the safety
        # net that owns the Kueue Job lifecycle. It iterates the cloud_job sidecar (status IN
        # SUBMITTED/RUNNING, D-02), maps Job + Workload conditions to outcomes, enforces the
        # delete-after-record ordering + S3 cleanup (D-04/D-05), drives the bounded re-drive to
        # ANALYSIS_FAILED (D-08), and surfaces Inadmissible without consuming the cap (D-06/D-07). The
        # out-of-band /api/internal/agent/* callback remains the SOLE result writer (KSUBMIT-03) -- this
        # cron only drives cleanup, re-drive, and alerting. NARROW: in-flight K8s reconcile ONLY -- DO
        # NOT re-add a general auto-advance / recover_orphaned_work cron here (same guard as above).
        CronJob(reconcile_cloud_jobs, cron="*/5 * * * *"),  # type: ignore[type-var]
    ],
    "startup": startup,
    "shutdown": shutdown,
}
