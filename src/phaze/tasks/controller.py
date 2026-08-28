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

phaze-48ghg.7: repowise's health index flags a `hidden_coupling` between this module and
`phaze.models.file` (60% of their shared commits, no static dependency -- this module never
imports `FileRecord`). That is by design: this module is a composition root that registers task
functions from `phaze.tasks.*`, and it is THOSE modules (reenqueue, scan_reaper,
release_awaiting_cloud, submit_cloud_job, reconcile_cloud_jobs, ...) that read/write the `files`
table. The coupling is real, not spurious -- every shared commit in the git history is a "Phase N"
pipeline-feature landing that adds a new tracked-file signal AND a new/changed entry in
`settings["functions"]` / `settings["cron_jobs"]` below in the same change -- it just travels
through the task-module layer rather than a direct import, so a static import graph cannot see it.
See `phaze.models.file.FileRecord`'s docstring for the matching note.

Docker invocation (set by Plan 13's docker-compose.yml update):
    services:
      worker:
        command: uv run saq phaze.tasks.controller.settings
        environment:
          PHAZE_ROLE: control
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import redis.asyncio as redis_async
from saq import CronJob
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import structlog

from phaze.config import export_llm_api_keys, get_settings
from phaze.database import build_async_engine
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
from phaze.tasks.filename_convention import learn_filename_conventions
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
from phaze.tasks.tracklist_drain_control import continue_armed_tracklist_drain, record_drain_slice_completion
from phaze.telemetry import configure_telemetry, shutdown_telemetry
from phaze.telemetry.db import instrument_engine
from phaze.telemetry.saq import after_process as telemetry_after_process, before_process as telemetry_before_process


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)

# phaze-sekvl: docker-compose gives `worker` and `api` the IDENTICAL `depends_on: postgres:
# service_healthy` with no ordering edge between them, so `docker compose up -d` can boot this
# process concurrently with (or before) the api lifespan's `alembic upgrade head`
# (phaze.database.run_migrations). Every OTHER control-worker duty is a `* * * * *` / `*/5 * * * *`
# CronJob that simply retries itself out of a transient pre-head schema -- but the two boot
# reconciles below (`backfill_ledger_from_saq_jobs`, `recover_orphaned_work`) are one-shot AND
# exception-swallowed (boot resilience, D-05 -- a reconcile failure must never abort controller
# boot), so hitting the race was TERMINAL for the process lifetime: the in-flight/orphaned job
# cohort was silently never reconciled until an operator happened to restart the worker again.
# Bounded retry-with-backoff closes the window the same way the cron jobs do, without turning a
# genuine, persistent failure into an indefinite hang (D-05 stays honored: the final attempt's
# failure is still swallowed, just after giving the api's migration a realistic chance to land).
_BOOT_RECONCILE_RETRY_ATTEMPTS = 5
_BOOT_RECONCILE_RETRY_DELAY_SECONDS = 2.0
# Only retry the failure shape this race actually produces: a missing table/column (or a not-yet-
# accepting-connections Postgres) surfaces through SQLAlchemy as a DBAPIError subclass (asyncpg's
# UndefinedTableError/UndefinedColumnError arrive wrapped as ProgrammingError; a refused connection
# as OperationalError). Scoping the retry to THIS type, rather than bare ``Exception``, keeps the
# retry budget for the race it exists to cover and lets any OTHER failure (a real bug in the
# reconcile logic) surface -- and get logged -- on the first attempt, exactly as before.
_RETRYABLE_BOOT_RECONCILE_EXCEPTIONS = (DBAPIError,)


async def _pause_before_boot_retry(description: str, attempt: int, attempts: int, delay_seconds: float) -> None:
    """One retryable boot-reconcile failure: warn + back off while budget remains, else log the final failure.

    Split out of :func:`_run_boot_reconcile_with_retry` (phaze-vu88k.7) so the retry loop reads as a loop
    rather than a four-deep nest. Returns either way -- exhausting the budget is not an exception here,
    because boot resilience means a failed reconcile NEVER aborts controller boot.
    """
    if attempt < attempts:
        logger.warning(f"{description} failed (schema not ready?), retrying", attempt=attempt, attempts=attempts)
        await asyncio.sleep(delay_seconds)
    else:
        logger.exception(f"{description} failed on final attempt", attempts=attempts)


async def _run_boot_reconcile_with_retry(
    description: str,
    attempt_fn: Callable[[], Awaitable[Any]],
    *,
    attempts: int = _BOOT_RECONCILE_RETRY_ATTEMPTS,
    delay_seconds: float = _BOOT_RECONCILE_RETRY_DELAY_SECONDS,
) -> Any | None:
    """Run a one-shot boot reconcile with bounded retry-with-backoff (phaze-sekvl).

    Retries ``attempt_fn`` up to ``attempts`` times on a :data:`_RETRYABLE_BOOT_RECONCILE_EXCEPTIONS`
    (schema-not-ready) failure, sleeping ``delay_seconds`` between attempts. ANY OTHER exception is
    logged once and NOT retried -- retrying it would not help, and consuming the retry budget on a
    non-transient bug would only delay surfacing it. Returns the successful result, or ``None`` if
    every attempt failed (or a non-retryable exception was raised) -- never raises, so this NEVER
    aborts controller boot, matching the pre-existing broad try/except contract.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await attempt_fn()
        except _RETRYABLE_BOOT_RECONCILE_EXCEPTIONS:
            await _pause_before_boot_retry(description, attempt, attempts, delay_seconds)
        except Exception:
            # Boot resilience still applies (never abort controller boot) -- but this is not the
            # race the retry budget exists for, so don't spend it: log once and stop.
            logger.exception(f"{description} failed")
            break
    return None


async def _probe_kueue_local_queues(control_cfg: ControlSettings) -> None:
    """Per-cluster LocalQueue reachability probe, warn-only (KDEPLOY-04, MKUE-01/03, D-05/D-06)."""
    # Phase 56/70 (KDEPLOY-04, MKUE-01/03, D-05/D-06 -- REVISED phaze-6r39): PER-CLUSTER LocalQueue-
    # reachability probe. This is a RUNTIME probe, distinct from the fail-fast kube config validators --
    # for EACH configured Kueue backend it GETs THAT cluster's LocalQueue (threaded the backend's own
    # KubeConfig) and logs a WARNING on failure. Phase 70 iterates every kueue backend (was a single
    # global probe gated on a ≤1-non-local resolved kind), so N clusters each get their own reachability
    # check. Each probe is INDEPENDENTLY guarded (its own broad try/except): a transient kube/mesh blip
    # MUST NEVER abort controller boot (D-05 -- the control plane still boots Postgres/Redis/UI/local-
    # analysis). Warnings name only the config surface; they never interpolate an SA token or kube DSN
    # (T-56-LOG / T-54-07).
    #
    # phaze-6r39: this loop USED TO ALSO persist a cross-process Redis flag (D-05/D-06, the
    # "LocalQueue-unreachable" key) for the dashboard to read. That flag was a
    # boot-time SNAPSHOT with no TTL and no other writer: it never cleared once connectivity was
    # restored (the reported bug) and never appeared at all for an outage that began AFTER boot (the
    # silent, more dangerous half -- the alert was structurally incapable of firing for the exact class
    # of event it exists to surface). The dashboard now derives the SAME alert LIVE, from the SAME probe
    # already run on every 5s ``/pipeline/stats`` poll via ``get_backend_lane_snapshot`` ->
    # ``derive_localqueue_unreachable`` (services/backends.py), so the Redis write is GONE -- there is no
    # migration and no key to clear; a currently-stuck stale key simply becomes unread and inert the
    # moment this deploy lands. This loop and its WARNING log are KEPT deliberately: they remain the
    # operator's boot-time log signal that a configured cluster was unreachable at startup. Do NOT
    # "restore" a Redis write here -- only the write was retired, the probe and its log were not.
    kueue_kubes = [kube for entry in control_cfg.backends if entry.kind == "kueue" and (kube := getattr(entry, "kube", None)) is not None]
    for kube in kueue_kubes:
        try:
            await kube_staging.get_local_queue(kube)
        except Exception:
            logger.warning(
                "phaze.controller startup: a Kueue LocalQueue is unreachable -- check cluster connectivity "
                "and the backend's [kube] local_queue configuration; control plane boots regardless (D-05)"
            )


async def _push_bucket_lifecycle_ttls(control_cfg: ControlSettings) -> None:
    """Push the KSTAGE-04/D-02 lifecycle-TTL backstop onto every configured staging bucket (phaze-cws5)."""
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

    # phaze-m1drf.1: the control worker is its own OS process, so it installs its own SDK.
    # Off unless an OTLP endpoint is configured; never raises.
    configure_telemetry("controller")

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
    # phaze-bk9el.12: engine construction (the PgBouncer session-mode pool tuning --
    # quick-260707-ryn) is now the single shared ``build_async_engine`` in phaze.database
    # -- see its docstring for the pool-kwarg rationale. This module's own database.py
    # counterpart (the api process's module-level ``engine``) builds the SAME way, from
    # the api's ``settings`` singleton; this was a 25-line clone between the two.
    task_engine = build_async_engine(cfg)
    instrument_engine(task_engine)
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
    # drains. Boot resilience (T-45-14) is now bounded retry-with-backoff rather than a bare
    # try/except -- see ``_run_boot_reconcile_with_retry`` (phaze-sekvl): a single failed attempt no
    # longer strands the process for its whole lifetime if it landed while the api's migration was
    # still in flight.
    async def _do_backfill() -> dict[str, int]:
        async with ctx["async_session"]() as session:
            tally = await backfill_ledger_from_saq_jobs(session)
            await session.commit()
        return tally

    tally = await _run_boot_reconcile_with_retry("ledger backfill on startup", _do_backfill)
    if tally is not None:
        logger.info("phaze.controller startup ledger backfill", inserted=tally["inserted"], skipped=tally["skipped"])

    async def _do_recovery() -> dict[str, Any]:
        return await recover_orphaned_work(ctx)

    result = await _run_boot_reconcile_with_retry("recover_orphaned_work on startup", _do_recovery)
    if result is not None:
        logger.info("phaze.controller startup recovery", detected_loss=result["detected_loss"], stages=result["stages"])

    control_cfg = cast("ControlSettings", cfg)
    await _probe_kueue_local_queues(control_cfg)
    await _push_bucket_lifecycle_ttls(control_cfg)


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

    # LAST. Bounded flush, never raises -- see phaze/telemetry/bootstrap.py.
    shutdown_telemetry()


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
    # the settings dict the SAQ CLI hands to Worker.__init__. A list runs every hook in order
    # (mirrors agent_worker.py's `[repark_if_stage_paused, increment_completed]`).
    # phaze-6nrrf: record_drain_slice_completion is the continuous-drain cron's own after_process
    # half -- see tasks/tracklist_drain_control.py.
    # phaze-m1drf.1 acceptance 3: telemetry_after_process runs LAST so the duration it
    # records covers the other hooks' work too -- a ledger clear that becomes slow is part
    # of what the job cost. Both telemetry hooks swallow every exception; they run in SAQ's
    # own `finally` alongside the ledger clear and must not be able to displace it.
    "before_process": telemetry_before_process,
    "after_process": [increment_completed, record_drain_slice_completion, telemetry_after_process],
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
        # phaze-6nrrf: the continuous-drain cron body. Registered here (Worker functions) AND in
        # cron_jobs below, mirroring reap_stalled_scans -- it is CRON-ONLY, never operator/API
        # enqueued directly, so it is NOT in enqueue_router.CONTROLLER_TASKS. See
        # tasks/tracklist_drain_control.py's module docstring for why this is not the forbidden
        # general auto-advance cron pattern warned about elsewhere in this file: it only ever
        # CONTINUES a pass the operator explicitly armed, never starts one on its own.
        continue_armed_tracklist_drain,
        # phaze-5fta.3: one full refresh of the corpus-learned release-group date-order
        # conventions. Operator-enqueueable with NO CronJob, deliberately (see the task module):
        # it sweeps the whole corpus and its output gates rename proposals, so recompute timing is
        # left to the operator to invoke at runtime, not automated. The table is a pure cache, so
        # staleness is the only cost of never running it.
        learn_filename_conventions,
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
        # Phase 54 (KSUBMIT-04): the every-minute in-flight K8s reconcile cron. Registered in BOTH functions
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
        #
        # phaze-6nrrf: there is STILL no `drain_tracklists` CronJob (test_the_drain_has_no_cron_job
        # asserts it) -- the ethics bound is unchanged, nothing may start crawling on container
        # boot. `continue_armed_tracklist_drain` below is a DIFFERENT function: it is a narrow
        # continuation gate that only re-enqueues a slice when the durable
        # `tracklist_drain_arm_state` row already reads armed=true, which is set ONLY by the
        # operator's explicit Arm click (never by this cron, never by boot/deploy). Every-minute
        # cadence matches this file's other reapers; a full slice's own host-budget pacing (~1
        # req/8s) is far coarser than one minute, so this cadence only bounds how quickly the NEXT
        # slice starts after the previous one's cooldown elapses, never how fast requests fire.
        CronJob(continue_armed_tracklist_drain, cron="* * * * *"),  # type: ignore[type-var]
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
        # Phase 54 (D-01/D-03, KSUBMIT-04; phaze-i3pkb.1): the every-minute in-flight K8s reconcile
        # cron -- the one-minute freshness bound keeps durable admission/pod state close to Kueue truth.
        # It remains the safety net that owns the Kueue Job lifecycle. It iterates the cloud_job sidecar (status IN
        # SUBMITTED/RUNNING, D-02), maps Job + Workload conditions to outcomes, enforces the
        # delete-after-record ordering + S3 cleanup (D-04/D-05), drives the bounded re-drive to
        # ANALYSIS_FAILED (D-08), and surfaces Inadmissible without consuming the cap (D-06/D-07). The
        # out-of-band /api/internal/agent/* callback remains the SOLE result writer (KSUBMIT-03) -- this
        # cron only drives cleanup, re-drive, and alerting. NARROW: in-flight K8s reconcile ONLY -- DO
        # NOT re-add a general auto-advance / recover_orphaned_work cron here (same guard as above).
        CronJob(reconcile_cloud_jobs, cron="* * * * *"),  # type: ignore[type-var]
    ],
    "startup": startup,
    "shutdown": shutdown,
}
