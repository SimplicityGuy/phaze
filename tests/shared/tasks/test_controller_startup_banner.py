"""Startup-banner test for phaze.tasks.controller (Phase 26 W2 / OPS-01)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_controller_startup_logs_role_banner(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPS-01: controller startup must emit a banner line with role + queue name."""
    # Patch heavyweight constructors so the test doesn't open Postgres/HTTP connections.
    # Unused lambda args are prefixed with `_` to satisfy ruff ARG005 (CLAUDE.md ruleset).
    monkeypatch.setattr("phaze.tasks.controller.create_async_engine", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.DiscogsographyClient", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.load_prompt_template", lambda: "stub")
    monkeypatch.setattr("phaze.tasks.controller.ProposalService", lambda *_a, **_kw: MagicMock())

    # Patch get_settings so startup doesn't depend on real env. ControlSettings-like mock.
    fake_cfg = MagicMock()
    fake_cfg.redis_url = "redis://localhost:6379/0"
    fake_cfg.database_url = "postgresql+asyncpg://test"
    fake_cfg.debug = False
    fake_cfg.discogsography_url = "http://test"
    fake_cfg.llm_model = "stub-model"
    fake_cfg.llm_max_rpm = 60
    # PR3: startup() now calls configure_logging(level=cfg.log_level, json_logs=cfg.log_json);
    # pin concrete values so the central pipeline renders deterministic JSON to stdout.
    fake_cfg.log_level = "INFO"
    fake_cfg.log_json = True
    # startup() now bridges the LLM keys into os.environ; pin them to None so the
    # MagicMock doesn't yield a non-str auto-attribute for the env assignment.
    fake_cfg.anthropic_api_key = None
    fake_cfg.openai_api_key = None
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)

    # Import AFTER patching. The module-level Queue.from_url already ran at first import
    # (using real env from conftest.py); subsequent startup() calls use our patched get_settings.
    from phaze.tasks import controller

    # PR3: the banner now renders through the central structlog pipeline to stdout;
    # capture stdout instead of caplog (whose root handler configure_logging clears).
    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    # Assert the banner: must include role and queue identifiers
    text = capsys.readouterr().out
    assert "role=control" in text, f"banner missing role=control: {text!r}"
    assert "queue=controller" in text, f"banner missing queue=controller: {text!r}"
    # Verify the W4 fix landed: ctx["queue"] is stashed
    assert "queue" in ctx, "controller.startup did not stash ctx['queue'] (W4)"


@pytest.mark.asyncio
async def test_controller_startup_exports_llm_api_key_for_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug A regression: startup() must bridge the file-loaded Anthropic key into
    ``ANTHROPIC_API_KEY`` so litellm authenticates. Originally nothing called the
    bridge, so every generate_proposals raised AuthenticationError.
    """
    import os

    from pydantic import SecretStr

    # startup() mutates os.environ directly (litellm reads it there); snapshot/restore
    # so the key cannot leak into ControlSettings() in unrelated tests.
    _saved = os.environ.get("ANTHROPIC_API_KEY")
    os.environ.pop("ANTHROPIC_API_KEY", None)
    monkeypatch.setattr("phaze.tasks.controller.create_async_engine", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.DiscogsographyClient", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.load_prompt_template", lambda: "stub")
    monkeypatch.setattr("phaze.tasks.controller.ProposalService", lambda *_a, **_kw: MagicMock())

    fake_cfg = MagicMock()
    fake_cfg.redis_url = "redis://localhost:6379/0"
    fake_cfg.database_url = "postgresql+asyncpg://test"
    fake_cfg.debug = False
    fake_cfg.discogsography_url = "http://test"
    fake_cfg.llm_model = "stub-model"
    fake_cfg.llm_max_rpm = 60
    fake_cfg.log_level = "INFO"
    fake_cfg.log_json = True
    # The two fields the bridge reads -- concrete values (a MagicMock would break the
    # `is not None` guard and os.environ assignment).
    fake_cfg.anthropic_api_key = SecretStr("sk-ant-startup-test")
    fake_cfg.openai_api_key = None
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    try:
        await controller.startup(ctx)
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-startup-test"
    finally:
        if _saved is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = _saved


@pytest.mark.asyncio
async def test_controller_startup_sources_task_engine_pool_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """quick-260707-ryn: startup() must build task_engine with ALL pool kwargs from cfg.

    Leans phaze's PgBouncer session-mode server-connection footprint (the ~55-cap deadlock):
    reduced pool_size/max_overflow + pre_ping + recycle + a bounded acquire timeout. A capturing
    fake records create_async_engine's kwargs so we assert they equal the config values.
    """
    captured: dict[str, object] = {}

    def _capturing_engine(*_a: object, **kw: object) -> MagicMock:
        captured.update(kw)
        return MagicMock()

    monkeypatch.setattr("phaze.tasks.controller.create_async_engine", _capturing_engine)
    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.DiscogsographyClient", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.load_prompt_template", lambda: "stub")
    monkeypatch.setattr("phaze.tasks.controller.ProposalService", lambda *_a, **_kw: MagicMock())

    fake_cfg = MagicMock()
    fake_cfg.redis_url = "redis://localhost:6379/0"
    fake_cfg.database_url = "postgresql+asyncpg://test"
    fake_cfg.debug = False
    fake_cfg.discogsography_url = "http://test"
    fake_cfg.llm_model = "stub-model"
    fake_cfg.llm_max_rpm = 60
    fake_cfg.log_level = "INFO"
    fake_cfg.log_json = True
    fake_cfg.anthropic_api_key = None
    fake_cfg.openai_api_key = None
    # The five pool knobs the ryn wiring reads -- concrete values (a MagicMock would not
    # equality-compare against the expected ints/bool below).
    fake_cfg.db_pool_size = 5
    fake_cfg.db_max_overflow = 5
    fake_cfg.db_pool_timeout = 10
    fake_cfg.db_pool_recycle = 1800
    fake_cfg.db_pool_pre_ping = True
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    assert captured["pool_size"] == 5
    assert captured["max_overflow"] == 5
    assert captured["pool_timeout"] == 10
    assert captured["pool_recycle"] == 1800
    assert captured["pool_pre_ping"] is True


@pytest.mark.asyncio
async def test_controller_shutdown_disposes_engine_and_closes_discogs_client() -> None:
    """shutdown() must dispose task_engine and close discogs_client when present in ctx."""
    from unittest.mock import AsyncMock

    from phaze.tasks import controller

    engine = MagicMock()
    engine.dispose = AsyncMock()
    discogs_client = MagicMock()
    discogs_client.close = AsyncMock()

    ctx: dict[str, Any] = {"task_engine": engine, "discogs_client": discogs_client}
    await controller.shutdown(ctx)

    engine.dispose.assert_awaited_once()
    discogs_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_controller_shutdown_tolerates_missing_ctx_keys() -> None:
    """shutdown() must no-op when startup never ran (empty ctx)."""
    from phaze.tasks import controller

    await controller.shutdown({})
