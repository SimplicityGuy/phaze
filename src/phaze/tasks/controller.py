"""SAQ controller settings -- entry point for ``saq phaze.tasks.controller.settings`` (Phase 26 D-01..D-04).

Control role: runs the application server's SAQ worker pool. Fileless tasks only:
- generate_proposals (LLM-driven rename suggestions)
- match_tracklist_to_discogs (Discogsography HTTP API)
- search_tracklist + scrape_and_store_tracklist (1001Tracklists scraping)
- refresh_tracklists (monthly cron)

This module does NOT import `phaze.services.fingerprint` or `phaze.tasks.pool`
(those belong to the agent role per Phase 26 D-03). Cross-imports between
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

from typing import Any

import redis.asyncio as redis_async
from saq import CronJob
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
import structlog

from phaze.config import export_llm_api_keys, get_settings
from phaze.logging_config import configure_logging
from phaze.services import kube_staging
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.discogs_matcher import DiscogsographyClient
from phaze.services.proposal import ProposalService, load_prompt_template
from phaze.tasks._shared.deterministic_key import increment_completed
from phaze.tasks._shared.queue_factory import build_pipeline_queue
from phaze.tasks.discogs import match_tracklist_to_discogs
from phaze.tasks.proposal import generate_proposals
from phaze.tasks.reconcile_cloud_jobs import reconcile_cloud_jobs
from phaze.tasks.reenqueue import backfill_ledger_from_saq_jobs, recover_orphaned_work
from phaze.tasks.release_awaiting_cloud import stage_cloud_window
from phaze.tasks.scan_reaper import reap_stalled_scans
from phaze.tasks.submit_cloud_job import submit_cloud_job
from phaze.tasks.tracklist import refresh_tracklists, scrape_and_store_tracklist, search_tracklist


logger = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize shared resources for fileless tasks (SAQ startup hook).

    Does NOT initialize: process pool, fingerprint orchestrator, models check.
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

    # Shared async engine pool for all fileless task functions (INFRA-01 from v1.0).
    task_engine = create_async_engine(
        str(cfg.database_url),
        echo=cfg.debug,
        pool_size=10,
        max_overflow=5,
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

    # Phase 56 (KDEPLOY-04, D-05/D-06): live LocalQueue-reachability probe. This is a RUNTIME probe,
    # distinct from the three fail-fast kube config validators -- it GETs the configured Kueue
    # LocalQueue and writes a cross-process flag the dashboard reads. Gated on cloud_target == "k8s"
    # so a non-k8s control plane never touches kube. Wrapped in its OWN broad try/except mirroring the
    # recovery block above: a transient kube/mesh blip MUST NEVER abort controller boot (D-05 -- the
    # control plane still boots Postgres/Redis/UI/local-analysis). The WARNING names only the env var
    # PHAZE_KUBE_LOCAL_QUEUE; it never interpolates the SA token or kube DSN (T-56-LOG / T-54-07).
    if cfg.cloud_target == "k8s":  # type: ignore[attr-defined]
        try:
            await kube_staging.get_local_queue()
            await ctx["redis"].delete("phaze:k8s:localqueue_unreachable")
        except Exception:
            logger.warning(
                "phaze.controller startup: Kueue LocalQueue unreachable -- check cluster connectivity "
                "and the PHAZE_KUBE_LOCAL_QUEUE configuration; control plane boots regardless (D-05)"
            )
            await ctx["redis"].set("phaze:k8s:localqueue_unreachable", "1")


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
        search_tracklist,
        scrape_and_store_tracklist,
        reap_stalled_scans,
        recover_orphaned_work,
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
        CronJob(refresh_tracklists, cron="0 3 1 * *"),  # type: ignore[type-var]
        # PR4: every-minute stall reaper (control-only -- needs ctx["async_session"]).
        # 5-field standard cron form, matching refresh_tracklists above.
        CronJob(reap_stalled_scans, cron="* * * * *"),  # type: ignore[type-var]
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
