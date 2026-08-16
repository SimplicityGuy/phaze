"""Agent Details control and expanded-row poll survival.

Locks the agent surface's expanded-row contract (replacing the shared _detail_pane.html side panel):

* the `agents_table.html` keeps native row semantics and exposes a stable, native Details button with
  the HTMX wiring (`hx-get` re-requesting the WHOLE table under the current sort, `hx-vals` naming this
  row's own id, `hx-target="#agents-table-section"`, `hx-swap="outerHTML"`, `hx-push-url` `?agent=`) --
  a click re-renders the table so the row's own expanded-row slot (which does not exist in the DOM
  until `selected_agent` resolves to it) gets created;
* the `#agents-table-section` self-poll re-emits `aria-expanded="true"` on the Details button whose
  row matches `?agent=` (D-02), so expanded state survives every 5s `outerHTML` swap; an
  unknown/absent `?agent=` expands nothing and NEVER 500s.

Uses the self-contained smoke-app fixture from test_admin_agents.py (bare FastAPI app mounting only
admin_agents.router, get_session overridden to the shared test session).
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


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


AGENT_ID = "alive-agent"


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a smoke FastAPI app mounting only admin_agents.router (mirrors test_admin_agents)."""
    app = FastAPI(title="agent-drill-smoke", version="test")
    app.include_router(admin_agents.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest_asyncio.fixture
async def smoke(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Smoke client seeding one live agent with the known kebab-case id AGENT_ID."""
    session.add(
        Agent(id=AGENT_ID, name="AliveBox", scan_roots=["/data/music"], last_seen_at=datetime.now(UTC), kind="compute"),
    )
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_agent_row_has_an_explicit_details_control(smoke: AsyncClient) -> None:
    """The row stays semantic table content and a native button owns expansion."""
    response = await smoke.get("/admin/agents/_table")
    assert response.status_code == 200, response.text
    body = response.text

    assert f'id="agent-trigger-{AGENT_ID}"' in body
    assert 'role="button"' not in body
    assert 'tabindex="0"' not in body
    assert f'id="agent-trigger-{AGENT_ID}-details"' in body
    assert ">\n                            Details\n                        </button>" in body
    assert 'aria-expanded="false"' in body
    # phaze-2u8v.6: the click re-fetches the WHOLE table under the CURRENT sort/order (a re-read, like
    # a poll), naming its own id via a plain (non-`js:`) hx-vals override, and swaps #agents-table-section
    # -- not the old shared #detail-pane -- because the expanded row's slot does not exist until this
    # agent is the one selected_agent resolves to.
    assert 'hx-get="/admin/agents/_table?sort=last_seen&amp;order=desc"' in body
    assert f'hx-vals=\'{{"agent": "{AGENT_ID}"}}\'' in body
    assert 'hx-target="#agents-table-section"' in body
    assert 'hx-swap="outerHTML"' in body
    assert f"/s/agents?agent={AGENT_ID}" in body  # canonical hx-push-url carries the ?agent= selection
    assert "onkeydown" not in body
    # The old side-panel target must be gone from this surface entirely.
    assert '"#detail-pane"' not in body


@pytest.mark.asyncio
async def test_agents_table_reemits_selected_details_state(smoke: AsyncClient) -> None:
    """A poll re-emits expanded state on the stable Details control."""
    response = await smoke.get("/admin/agents/_table", params={"agent": AGENT_ID})
    assert response.status_code == 200, response.text
    body = response.text
    assert 'aria-expanded="true"' in body
    # The expanded row exists now that this agent is selected.
    assert f'id="agent-detail-row-{AGENT_ID}"' in body


@pytest.mark.asyncio
async def test_agents_table_unknown_agent_highlights_nothing(smoke: AsyncClient) -> None:
    """An unknown `?agent=` is a lookup-miss: 200 with NO highlight, never a 500 (T-88-01 known-set).

    phaze-w92dg: the response DOES now carry an empty hx-preserve carrier under the detail-row id —
    that is the never-auto-collapse invariant, not a resolved selection. What must stay absent is
    the ring and the real expanded-row body slot.
    """
    response = await smoke.get("/admin/agents/_table", params={"agent": "__nonexistent__"})
    assert response.status_code == 200, response.text
    assert 'aria-expanded="true"' not in response.text
    assert '<tr id="agent-detail-row-__nonexistent__" hx-preserve></tr>' in response.text
    assert 'id="agent-activity-__nonexistent__"' not in response.text
