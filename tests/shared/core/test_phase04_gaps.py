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
    """FastAPI lifespan creates the named ``controller`` SAQ queue on app.state during startup.

    Phase 30: the lifespan wires ``app.state.controller_queue`` (name="controller",
    consumed by the application-server phaze-worker) and no longer constructs the
    unnamed default queue (``app.state.queue``), which had no consumer.
    """
    from fastapi import FastAPI

    mock_queue = MagicMock()
    mock_queue.disconnect = AsyncMock()
    # Phase 36: the lifespan opens the PostgresQueue broker pool (await connect()).
    mock_queue.connect = AsyncMock()
    # Phase 36 (WR-01): shutdown closes the factory-attached cache_redis (await aclose()).
    mock_queue.cache_redis = AsyncMock()

    with (
        # Phase 36: the lifespan builds the controller queue via build_pipeline_queue
        # (PostgresQueue factory), no longer Queue.from_url.
        patch("phaze.main.build_pipeline_queue") as mock_build,
        patch("phaze.main.engine") as mock_engine,
        # Phase 27 UAT Gap 2 / Gap 3: lifespan now also invokes run_migrations
        # and ensure_dev_agent. Patch them out so this test stays unit-level.
        patch("phaze.main.run_migrations", new=AsyncMock()),
        patch("phaze.main.ensure_dev_agent", new=AsyncMock(return_value=None)),
        patch("phaze.main.async_session") as mock_async_session,
        # Phase 33: the lifespan now mounts /saq (gated by enable_saq_ui, default True),
        # which reads non-revoked agents via ``async_session``. This test is scoped to the
        # controller-queue lifecycle, so disable the flag to skip the unrelated agent read.
        patch("phaze.main.settings.enable_saq_ui", False),
    ):
        mock_build.return_value = mock_queue
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
            # build_pipeline_queue must have been called exactly once during startup,
            # with the queue name "controller" as its first positional arg (Phase 36).
            mock_build.assert_called_once()
            assert mock_build.call_args.args[0] == "controller"
            # The named controller queue is stored on app.state; the unnamed default is gone.
            assert app.state.controller_queue is mock_queue
            assert not hasattr(app.state, "queue")


@pytest.mark.asyncio
async def test_lifespan_disconnects_queue_on_shutdown() -> None:
    """FastAPI lifespan disconnects the SAQ queue when the application shuts down."""
    from fastapi import FastAPI

    mock_queue = MagicMock()
    mock_queue.disconnect = AsyncMock()
    # Phase 36: the lifespan opens the PostgresQueue broker pool (await connect()).
    mock_queue.connect = AsyncMock()
    # Phase 36 (WR-01): shutdown closes the factory-attached cache_redis (await aclose()).
    mock_queue.cache_redis = AsyncMock()

    with (
        # Phase 36: controller queue built via build_pipeline_queue (PostgresQueue factory).
        patch("phaze.main.build_pipeline_queue") as mock_build,
        patch("phaze.main.engine") as mock_engine,
        # Phase 27 UAT Gap 2 / Gap 3: see test_lifespan_creates_queue_on_startup above.
        patch("phaze.main.run_migrations", new=AsyncMock()),
        patch("phaze.main.ensure_dev_agent", new=AsyncMock(return_value=None)),
        patch("phaze.main.async_session") as mock_async_session,
        # Phase 33: skip the /saq agent read (see test_lifespan_creates_queue_on_startup).
        patch("phaze.main.settings.enable_saq_ui", False),
    ):
        mock_build.return_value = mock_queue
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
async def test_agent_startup_invokes_ensure_models_present_after_whoami(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 29 D-21: agent_worker.startup delegates the models check to
    ensure_models_present and invokes it AFTER /whoami succeeds (RESEARCH
    <specifics> line 906 -- auth fails fast before spending 5min on the 150MB
    download). The old fail-fast RuntimeError("Models directory not found")
    behaviour is REPLACED, not duplicated.
    """
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test-id")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", str(tmp_path))
    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://localhost:6379/0")

    from phaze.config import AgentSettings
    import phaze.tasks.agent_worker as aw

    models_dir = tmp_path / "models"
    fake_cfg = AgentSettings()
    fake_cfg.models_path = str(models_dir)  # type: ignore[misc]
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)

    # Order tracking: whoami must run BEFORE ensure_models_present (D-21 specifics).
    call_order: list[str] = []
    fake_identity = MagicMock(agent_id="test-id")
    fake_client = AsyncMock()

    async def fake_whoami() -> object:
        call_order.append("whoami")
        return fake_identity

    fake_client.whoami = fake_whoami
    fake_client.close = AsyncMock()
    monkeypatch.setattr(aw, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(aw, "AudfprintAdapter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(aw, "PanakoAdapter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(aw, "FingerprintOrchestrator", lambda **_kw: MagicMock(engines=[]))

    def fake_ensure(models_path: Path) -> None:
        call_order.append("ensure_models_present")
        assert models_path == models_dir, "ensure_models_present must receive cfg.models_path"

    monkeypatch.setattr(aw, "ensure_models_present", fake_ensure)

    await aw.startup({})

    assert call_order == ["whoami", "ensure_models_present"], f"expected whoami then ensure_models_present, got: {call_order}"


@pytest.mark.asyncio
async def test_agent_startup_propagates_ensure_models_present_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A RuntimeError from ensure_models_present propagates out of startup so
    the container exits non-zero and restart: unless-stopped retries
    (T-29-05-02 / Phase 29 D-21 failure mode).
    """
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test-id")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", str(tmp_path))
    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://localhost:6379/0")

    from phaze.config import AgentSettings
    import phaze.tasks.agent_worker as aw

    fake_cfg = AgentSettings()
    fake_cfg.models_path = str(tmp_path / "models")  # type: ignore[misc]
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)

    fake_identity = MagicMock(agent_id="test-id")
    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(return_value=fake_identity)
    fake_client.close = AsyncMock()
    monkeypatch.setattr(aw, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(aw, "AudfprintAdapter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(aw, "PanakoAdapter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(aw, "FingerprintOrchestrator", lambda **_kw: MagicMock(engines=[]))

    def boom(_models_path: Path) -> None:
        msg = "Model download failed: simulated network failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(aw, "ensure_models_present", boom)

    with pytest.raises(RuntimeError, match="Model download failed"):
        await aw.startup({})


# ---------------------------------------------------------------------------
# Gap 2: Docker Compose controller service uses the correct SAQ command
# (Phase 26 D-04 -- worker.py deleted; the application-server worker now runs
# phaze.tasks.controller.settings under PHAZE_ROLE=control.)
# ---------------------------------------------------------------------------


def test_docker_compose_worker_command_is_controller_settings() -> None:
    """docker-compose.yml worker service command is 'uv run saq phaze.tasks.controller.settings'."""
    compose_file = Path(__file__).parent.parent.parent.parent / "docker-compose.yml"
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
    """A compose file declares a service running 'saq phaze.tasks.agent_worker.settings' as PHAZE_ROLE=agent.

    Phase 29 D-15/D-17 split the compose surface in two:
      - docker-compose.yml          — application-server-only services (api, worker=control, postgres, redis).
      - docker-compose.agent.yml    — file-server-only services (worker=agent, watcher, audfprint, panako).
    The agent-worker now lives in docker-compose.agent.yml; this test scans
    BOTH files so the Phase 27 UAT gap-13 invariant (an agent-side SAQ
    consumer exists somewhere in the deployment surface) stays codified.
    """
    import yaml

    root_dir = Path(__file__).parent.parent.parent.parent
    compose_files = [
        root_dir / "docker-compose.yml",
        root_dir / "docker-compose.agent.yml",
    ]

    def env_has(svc_env: object, key: str, value: str) -> bool:
        # Compose env may be a list ("KEY=VAL") or a dict.
        if isinstance(svc_env, list):
            return f"{key}={value}" in svc_env
        if isinstance(svc_env, dict):
            return svc_env.get(key) == value
        return False

    consumers: list[str] = []
    for compose_file in compose_files:
        if not compose_file.exists():
            continue
        compose = yaml.safe_load(compose_file.read_text())
        services = compose.get("services", {}) or {}
        for name, spec in services.items():
            command = str(spec.get("command", ""))
            if "saq phaze.tasks.agent_worker.settings" in command and env_has(spec.get("environment"), "PHAZE_ROLE", "agent"):
                consumers.append(f"{compose_file.name}::{name}")

    assert consumers, (
        "No compose file declares a service running "
        "'uv run saq phaze.tasks.agent_worker.settings' with PHAZE_ROLE=agent. "
        "Phase 29 moved the agent-worker out of docker-compose.yml and into "
        "docker-compose.agent.yml (D-15 / D-17). Without an agent-side SAQ "
        "consumer somewhere in the deployment surface, scan_directory / "
        "extract_file_metadata jobs the API enqueues onto "
        "'phaze-agent-{agent_id}' have no consumer (Phase 27 UAT gap-13)."
    )
