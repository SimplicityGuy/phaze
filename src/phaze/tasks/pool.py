"""Killable process pool lifecycle and helper for CPU-bound work.

Phase 43: replaces the un-killable ``concurrent.futures.ProcessPoolExecutor``
with a ``pebble.ProcessPool`` so a runaway essentia child that exceeds the inner
per-task timeout is SIGKILLed and its slot reclaimed. ``concurrent.futures``
cannot cancel an already-started child, so a timed-out job leaked compute and
starved the 4-slot pool — this module owns the deterministic kill instead.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from pebble import ProcessPool

from phaze.config import settings


def create_process_pool() -> ProcessPool:
    """Create a killable pebble ProcessPool sized from settings (Phase 43, D-04).

    ``max_tasks=1`` recycles each worker after every task to bound essentia's
    ~7 GiB/file memory leak; pebble's per-task ``timeout`` (passed via
    :func:`run_in_process_pool`) SIGKILLs a runaway child and reclaims its slot.
    """
    return ProcessPool(max_workers=settings.worker_process_pool_size, max_tasks=1)


async def run_in_process_pool(
    ctx: dict[str, Any],
    func: Callable[..., Any],
    *args: Any,
    timeout: float | None = None,
    **kwargs: Any,
) -> Any:
    """Run a CPU-bound function in the worker's killable pebble pool.

    Schedules ``func`` on the pebble ProcessPool with a hard per-task
    ``timeout``; a child exceeding it is SIGKILLed and the resulting
    ``ProcessFuture`` raises ``builtins.TimeoutError`` (Phase 43).
    ``asyncio.wrap_future`` bridges the pebble future onto the running event
    loop so the loop is never blocked (per D-04).
    """
    pool: ProcessPool = ctx["process_pool"]
    # pebble's schedule() types args as ``list`` and timeout as ``float`` (no None),
    # so build the call to satisfy strict mypy: pass timeout only when provided.
    schedule_kwargs: dict[str, Any] = {"args": list(args), "kwargs": kwargs}
    if timeout is not None:
        schedule_kwargs["timeout"] = timeout
    future = pool.schedule(func, **schedule_kwargs)
    return await asyncio.wrap_future(future)
