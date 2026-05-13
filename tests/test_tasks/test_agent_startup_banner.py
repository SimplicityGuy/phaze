"""Startup-banner test for phaze.tasks.agent_worker (Phase 26 W2 / D-13 / OPS-01)."""

from __future__ import annotations

import logging
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_agent_worker_startup_logs_role_banner_with_token_preview(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPS-01 + D-13: agent startup must emit a banner with role + agent_id + token preview.

    D-13 invariant: token preview is FIRST 12 CHARS + "..." -- the secret portion
    (everything after the 12-char `phaze_agent_` prefix) MUST NOT appear.
    """
    # Patch the env reads via monkeypatch
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_SECRET-BYTES-ABCDEF1234567890")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test-id")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/var/empty")  # value is never read; models-check is monkeypatched
    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://localhost:6379/0")

    # Import after env is set so module-level Queue.from_url uses the patched env.
    # (Re-importing the module is cheap because pytest caches the module load
    # across tests; the conftest may have already imported it via collect.)
    # Force get_settings() to return a fresh AgentSettings reflecting the patched env
    # (lru_cache otherwise returns the first-call instance, possibly ControlSettings).
    from phaze.config import AgentSettings
    import phaze.tasks.agent_worker as aw

    fake_cfg = AgentSettings()
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)

    # Patch heavy constructors so the test runs in-memory (no Postgres/Redis/.pb files).
    fake_identity = MagicMock(agent_id="test-id")
    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(return_value=fake_identity)
    fake_client.close = AsyncMock()
    monkeypatch.setattr(aw, "PhazeAgentClient", lambda *_a, **_kw: fake_client)
    monkeypatch.setattr(aw, "create_process_pool", lambda: MagicMock())
    monkeypatch.setattr(aw, "AudfprintAdapter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(aw, "PanakoAdapter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(aw, "FingerprintOrchestrator", lambda **_kw: MagicMock(engines=[]))

    # Patch models-dir check so we don't need real .pb files mounted.
    monkeypatch.setattr(pathlib.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(pathlib.Path, "glob", lambda _self, _pat: [pathlib.Path("/m/x.pb")])

    ctx: dict[str, Any] = {}
    with caplog.at_level(logging.INFO, logger="phaze.tasks.agent_worker"):
        await aw.startup(ctx)

    text = "\n".join(rec.getMessage() for rec in caplog.records)

    # OPS-01 banner assertions: module identifier + role + agent_id
    assert "phaze.tasks.agent_worker" in text, f"banner missing module identifier: {text!r}"
    assert "role=agent" in text, f"banner missing role=agent: {text!r}"
    assert "agent_id=test-id" in text, f"banner missing agent_id: {text!r}"

    # D-13: token preview is EXACTLY 12 chars + "..." -- the prefix `phaze_agent_`
    # is itself 12 chars, so the rendered preview is `phaze_agent_...`.
    assert "phaze_agent_..." in text, f"banner missing 12-char prefix preview: {text!r}"

    # D-13: the secret bytes after the 12-char prefix MUST NOT appear anywhere in the logs.
    assert "SECRET-BYTES-ABCDEF1234567890" not in text, f"D-13 violation: secret portion leaked into logs: {text!r}"


@pytest.mark.asyncio
async def test_agent_worker_startup_raises_on_queue_token_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GAP-5 / Pitfall 1: startup() raises RuntimeError when PHAZE_AGENT_QUEUE does not match
    agent_id returned by /whoami.

    PHAZE_AGENT_QUEUE=phaze-agent-wrong-id  vs  whoami -> agent_id="correct-id"
    Expected: RuntimeError with message matching "queue/token mismatch".
    """
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_SECRET-BYTES-ABCDEF1234567890")
    # Queue declares wrong agent id -- the mismatch under test
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-wrong-id")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/var/empty")

    from phaze.config import AgentSettings
    import phaze.tasks.agent_worker as aw

    fake_cfg = AgentSettings()
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)

    # whoami returns agent_id="correct-id" -- mismatch with PHAZE_AGENT_QUEUE suffix
    fake_identity = type("AgentIdentity", (), {"agent_id": "correct-id"})()
    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(return_value=fake_identity)
    fake_client.close = AsyncMock()
    monkeypatch.setattr(aw, "PhazeAgentClient", lambda *_a, **_kw: fake_client)
    monkeypatch.setattr(aw, "create_process_pool", lambda: MagicMock())
    monkeypatch.setattr(aw, "AudfprintAdapter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(aw, "PanakoAdapter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(aw, "FingerprintOrchestrator", lambda **_kw: MagicMock(engines=[]))
    monkeypatch.setattr(pathlib.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(pathlib.Path, "glob", lambda _self, _pat: [pathlib.Path("/m/x.pb")])

    ctx: dict[str, Any] = {}
    with pytest.raises(RuntimeError, match="queue/token mismatch"):
        await aw.startup(ctx)
