"""ProcessPoolExecutor lifecycle and helper for CPU-bound work."""

import asyncio
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from phaze.config import settings


def create_process_pool() -> ProcessPoolExecutor:
    """Create a ProcessPoolExecutor sized from settings (D-04)."""
    return ProcessPoolExecutor(max_workers=settings.worker_process_pool_size)


async def run_in_process_pool(ctx: dict[str, Any], func: Callable[..., Any], *args: Any) -> Any:
    """Run a CPU-bound function in the worker's process pool.

    Uses asyncio.get_running_loop().run_in_executor() so the async
    event loop is never blocked (per D-04).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(ctx["process_pool"], func, *args)
