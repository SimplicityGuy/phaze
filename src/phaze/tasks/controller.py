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

from saq import CronJob, Queue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
import structlog

from phaze.config import get_settings
from phaze.logging_config import configure_logging
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.discogs_matcher import DiscogsographyClient
from phaze.services.proposal import ProposalService, load_prompt_template
from phaze.tasks._shared.queue_defaults import apply_project_job_defaults
from phaze.tasks.discogs import match_tracklist_to_discogs
from phaze.tasks.proposal import generate_proposals
from phaze.tasks.reenqueue import reenqueue_discovered
from phaze.tasks.scan_reaper import reap_stalled_scans
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

    logger.info("phaze.controller startup role=control queue=controller redis=%s", cfg.redis_url)

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

    # W4 audit RESULT: `ctx["queue"]` IS read by fileless tasks. Specifically
    # `phaze.tasks.proposal.generate_proposals` calls `ctx["queue"].redis` for
    # LLM rate-limit cache, and `phaze.tasks.execution` reads it as well.
    # Stash the module-level queue so those readers find it.
    ctx["queue"] = queue

    # Phase 32: per-agent task router for reboot re-enqueue routing. Built ONCE
    # here and reused for the boot-time call + every cron tick (RESEARCH Pitfall 4 --
    # never construct a fresh AgentTaskRouter per call, it would leak Redis pools).
    # Mirrors the discogs_client create/close lifecycle: created in startup, closed
    # in shutdown.
    ctx["task_router"] = AgentTaskRouter(cfg.redis_url)

    # Phase 32 reboot recovery: re-enqueue every DISCOVERED file ONCE on boot so a
    # reboot/Redis-flush resumes analysis with no manual "Run Analysis" (CONTEXT
    # "Trigger"). Redis is empty after a reboot, so every DISCOVERED file re-enqueues;
    # mid-run boots dedup to no-ops via the shared deterministic key. Boot resilience
    # is non-negotiable: a re-enqueue failure must NEVER abort controller boot
    # (RESEARCH Pitfall 3) -- wrap in a broad try/except and continue.
    try:
        counts = await reenqueue_discovered(ctx)
        logger.info("phaze.controller startup re-enqueue", reenqueued=counts["reenqueued"], skipped=counts["skipped"])
    except Exception:
        logger.exception("reenqueue on startup failed")


async def shutdown(ctx: dict[str, Any]) -> None:
    """Clean up shared resources (SAQ shutdown hook)."""
    logger.info("phaze.controller shutdown")

    task_engine = ctx.get("task_engine")
    if task_engine is not None:
        await task_engine.dispose()

    discogs_client = ctx.get("discogs_client")
    if discogs_client is not None:
        await discogs_client.close()

    # Phase 32: close the per-agent task router (disconnects every cached Redis
    # queue; idempotent). Mirrors the discogs_client cleanup above.
    task_router = ctx.get("task_router")
    if task_router is not None:
        await task_router.close()


# Module-level Queue construction. SAQ's `saq <module>.settings` CLI imports
# this module and reads `settings` as a top-level attribute (RESEARCH §A2).
queue = Queue.from_url(get_settings().redis_url, name="controller")
# Phase 27 UAT Gap 1: SAQ 0.26.3's Worker.__init__ does NOT accept `timeout`,
# `retries`, or `keep_result` -- those are per-Job settings. Apply the project's
# policy defaults via a `before_enqueue` hook on the Queue so every enqueued
# Job inherits the longer timeout / retry budget without breaking Worker
# construction. See phaze.tasks._shared.queue_defaults for the hook body.
queue.register_before_enqueue(apply_project_job_defaults)


settings = {
    "queue": queue,
    "functions": [
        generate_proposals,
        match_tracklist_to_discogs,
        search_tracklist,
        scrape_and_store_tracklist,
        reap_stalled_scans,
        reenqueue_discovered,
    ],
    "concurrency": get_settings().worker_max_jobs,
    "cron_jobs": [
        CronJob(refresh_tracklists, cron="0 3 1 * *"),  # type: ignore[type-var]
        # PR4: every-minute stall reaper (control-only -- needs ctx["async_session"]).
        # 5-field standard cron form, matching refresh_tracklists above.
        CronJob(reap_stalled_scans, cron="* * * * *"),  # type: ignore[type-var]
        # Phase 32: mid-run stall recovery (control-only -- needs ctx["async_session"]
        # + ctx["task_router"]). Every 5 min, not every minute like the reaper:
        # re-enqueue scans more rows (all DISCOVERED files) than the 1-min reaper,
        # so 5 min balances recovery latency against DB load (CONTEXT Claude's Discretion).
        CronJob(reenqueue_discovered, cron="*/5 * * * *"),  # type: ignore[type-var]
    ],
    "startup": startup,
    "shutdown": shutdown,
}
