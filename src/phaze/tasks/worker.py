"""arq WorkerSettings -- the entry point for ``arq phaze.tasks.worker.WorkerSettings``."""

import logging
from pathlib import Path
from typing import Any, ClassVar

from arq.connections import RedisSettings

from phaze.config import settings
from phaze.services.proposal import ProposalService, load_prompt_template
from phaze.tasks.execution import execute_approved_batch
from phaze.tasks.functions import process_file
from phaze.tasks.metadata_extraction import extract_file_metadata
from phaze.tasks.pool import create_process_pool
from phaze.tasks.proposal import generate_proposals


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


async def shutdown(ctx: dict[str, Any]) -> None:
    """Clean up shared resources (arq on_shutdown hook)."""
    pool = ctx.get("process_pool")
    if pool is not None:
        pool.shutdown(wait=True)


class WorkerSettings:
    """arq worker configuration.

    Run via: ``uv run arq phaze.tasks.worker.WorkerSettings``
    """

    functions: ClassVar[list[Any]] = [process_file, generate_proposals, execute_approved_batch, extract_file_metadata]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_max_jobs
    job_timeout = settings.worker_job_timeout
    max_tries = settings.worker_max_retries
    health_check_interval = settings.worker_health_check_interval
    keep_result = settings.worker_keep_result
