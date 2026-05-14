"""Phase 4 gap-filling tests: SAQ queue lifespan and docker-compose worker command.

Covers:
- 04-02-01 (INF-02): SAQ queue is created during FastAPI lifespan startup
  and closed during shutdown.
- 04-02-01 (INF-02): Docker Compose worker service uses the correct SAQ command.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Gap 1: SAQ queue lifecycle in FastAPI lifespan
# ---------------------------------------------------------------------------
# Note: ASGITransport does not invoke the FastAPI lifespan, so we invoke the
# lifespan context manager directly against a minimal mock app object.


@pytest.mark.asyncio
async def test_lifespan_creates_queue_on_startup() -> None:
    """FastAPI lifespan creates a SAQ queue on app.state during startup."""
    from fastapi import FastAPI

    mock_queue = MagicMock()
    mock_queue.disconnect = AsyncMock()

    with (
        patch("phaze.main.Queue") as mock_queue_cls,
        patch("phaze.main.engine") as mock_engine,
        # Phase 27 UAT Gap 2 / Gap 3: lifespan now also invokes run_migrations
        # and ensure_dev_agent. Patch them out so this test stays unit-level.
        patch("phaze.main.run_migrations", new=AsyncMock()),
        patch("phaze.main.ensure_dev_agent", new=AsyncMock(return_value=None)),
        patch("phaze.main.async_session") as mock_async_session,
    ):
        mock_queue_cls.from_url.return_value = mock_queue
        mock_conn = AsyncMock()
        mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.dispose = AsyncMock()
        # async_session() is used as `async with async_session() as s:` inside
        # the lifespan -- give it a context-manager protocol that yields a mock.
        mock_async_session.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_async_session.return_value.__aexit__ = AsyncMock(return_value=False)

        from phaze.main import lifespan

        app = FastAPI()
        # Invoke the lifespan directly so startup hooks actually run
        async with lifespan(app):
            # Queue.from_url must have been called exactly once during startup
            mock_queue_cls.from_url.assert_called_once()
            # Queue must be stored on app.state
            assert app.state.queue is mock_queue


@pytest.mark.asyncio
async def test_lifespan_disconnects_queue_on_shutdown() -> None:
    """FastAPI lifespan disconnects the SAQ queue when the application shuts down."""
    from fastapi import FastAPI

    mock_queue = MagicMock()
    mock_queue.disconnect = AsyncMock()

    with (
        patch("phaze.main.Queue") as mock_queue_cls,
        patch("phaze.main.engine") as mock_engine,
        # Phase 27 UAT Gap 2 / Gap 3: see test_lifespan_creates_queue_on_startup above.
        patch("phaze.main.run_migrations", new=AsyncMock()),
        patch("phaze.main.ensure_dev_agent", new=AsyncMock(return_value=None)),
        patch("phaze.main.async_session") as mock_async_session,
    ):
        mock_queue_cls.from_url.return_value = mock_queue
        mock_conn = AsyncMock()
        mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.dispose = AsyncMock()
        mock_async_session.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_async_session.return_value.__aexit__ = AsyncMock(return_value=False)

        from phaze.main import lifespan

        app = FastAPI()
        async with lifespan(app):
            pass  # context exit triggers lifespan shutdown

        # Queue disconnect must be called exactly once on shutdown
        mock_queue.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# Gap 2: Docker Compose worker service uses the correct SAQ command
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Gap 3: Agent-worker startup checks for models directory (Phase 26 D-04 -- the
# models-dir guard is now owned by phaze.tasks.agent_worker; the controller is
# fileless and never reads models. Detailed startup-behaviour coverage lives in
# tests/test_tasks/test_agent_startup_banner.py.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_startup_raises_if_models_dir_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent-worker startup fails fast if models directory does not exist."""
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", str(tmp_path))
    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://localhost:6379/0")

    from phaze.config import AgentSettings
    import phaze.tasks.agent_worker as aw

    missing = tmp_path / "nonexistent"
    fake_cfg = AgentSettings()
    fake_cfg.models_path = str(missing)  # type: ignore[misc]
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)

    with pytest.raises(RuntimeError, match="Models directory not found"):
        await aw.startup({})


@pytest.mark.asyncio
async def test_agent_startup_raises_if_no_pb_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent-worker startup fails fast if models directory has no .pb files."""
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", str(tmp_path))
    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://localhost:6379/0")

    from phaze.config import AgentSettings
    import phaze.tasks.agent_worker as aw

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "readme.txt").write_text("empty")

    fake_cfg = AgentSettings()
    fake_cfg.models_path = str(models_dir)  # type: ignore[misc]
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)

    with pytest.raises(RuntimeError, match=r"No \.pb model files"):
        await aw.startup({})


# ---------------------------------------------------------------------------
# Gap 2: Docker Compose controller service uses the correct SAQ command
# (Phase 26 D-04 -- worker.py deleted; the application-server worker now runs
# phaze.tasks.controller.settings under PHAZE_ROLE=control.)
# ---------------------------------------------------------------------------


def test_docker_compose_worker_command_is_controller_settings() -> None:
    """docker-compose.yml worker service command is 'uv run saq phaze.tasks.controller.settings'."""
    compose_file = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose_file.exists(), "docker-compose.yml not found at project root"
    content = compose_file.read_text()
    assert "uv run saq phaze.tasks.controller.settings" in content, (
        "Worker service must use 'uv run saq phaze.tasks.controller.settings' (Phase 26 D-04)"
    )
    assert "phaze.tasks.worker.settings" not in content, "Legacy phaze.tasks.worker.settings must be removed (Phase 26 D-04)"


# ---------------------------------------------------------------------------
# Phase 27 UAT gap-13: Docker Compose must include an agent-side SAQ worker
# that consumes the per-agent queue. Without it, scan_directory and
# extract_file_metadata jobs that the API enqueues sit in Redis forever
# (the `worker` service above only consumes the `controller` queue), so
# user-initiated scans never reach COMPLETED.
# ---------------------------------------------------------------------------


def test_docker_compose_has_agent_worker_consuming_agent_queue() -> None:
    """docker-compose.yml has a service running 'saq phaze.tasks.agent_worker.settings' as PHAZE_ROLE=agent."""
    import yaml

    compose_file = Path(__file__).parent.parent / "docker-compose.yml"
    assert compose_file.exists(), "docker-compose.yml not found at project root"

    compose = yaml.safe_load(compose_file.read_text())
    services = compose.get("services", {})

    def env_has(svc_env: object, key: str, value: str) -> bool:
        # Compose env may be a list ("KEY=VAL") or a dict.
        if isinstance(svc_env, list):
            return f"{key}={value}" in svc_env
        if isinstance(svc_env, dict):
            return svc_env.get(key) == value
        return False

    consumers = [
        name
        for name, spec in services.items()
        if "saq phaze.tasks.agent_worker.settings" in str(spec.get("command", "")) and env_has(spec.get("environment"), "PHAZE_ROLE", "agent")
    ]

    assert consumers, (
        "docker-compose.yml must include at least one service that runs "
        "'uv run saq phaze.tasks.agent_worker.settings' with PHAZE_ROLE=agent. "
        "Without it, scan_directory / extract_file_metadata jobs the API "
        "enqueues onto 'phaze-agent-{agent_id}' have no consumer (Phase 27 UAT gap-13)."
    )
