"""arq WorkerSettings -- the entry point for ``arq phaze.tasks.worker.WorkerSettings``."""

from typing import Any, ClassVar

from arq.connections import RedisSettings

from phaze.config import settings
from phaze.services.proposal import ProposalService, load_prompt_template
from phaze.tasks.functions import process_file
from phaze.tasks.pool import create_process_pool
from phaze.tasks.proposal import generate_proposals


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize shared resources for all jobs (arq on_startup hook)."""
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

    functions: ClassVar[list[Any]] = [process_file, generate_proposals]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_max_jobs
    job_timeout = settings.worker_job_timeout
    max_tries = settings.worker_max_retries
    health_check_interval = settings.worker_health_check_interval
    keep_result = settings.worker_keep_result
