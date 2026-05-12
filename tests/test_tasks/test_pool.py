"""Tests for process pool lifecycle and helpers.

The process pool is owned by ``phaze.tasks.agent_worker`` (Phase 26 D-03 +
D-04): only the agent role runs CPU-bound essentia analysis, so the controller
role does not need a process pool. The agent worker's startup/shutdown
behaviour is exercised by tests/test_tasks/test_agent_startup_banner.py;
this file covers the pool helpers themselves.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

from phaze.config import settings
from phaze.tasks.pool import create_process_pool, run_in_process_pool


def test_create_process_pool_returns_executor() -> None:
    """create_process_pool returns a ProcessPoolExecutor with correct max_workers."""
    pool = create_process_pool()
    try:
        assert isinstance(pool, ProcessPoolExecutor)
        assert pool._max_workers == settings.worker_process_pool_size
    finally:
        pool.shutdown(wait=False)


async def test_run_in_process_pool_executes_function() -> None:
    """run_in_process_pool calls run_in_executor and returns result."""
    pool = ProcessPoolExecutor(max_workers=1)
    ctx: dict[str, object] = {"process_pool": pool}
    try:
        result = await run_in_process_pool(ctx, _double, 21)
        assert result == 42
    finally:
        pool.shutdown(wait=False)


def _double(x: int) -> int:
    """Simple test function for process pool."""
    return x * 2
