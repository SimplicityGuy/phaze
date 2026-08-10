"""phaze-uvmcr.4: shell-hosted Compute/Agents pane acceptance coverage.

Covers the acceptance rules that don't already fall out of ``test_shell_routes.py``'s generic
``_UTILITY_PANE_STAGES`` parametrization (bare fragment / history-restore / focus-heading / rail
wiring) or ``tests/agents/routers/test_admin_agents.py``'s router-level content coverage (sort,
dedupe, kind badges, the BLOCKER-2 listener relocation):

- The GET /admin/agents -> /s/agents redirect preserves ?agent=/?clane=/?sort=/?order= TOGETHER,
  not just individually (acceptance rule 3).
- H3: none of the pane's three 5s/own-tick pollers (the table self-poll, an agent's activity
  own-tick, a lane's detail own-tick) live outside #stage-workspace -- so an innerHTML rail swap
  of that element is naturally self-cleaning for all three, with no bespoke teardown needed
  (acceptance rule 8).
- Deep links resolve through the real app (``client`` fixture, not the router-only smoke apps
  test_admin_agents.py uses) -- /s/agents?agent=<id> and /s/agents?clane=<id> both open their
  target, and an unknown id highlights nothing and never errors (acceptance rule 7).

Uses the shared full-application ``client``/``session``/``backends_toml_env`` fixtures (the
production app, main.create_app()), since these assertions are about the REAL end-to-end wiring
between admin_agents.router's fragment endpoints and shell.router's pane host, not an isolated
router-only smoke app.
"""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest

from phaze.models.agent import Agent


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_TWO_CLUSTER_REGISTRY = """
[[backends]]
kind = "compute"
id = "pane-vox"
rank = 10
cap = 2
agent_ref = "pane-vox-node"
scratch_dir = "/scratch/pane-vox"
push_host = "pane-vox.push"
"""


@pytest.mark.asyncio
async def test_redirect_preserves_the_combined_query_string(client: AsyncClient) -> None:
    """Acceptance rule 3: ?agent=/?clane=/?sort=/?order= all round-trip TOGETHER, not just alone.

    Pinned as one combined query string rather than four separate single-param tests -- a redirect
    built by re-serializing individually-typed params (instead of passing the raw query string
    through) could drop or reorder one silently while every single-param test still passed.
    """
    response = await client.get(
        "/admin/agents",
        params={"agent": "some-agent", "clane": "some-lane", "sort": "name", "order": "desc"},
        follow_redirects=False,
    )
    assert response.status_code == 301
    location = response.headers["location"]
    assert location.startswith("/s/agents?")
    query = parse_qs(urlparse(location).query)
    assert query == {
        "agent": ["some-agent"],
        "clane": ["some-lane"],
        "sort": ["name"],
        "order": ["desc"],
    }


@pytest.mark.asyncio
async def test_redirect_round_trip_lands_on_a_working_full_shell(client: AsyncClient) -> None:
    """Following the combined-query redirect actually resolves -- not just a well-formed Location header."""
    response = await client.get(
        "/admin/agents",
        params={"agent": "no-such-agent", "clane": "no-such-lane", "sort": "kind", "order": "asc"},
        follow_redirects=True,
    )
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="stage-workspace"' in body
    assert 'id="agents-table-section"' in body
    # Unresolvable ids highlight nothing rather than erroring (acceptance rule 7's negative half).
    assert "agent-detail-row-" not in body
    assert "compute-lane-detail-row-" not in body


# ---------------------------------------------------------------------------
# H3 -- no orphaned pollers: every 5s/own-tick poll element the agents pane can render lives
# INSIDE #stage-workspace, so htmx's own "cancel a trigger when its element leaves the DOM" rule
# (fired by the rail's innerHTML swap of that element) is what stops all three -- no bespoke
# teardown code is needed, and none exists.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_self_poll_lives_inside_stage_workspace(client: AsyncClient) -> None:
    """The #agents-table-section 5s self-poll is nested inside #stage-workspace, not shell chrome."""
    response = await client.get("/s/agents")
    assert response.status_code == 200, response.text
    body = response.text
    workspace_start = body.index('id="stage-workspace"')
    section_start = body.index('id="agents-table-section"')
    workspace_end = body.index('<div id="pipeline-stats"')  # the next chrome-level element after main
    assert workspace_start < section_start < workspace_end, (
        "the table's 5s self-poll must be nested inside #stage-workspace -- a rail swap's innerHTML "
        "replace is what cancels it (H3); hoisted into shell chrome, it would survive every swap "
        "and orphan itself the first time the operator navigates away"
    )


@pytest.mark.asyncio
async def test_agent_activity_own_tick_lives_inside_stage_workspace(client: AsyncClient, session: AsyncSession) -> None:
    """A deep-linked agent's expanded-row activity own-tick is nested inside #stage-workspace too."""
    session.add(Agent(id="pane-poll-agent", name="PanePollAgent", scan_roots=[], kind="fileserver", last_seen_at=datetime.now(UTC)))
    await session.commit()

    response = await client.get("/s/agents", params={"agent": "pane-poll-agent"})
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="agent-activity-pane-poll-agent"' in body, "fixture setup: the expanded row must actually be open"
    workspace_start = body.index('id="stage-workspace"')
    activity_start = body.index('id="agent-activity-pane-poll-agent"')
    workspace_end = body.index('<div id="pipeline-stats"')
    assert workspace_start < activity_start < workspace_end


@pytest.mark.asyncio
async def test_compute_lane_detail_own_tick_lives_inside_stage_workspace(
    client: AsyncClient,
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """A deep-linked lane's expanded-row detail own-tick is nested inside #stage-workspace too."""
    backends_toml_env(_TWO_CLUSTER_REGISTRY)

    response = await client.get("/s/agents", params={"clane": "pane-vox"})
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="compute-lane-detail-row-pane-vox"' in body, "fixture setup: the expanded row must actually be open"
    workspace_start = body.index('id="stage-workspace"')
    detail_start = body.index('id="compute-lane-detail-row-pane-vox"')
    workspace_end = body.index('<div id="pipeline-stats"')
    assert workspace_start < detail_start < workspace_end


# ---------------------------------------------------------------------------
# Deep links (acceptance rule 7), through the real production app.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_deep_link_opens_its_row_via_the_shell(client: AsyncClient, session: AsyncSession) -> None:
    """/s/agents?agent=<id> opens that agent's expanded row through the shell-hosted pane."""
    session.add(Agent(id="pane-deep-link-agent", name="PaneDeepLinkAgent", scan_roots=[], kind="fileserver", last_seen_at=datetime.now(UTC)))
    await session.commit()

    response = await client.get("/s/agents", params={"agent": "pane-deep-link-agent"})
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="agent-detail-row-pane-deep-link-agent"' in body
    assert 'aria-current="true"' in body


@pytest.mark.asyncio
async def test_clane_deep_link_opens_its_row_via_the_shell(
    client: AsyncClient,
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """/s/agents?clane=<id> opens that lane's expanded row through the shell-hosted pane."""
    backends_toml_env(_TWO_CLUSTER_REGISTRY)

    response = await client.get("/s/agents", params={"clane": "pane-vox"})
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="compute-lane-detail-row-pane-vox"' in body
    assert 'aria-current="true"' in body


@pytest.mark.asyncio
async def test_unknown_agent_deep_link_highlights_nothing_and_never_errors(client: AsyncClient) -> None:
    """An unresolvable ?agent= on /s/agents highlights nothing and renders the pane at 200."""
    response = await client.get("/s/agents", params={"agent": "no-such-agent-at-all"})
    assert response.status_code == 200, response.text
    assert "agent-detail-row-" not in response.text
    assert 'id="agents-table-section"' in response.text


@pytest.mark.asyncio
async def test_unknown_clane_deep_link_highlights_nothing_and_never_errors(client: AsyncClient) -> None:
    """An unresolvable ?clane= on /s/agents highlights nothing and renders the pane at 200."""
    response = await client.get("/s/agents", params={"clane": "no-such-lane-at-all"})
    assert response.status_code == 200, response.text
    assert "compute-lane-detail-row-" not in response.text
    assert 'id="agents-table-section"' in response.text


@pytest.mark.asyncio
async def test_rail_swap_into_agents_replaces_only_stage_workspace(client: AsyncClient) -> None:
    """Acceptance rule 2: a rail-swap response for /s/agents is scoped to #stage-workspace content
    only -- it carries no rail/header markup to re-render, so a real htmx innerHTML swap targeting
    #stage-workspace cannot touch the rail alongside it.
    """
    response = await client.get("/s/agents", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body
    assert 'id="stage-workspace"' not in body, "the fragment must be the WORKSPACE CONTENT, not a copy of its own host div"
    assert "data-rail-stage=" not in body, "a rail-swap fragment must never re-emit rail markup"
    assert 'id="agents-table-section"' in body


@pytest.mark.asyncio
async def test_agents_rail_node_gets_aria_current_after_navigating_there(client: AsyncClient) -> None:
    """Acceptance rule 2: the agents rail node carries aria-current="page" when it is the active stage."""
    response = await client.get("/s/agents")
    assert response.status_code == 200
    assert re.search(r'data-rail-stage="agents"[^>]*aria-current="page"', response.text), (
        'the agents rail node must carry aria-current="page" on /s/agents'
    )
