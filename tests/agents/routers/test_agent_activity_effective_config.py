"""phaze-mvq8z.9: the "Effective runtime config" section of GET /admin/agents/{id}/_activity.

Mirrors ``test_agent_activity.py``'s smoke-app pattern. ``Agent.last_status`` is seeded directly
with the wire shape ``HeartbeatRequest.effective_config`` persists (``routers/agent_heartbeat.py``),
so this module proves the RENDERING half in isolation from the POST that produces it (covered by
``tests/shared/tasks/test_heartbeat_runtime_config.py`` and
``tests/agents/routers/test_agent_heartbeat.py``'s byte-identical-old-shape tests).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers import admin_agents
from phaze.routers.admin_agents import _effective_config_view


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


_AGENT_ID = "effective-config-agent"

_EFFECTIVE_CONFIG = {
    "values": {"log_level": "DEBUG", "worker_max_jobs": 9},
    "sources": {"log_level": "env", "worker_max_jobs": "override"},
    "restart_only_keys": ["database_url", "queue_url"],
    "last_reload": {"source": "poll", "outcome": "applied", "error": None, "at": datetime.now(UTC).timestamp()},
}


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    app = FastAPI(title="agent-effective-config-smoke", version="test")
    app.include_router(admin_agents.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest_asyncio.fixture
async def smoke_with_effective_config(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    session.add(
        Agent(
            id=_AGENT_ID,
            name="ConfiguredBox",
            scan_roots=["/data/music"],
            last_seen_at=datetime.now(UTC),
            kind="fileserver",
            last_status={"agent_version": "5.0.0", "worker_pid": 1, "queue_depth": 0, "effective_config": _EFFECTIVE_CONFIG},
        )
    )
    await session.flush()
    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def smoke_without_effective_config(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """An agent whose last beat predates this bead (no `effective_config` key at all)."""
    session.add(
        Agent(
            id=_AGENT_ID,
            name="OldImageBox",
            scan_roots=["/data/music"],
            last_seen_at=datetime.now(UTC),
            kind="fileserver",
            last_status={"agent_version": "4.9.0", "worker_pid": 1, "queue_depth": 0},
        )
    )
    await session.flush()
    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_effective_config_renders_values_sources_and_restart_only_keys(smoke_with_effective_config: AsyncClient) -> None:
    response = await smoke_with_effective_config.get(f"/admin/agents/{_AGENT_ID}/_activity")
    assert response.status_code == 200, response.text
    body = response.text

    assert "Effective runtime config" in body
    assert "log_level" in body
    assert "DEBUG" in body
    assert "worker_max_jobs" in body
    assert "9" in body
    # Source pills.
    assert "override" in body
    assert "env" in body
    # Restart-only keys, listed by name.
    assert "database_url" in body
    assert "queue_url" in body
    assert "Restart-only on this agent" in body
    # Last reload summary.
    assert "Last reload" in body
    assert "poll" in body
    assert "applied" in body


@pytest.mark.asyncio
async def test_effective_config_degrades_to_a_friendly_line_when_absent(smoke_without_effective_config: AsyncClient) -> None:
    """An agent that has never beaten with `effective_config` (an older image) never 500s -- the
    section renders a friendly "not yet reported" line instead (D-00b degrade-safe posture).
    """
    response = await smoke_without_effective_config.get(f"/admin/agents/{_AGENT_ID}/_activity")
    assert response.status_code == 200, response.text
    body = response.text

    assert "Effective runtime config" in body
    assert "Not yet reported" in body


# Unit-level coverage of every degrade branch in `_effective_config_view` -- the HTTP-level fixtures
# above exercise only the two end-to-end shapes (fully present / entirely absent); these pin the
# reader's behavior on every malformed-in-between shape directly, without a DB round trip.


def test_effective_config_view_none_for_no_last_status() -> None:
    assert _effective_config_view(None) is None
    assert _effective_config_view({}) is None


def test_effective_config_view_none_for_non_dict_effective_config() -> None:
    assert _effective_config_view({"effective_config": "not-a-dict"}) is None


def test_effective_config_view_none_for_missing_values_or_sources() -> None:
    assert _effective_config_view({"effective_config": {"sources": {}}}) is None
    assert _effective_config_view({"effective_config": {"values": {}}}) is None
    assert _effective_config_view({"effective_config": {"values": "nope", "sources": {}}}) is None


def test_effective_config_view_defaults_source_to_default_when_unlisted() -> None:
    view = _effective_config_view({"effective_config": {"values": {"log_level": "INFO"}, "sources": {}}})
    assert view is not None
    assert view["rows"] == [{"key": "log_level", "value": "INFO", "source": "default"}]
    assert view["restart_only_keys"] == []
    assert view["last_reload"] is None


def test_effective_config_view_ignores_a_non_dict_last_reload() -> None:
    view = _effective_config_view({"effective_config": {"values": {}, "sources": {}, "last_reload": "not-a-dict"}})
    assert view is not None
    assert view["last_reload"] is None


def test_effective_config_view_treats_a_non_numeric_at_as_absent() -> None:
    view = _effective_config_view(
        {"effective_config": {"values": {}, "sources": {}, "last_reload": {"source": "poll", "outcome": "applied", "error": None, "at": "n/a"}}}
    )
    assert view is not None
    assert view["last_reload"] == {"source": "poll", "outcome": "applied", "error": None, "at": None}


def test_effective_config_view_ignores_a_non_list_restart_only_keys() -> None:
    view = _effective_config_view({"effective_config": {"values": {}, "sources": {}, "restart_only_keys": "not-a-list"}})
    assert view is not None
    assert view["restart_only_keys"] == []
