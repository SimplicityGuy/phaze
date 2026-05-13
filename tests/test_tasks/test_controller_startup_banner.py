"""Startup-banner test for phaze.tasks.controller (Phase 26 W2 / OPS-01)."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_controller_startup_logs_role_banner(
    caplog: pytest.LogCaptureFixture,
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
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)

    # Import AFTER patching. The module-level Queue.from_url already ran at first import
    # (using real env from conftest.py); subsequent startup() calls use our patched get_settings.
    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    with caplog.at_level(logging.INFO, logger="phaze.tasks.controller"):
        await controller.startup(ctx)

    # Assert the banner: must include role and queue identifiers
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "role=control" in text, f"banner missing role=control: {text!r}"
    assert "queue=controller" in text, f"banner missing queue=controller: {text!r}"
    # Verify the W4 fix landed: ctx["queue"] is stashed
    assert "queue" in ctx, "controller.startup did not stash ctx['queue'] (W4)"


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
