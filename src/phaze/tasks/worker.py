"""SAQ worker settings -- the entry point for ``saq phaze.tasks.worker.settings``."""

import logging
from pathlib import Path
from typing import Any

from saq import CronJob, Queue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.config import settings as app_settings
from phaze.services.fingerprint import AudfprintAdapter, FingerprintOrchestrator, PanakoAdapter
from phaze.services.proposal import ProposalService, load_prompt_template
from phaze.tasks.execution import execute_approved_batch
from phaze.tasks.fingerprint import fingerprint_file
from phaze.tasks.functions import process_file
from phaze.tasks.metadata_extraction import extract_file_metadata
from phaze.tasks.pool import create_process_pool
from phaze.tasks.proposal import generate_proposals
from phaze.tasks.scan import scan_live_set
from phaze.tasks.tracklist import refresh_tracklists, scrape_and_store_tracklist, search_tracklist


logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize shared resources for all jobs (SAQ startup hook)."""
    # Check that ML models are available (volume mount)
    models_dir = Path(app_settings.models_path)
    if not models_dir.is_dir():
        msg = f"Models directory not found: {app_settings.models_path}. Run 'just download-models' to populate it."
        raise RuntimeError(msg)
    pb_files = list(models_dir.glob("*.pb"))
    if not pb_files:
        msg = f"No .pb model files found in {app_settings.models_path}. Run 'just download-models' to populate it."
        raise RuntimeError(msg)
    logger.info("Found %d model files in %s", len(pb_files), app_settings.models_path)

    ctx["process_pool"] = create_process_pool()

    # Phase 6: AI proposal generation
    prompt_template = load_prompt_template()
    ctx["proposal_service"] = ProposalService(
        model=app_settings.llm_model,
        prompt_template=prompt_template,
        max_rpm=app_settings.llm_max_rpm,
    )

    # Shared async engine pool for all task functions (INFRA-01)
    task_engine = create_async_engine(
        str(app_settings.database_url),
        echo=app_settings.debug,
        pool_size=10,
        max_overflow=5,
    )
    ctx["async_session"] = async_sessionmaker(task_engine, class_=AsyncSession, expire_on_commit=False)
    ctx["task_engine"] = task_engine

    # Phase 16: Fingerprint service orchestrator
    audfprint_adapter = AudfprintAdapter(base_url=app_settings.audfprint_url)
    panako_adapter = PanakoAdapter(base_url=app_settings.panako_url)
    ctx["fingerprint_orchestrator"] = FingerprintOrchestrator(engines=[audfprint_adapter, panako_adapter])


async def shutdown(ctx: dict[str, Any]) -> None:
    """Clean up shared resources (SAQ shutdown hook)."""
    pool = ctx.get("process_pool")
    if pool is not None:
        pool.shutdown(wait=True)

    task_engine = ctx.get("task_engine")
    if task_engine is not None:
        await task_engine.dispose()

    orchestrator = ctx.get("fingerprint_orchestrator")
    if orchestrator is not None:
        for eng in orchestrator.engines:
            if hasattr(eng, "close"):
                await eng.close()


queue = Queue.from_url(app_settings.redis_url)

settings = {
    "queue": queue,
    "functions": [
        process_file,
        generate_proposals,
        execute_approved_batch,
        extract_file_metadata,
        fingerprint_file,
        search_tracklist,
        scrape_and_store_tracklist,
        scan_live_set,
    ],
    "concurrency": app_settings.worker_max_jobs,
    "cron_jobs": [
        CronJob(refresh_tracklists, cron="0 3 1 * *"),  # type: ignore[type-var]  # 1st of each month at 03:00
    ],
    "startup": startup,
    "shutdown": shutdown,
}
