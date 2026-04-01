"""Tests for process pool lifecycle and helpers."""

from concurrent.futures import ProcessPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

from phaze.config import settings
from phaze.tasks.pool import create_process_pool, run_in_process_pool
from phaze.tasks.worker import shutdown, startup


def test_create_process_pool_returns_executor() -> None:
    """create_process_pool returns a ProcessPoolExecutor with correct max_workers."""
    pool = create_process_pool()
    try:
        assert isinstance(pool, ProcessPoolExecutor)
        assert pool._max_workers == settings.worker_process_pool_size
    finally:
        pool.shutdown(wait=False)


async def test_startup_creates_process_pool(tmp_path) -> None:
    """startup(ctx) creates ctx['process_pool'] as a ProcessPoolExecutor."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "test.pb").write_bytes(b"fake")

    ctx: dict = {}
    with (
        patch("phaze.tasks.worker.settings") as mock_settings,
        patch("phaze.tasks.worker.load_prompt_template", return_value="t"),
        patch("phaze.tasks.worker.ProposalService"),
        patch("phaze.tasks.worker.create_async_engine"),
    ):
        mock_settings.models_path = str(models_dir)
        mock_settings.llm_model = "test"
        mock_settings.llm_max_rpm = 30
        mock_settings.database_url = "postgresql+asyncpg://test:test@localhost/test"
        mock_settings.debug = False
        mock_settings.audfprint_url = "http://audfprint:8001"
        mock_settings.panako_url = "http://panako:8002"
        await startup(ctx)
    try:
        assert "process_pool" in ctx
        assert isinstance(ctx["process_pool"], ProcessPoolExecutor)
        assert "async_session" in ctx
        assert "task_engine" in ctx
    finally:
        ctx["process_pool"].shutdown(wait=False)


async def test_shutdown_calls_pool_shutdown() -> None:
    """shutdown(ctx) calls process_pool.shutdown(wait=True) and disposes task_engine."""
    mock_pool = MagicMock(spec=ProcessPoolExecutor)
    mock_engine = AsyncMock()
    ctx: dict = {"process_pool": mock_pool, "task_engine": mock_engine}
    await shutdown(ctx)
    mock_pool.shutdown.assert_called_once_with(wait=True)
    mock_engine.dispose.assert_awaited_once()


async def test_run_in_process_pool_executes_function() -> None:
    """run_in_process_pool calls run_in_executor and returns result."""
    pool = ProcessPoolExecutor(max_workers=1)
    ctx: dict = {"process_pool": pool}
    try:
        result = await run_in_process_pool(ctx, _double, 21)
        assert result == 42
    finally:
        pool.shutdown(wait=False)


def _double(x: int) -> int:
    """Simple test function for process pool."""
    return x * 2
