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

import logging
from typing import Any

from saq import CronJob, Queue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.config import get_settings
from phaze.services.discogs_matcher import DiscogsographyClient
from phaze.services.proposal import ProposalService, load_prompt_template
from phaze.tasks.discogs import match_tracklist_to_discogs
from phaze.tasks.proposal import generate_proposals
from phaze.tasks.tracklist import refresh_tracklists, scrape_and_store_tracklist, search_tracklist


logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize shared resources for fileless tasks (SAQ startup hook).

    Does NOT initialize: process pool, fingerprint orchestrator, models check.
    Those belong to the agent role; the control role's worker never reads files.
    """
    cfg = get_settings()
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


async def shutdown(ctx: dict[str, Any]) -> None:
    """Clean up shared resources (SAQ shutdown hook)."""
    logger.info("phaze.controller shutdown")

    task_engine = ctx.get("task_engine")
    if task_engine is not None:
        await task_engine.dispose()

    discogs_client = ctx.get("discogs_client")
    if discogs_client is not None:
        await discogs_client.close()


# Module-level Queue construction. SAQ's `saq <module>.settings` CLI imports
# this module and reads `settings` as a top-level attribute (RESEARCH §A2).
queue = Queue.from_url(get_settings().redis_url, name="controller")


settings = {
    "queue": queue,
    "functions": [
        generate_proposals,
        match_tracklist_to_discogs,
        search_tracklist,
        scrape_and_store_tracklist,
    ],
    "concurrency": get_settings().worker_max_jobs,
    "timeout": get_settings().worker_job_timeout,
    "retries": get_settings().worker_max_retries,
    "keep_result": get_settings().worker_keep_result,
    "cron_jobs": [
        CronJob(refresh_tracklists, cron="0 3 1 * *"),  # type: ignore[type-var]
    ],
    "startup": startup,
    "shutdown": shutdown,
}
