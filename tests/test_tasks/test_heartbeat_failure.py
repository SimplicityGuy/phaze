"""Phase 29 D-09 — failure-mode test for the SAQ heartbeat cron handler.

When ``client.heartbeat()`` raises any subclass of ``AgentApiError`` (auth,
4xx, or 5xx after retries), the cron handler must log a WARNING and return
without re-raising. SAQ retries the cron on the next tick; the application
server sees ``last_seen_at`` stop advancing and the admin UI surfaces
"stale" -> operator notices. Mirrors Phase 28 D-16 fire-and-forget posture.

``AgentApiServerError`` has NO custom ``__init__`` (verified at
``src/phaze/services/agent_client.py:86-87``); it accepts ONLY positional
args. Constructing it with ``status_code=`` would ``TypeError`` at test
setup time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from phaze.schemas.agent_identity import AgentIdentity
from phaze.services.agent_client import AgentApiServerError
from phaze.tasks.heartbeat import heartbeat_tick


if TYPE_CHECKING:
    import pytest


def _make_ctx() -> dict[str, Any]:
    """Build a minimally-populated ctx dict (matches test_heartbeat_cron._make_ctx)."""
    client = AsyncMock()
    identity = AgentIdentity(
        agent_id="test-agent",
        name="Test Agent",
        scan_roots=["/data"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    worker = MagicMock()
    queue = AsyncMock()
    queue.info = AsyncMock(
        return_value={
            "queued": 3,
            "active": 0,
            "scheduled": 0,
            "name": "phaze-agent-test-agent",
            "workers": {},
            "jobs": [],
        },
    )
    worker.queue = queue
    return {
        "api_client": client,
        "agent_identity": identity,
        "worker": worker,
        "job": MagicMock(),
    }


async def test_heartbeat_agentapierror_warning(caplog: pytest.LogCaptureFixture) -> None:
    """D-09: AgentApiServerError -> WARNING + swallow; no exception escapes."""
    ctx = _make_ctx()
    # AgentApiServerError has no custom __init__ -- POSITIONAL ARGS ONLY.
    # Verified at src/phaze/services/agent_client.py:86-87. Do NOT pass kwargs.
    ctx["api_client"].heartbeat = AsyncMock(side_effect=AgentApiServerError("server error"))

    with caplog.at_level("WARNING", logger="phaze.tasks.heartbeat"):
        # Must not raise.
        await heartbeat_tick(ctx)

    assert "heartbeat failed" in caplog.text
    assert any(r.levelname == "WARNING" for r in caplog.records)
