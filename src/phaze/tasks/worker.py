"""arq WorkerSettings -- the entry point for ``arq phaze.tasks.worker.WorkerSettings``."""

from typing import Any, ClassVar

from arq.connections import RedisSettings

from phaze.config import settings
from phaze.tasks.functions import process_file
from phaze.tasks.pool import create_process_pool


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize shared resources for all jobs (arq on_startup hook)."""
    ctx["process_pool"] = create_process_pool()


async def shutdown(ctx: dict[str, Any]) -> None:
    """Clean up shared resources (arq on_shutdown hook)."""
    pool = ctx.get("process_pool")
    if pool is not None:
        pool.shutdown(wait=True)


class WorkerSettings:
    """arq worker configuration.

    Run via: ``uv run arq phaze.tasks.worker.WorkerSettings``
    """

    functions: ClassVar[list[Any]] = [process_file]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_max_jobs
    job_timeout = settings.worker_job_timeout
    max_tries = settings.worker_max_retries
    health_check_interval = settings.worker_health_check_interval
    keep_result = settings.worker_keep_result
