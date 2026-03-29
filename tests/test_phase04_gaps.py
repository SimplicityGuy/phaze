"""Phase 4 gap-filling tests: ArqRedis pool lifespan and docker-compose worker command.

Covers:
- 04-02-01 (INF-02): ArqRedis pool is created during FastAPI lifespan startup
  and closed during shutdown.
- 04-02-01 (INF-02): Docker Compose worker service uses the correct arq command.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Gap 1: ArqRedis pool lifecycle in FastAPI lifespan
# ---------------------------------------------------------------------------
# Note: ASGITransport does not invoke the FastAPI lifespan, so we invoke the
# lifespan context manager directly against a minimal mock app object.


@pytest.mark.asyncio
async def test_lifespan_creates_arq_pool_on_startup() -> None:
    """FastAPI lifespan creates an arq pool on app.state during startup."""
    from fastapi import FastAPI

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()

    with (
        patch("phaze.main.create_pool", new_callable=AsyncMock, return_value=mock_pool) as mock_create,
        patch("phaze.main.engine") as mock_engine,
    ):
        mock_conn = AsyncMock()
        mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.dispose = AsyncMock()

        from phaze.main import lifespan

        app = FastAPI()
        # Invoke the lifespan directly so startup hooks actually run
        async with lifespan(app):
            # create_pool must have been called exactly once during startup
            mock_create.assert_called_once()
            # Pool must be stored on app.state
            assert app.state.arq_pool is mock_pool


@pytest.mark.asyncio
async def test_lifespan_closes_arq_pool_on_shutdown() -> None:
    """FastAPI lifespan closes the arq pool when the application shuts down."""
    from fastapi import FastAPI

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()

    with (
        patch("phaze.main.create_pool", new_callable=AsyncMock, return_value=mock_pool),
        patch("phaze.main.engine") as mock_engine,
    ):
        mock_conn = AsyncMock()
        mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.dispose = AsyncMock()

        from phaze.main import lifespan

        app = FastAPI()
        async with lifespan(app):
            pass  # context exit triggers lifespan shutdown

        # Pool close must be called exactly once on shutdown
        mock_pool.close.assert_called_once()


# ---------------------------------------------------------------------------
# Gap 2: Docker Compose worker service uses the correct arq command
# ---------------------------------------------------------------------------


def test_docker_compose_worker_command_is_arq() -> None:
    """docker-compose.yml worker service command is 'uv run arq phaze.tasks.worker.WorkerSettings'."""
    compose_file = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose_file.exists(), "docker-compose.yml not found at project root"
    content = compose_file.read_text()
    assert "uv run arq phaze.tasks.worker.WorkerSettings" in content, (
        "Worker service must use 'uv run arq phaze.tasks.worker.WorkerSettings' as its command"
    )
