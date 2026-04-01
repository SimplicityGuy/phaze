"""arq WorkerSettings -- the entry point for ``arq phaze.tasks.worker.WorkerSettings``."""

import logging
from pathlib import Path
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.config import settings
from phaze.services.proposal import ProposalService, load_prompt_template
from phaze.tasks.execution import execute_approved_batch
from phaze.tasks.functions import process_file
from phaze.tasks.metadata_extraction import extract_file_metadata
from phaze.tasks.pool import create_process_pool
from phaze.tasks.proposal import generate_proposals
from phaze.tasks.tracklist import refresh_tracklists, scrape_and_store_tracklist, search_tracklist


logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize shared resources for all jobs (arq on_startup hook)."""
    # Check that ML models are available (volume mount)
    models_dir = Path(settings.models_path)
    if not models_dir.is_dir():
        msg = f"Models directory not found: {settings.models_path}. Run 'just download-models' to populate it."
        raise RuntimeError(msg)
    pb_files = list(models_dir.glob("*.pb"))
    if not pb_files:
        msg = f"No .pb model files found in {settings.models_path}. Run 'just download-models' to populate it."
        raise RuntimeError(msg)
    logger.info("Found %d model files in %s", len(pb_files), settings.models_path)

    ctx["process_pool"] = create_process_pool()

    # Phase 6: AI proposal generation
    prompt_template = load_prompt_template()
    ctx["proposal_service"] = ProposalService(
        model=settings.llm_model,
        prompt_template=prompt_template,
        max_rpm=settings.llm_max_rpm,
    )

    # Shared async engine pool for all task functions (INFRA-01)
    task_engine = create_async_engine(
        str(settings.database_url),
        echo=settings.debug,
        pool_size=10,
        max_overflow=5,
    )
    ctx["async_session"] = async_sessionmaker(task_engine, class_=AsyncSession, expire_on_commit=False)
    ctx["task_engine"] = task_engine


async def shutdown(ctx: dict[str, Any]) -> None:
    """Clean up shared resources (arq on_shutdown hook)."""
    pool = ctx.get("process_pool")
    if pool is not None:
        pool.shutdown(wait=True)

    task_engine = ctx.get("task_engine")
    if task_engine is not None:
        await task_engine.dispose()


class WorkerSettings:
    """arq worker configuration.

    Run via: ``uv run arq phaze.tasks.worker.WorkerSettings``
    """

    functions: ClassVar[list[Any]] = [process_file, generate_proposals, execute_approved_batch, extract_file_metadata, search_tracklist, scrape_and_store_tracklist]
    cron_jobs: ClassVar[list[Any]] = [
        cron(refresh_tracklists, month={1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}, day=1, hour=3, minute=0, run_at_startup=False),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_max_jobs
    job_timeout = settings.worker_job_timeout
    max_tries = settings.worker_max_retries
    health_check_interval = settings.worker_health_check_interval
    keep_result = settings.worker_keep_result
