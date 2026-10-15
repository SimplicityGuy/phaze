"""Inventory parity across the Agents table, first paint and existing stats poll."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import re
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from bs4 import BeautifulSoup
import pytest
from sqlalchemy import delete, select, text

from phaze.models.agent import Agent
from phaze.services import worker_inventory


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_REGISTRY = """
[[backends]]
kind = "local"
id = "local"
rank = 99
cap = 1

[[backends]]
kind = "compute"
id = "cluster-a"
agent_ref = "callback-a"
rank = 10
cap = 2
scratch_dir = "/scratch/a"
push_host = "host-compute-a"

[[backends]]
kind = "compute"
id = "cluster-b"
agent_ref = "callback-b"
rank = 20
cap = 2
scratch_dir = "/scratch/b"
push_host = "host-compute-b"
"""


def _seed(body: str) -> dict[str, int]:
    match = re.search(r'Object\.assign\(Alpine\.store\("pipeline"\), (\{.*?\})\);', body, re.DOTALL)
    assert match
    return json.loads(match[1])


def _summary(body: str) -> str:
    node = BeautifulSoup(body, "html.parser").select_one("[data-worker-inventory]")
    assert node
    return node.get_text(" ", strip=True)


@pytest.mark.asyncio
async def test_inventory_parity_includes_idle_clusters_and_visible_offline_agents(
    client: AsyncClient, session: AsyncSession, backends_toml_env
) -> None:
    backends_toml_env(_REGISTRY)
    # The shared fixture's never-seen fileserver remains visible. Compute callback
    # identities and revoked rows must not inflate the count, even without a binding.
    now = datetime.now(UTC)
    session.add_all(
        [
            Agent(id="callback", name="Callback", kind="compute", scan_roots=[]),
            Agent(id="revoked", name="Revoked", kind="fileserver", scan_roots=[], revoked_at=now, last_seen_at=now),
        ]
    )
    await session.commit()
    body = (await client.get("/s/agents")).text
    assert _summary(body) == "1 agent · 2 clusters"
    heading = BeautifulSoup(body, "html.parser").select_one("#agents-table-heading + p")
    assert heading and heading.get_text(" ", strip=True) == _summary(body)
    seed = _seed(body)
    assert seed["visibleAgentCount"] == 1
    assert seed["clusterCount"] == 2
    assert seed["computeLanesActive"] == 0  # Both configured clusters are idle.
    assert seed["agentOnline"] == 0  # Never-seen fileserver still belongs to inventory.
    assert seed["visibleAgentCountKnown"] == seed["clusterCountKnown"] == 1
    soup = BeautifulSoup(body, "html.parser")
    for key in ("visibleAgentCount", "visibleAgentCountKnown", "clusterCount", "clusterCountKnown"):
        assert len(soup.select(f"#dag-seed-{key}")) == 1
    assert len(soup.select('[hx-get="/pipeline/stats"]')) == 1

    # Offline compute agents that HAVE heartbeated are real inventory, not shadows.
    session.add(Agent(id="offline", name="Offline", kind="compute", scan_roots=[], last_seen_at=now - timedelta(days=1)))
    await session.commit()
    poll = (await client.get("/pipeline/stats")).text
    assert "$store.pipeline.visibleAgentCount = 2" in poll
    assert "$store.pipeline.clusterCount = 2" in poll
    assert "$store.pipeline.visibleAgentCountKnown = 1" in poll
    table = BeautifulSoup((await client.get("/admin/agents/_table")).text, "html.parser")
    heading = table.select_one("#agents-table-heading + p")
    assert heading and heading.get_text(" ", strip=True) == "2 agents · 2 clusters"
    assert table.select_one("#agent-trigger-offline")
    assert table.select_one("#agent-trigger-callback") is None


@pytest.mark.asyncio
async def test_failed_inventory_read_is_unknown_and_does_not_poison_session(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    execute = session.execute

    async def fail_count(*args: object, **kwargs: object) -> None:
        await execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(session, "execute", fail_count)
    assert await worker_inventory.count_visible_agents(session) is None
    monkeypatch.setattr(session, "execute", execute)
    assert (await session.execute(select(Agent.id))).scalars().all() == ["test-fileserver"]


def test_failed_registry_read_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(worker_inventory, "get_settings", fail)
    assert worker_inventory.configured_backend_counts() is None


@pytest.mark.asyncio
async def test_inventory_failure_flags_survive_seed_and_poll(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from phaze.routers import admin_agents
    from phaze.routers.pipeline import dashboard_stats

    monkeypatch.setattr(dashboard_stats, "count_visible_agents", AsyncMock(return_value=None))
    monkeypatch.setattr(dashboard_stats, "configured_backend_counts", lambda: None)
    monkeypatch.setattr(admin_agents, "configured_backend_counts", lambda: None)
    body = (await client.get("/s/agents")).text
    assert _summary(body) == "Agents unavailable · Clusters unavailable"
    assert "None clusters" not in body
    seed = _seed(body)
    assert seed["seedKnown"] == 1  # Other measured counters remain usable.
    assert seed["visibleAgentCountKnown"] == seed["clusterCountKnown"] == 0
    assert "Clusters unavailable" in (await client.get("/admin/agents/_table")).text
    poll = (await client.get("/pipeline/stats")).text
    assert "$store.pipeline.visibleAgentCountKnown = 0" in poll
    assert "$store.pipeline.clusterCountKnown = 0" in poll


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agents", "clusters", "expected"),
    [
        (0, 0, "0 agents · 0 clusters"),
        (1, 1, "1 agent · 1 cluster"),
        (2, 1, "2 agents · 1 cluster"),
        (1234, 5678, "1,234 agents · 5,678 clusters"),
    ],
)
async def test_summary_formats_counts_on_initial_render(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, agents: int, clusters: int, expected: str
) -> None:
    from phaze.routers.shell import store_seed

    build_seed = store_seed.build_pipeline_store_seed

    async def seed(*args: object, **kwargs: object) -> dict[str, int]:
        values = await build_seed(*args, **kwargs)
        assert values is not None
        return {**values, "visibleAgentCount": agents, "clusterCount": clusters, "visibleAgentCountKnown": 1, "clusterCountKnown": 1}

    monkeypatch.setattr("phaze.routers.shell.build_pipeline_store_seed", seed)
    assert _summary((await client.get("/s/discover")).text) == expected


@pytest.mark.asyncio
async def test_empty_inventory_is_measured_with_one_aggregate(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await session.execute(delete(Agent))
    await session.flush()
    execute = session.execute
    statements: list[str] = []

    async def observe(statement, *args, **kwargs):
        statements.append(str(statement))
        return await execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", observe)
    assert await worker_inventory.count_visible_agents(session) == 0
    assert len(statements) == 1
    assert "count(agents.id)" in statements[0]
