"""Phase 88 (88-01, DRILL-03 / D-02 / D-09) + phaze-2u8v.6: agent-row drill-in trigger + poll-survival.

Locks the agent surface's expanded-row contract (replacing the shared _detail_pane.html side panel):

* the `agents_table.html` `<tr>` is a keyboard-accessible `role="button"` drill-in trigger with a
  STABLE `id="agent-trigger-{id}"`, `tabindex="0"`, an `aria-label`, the `onkeydown` Space handler, and
  the HTMX wiring (`hx-get` re-requesting the WHOLE table under the current sort, `hx-vals` naming this
  row's own id, `hx-target="#agents-table-section"`, `hx-swap="outerHTML"`, `hx-push-url` `?agent=`) --
  a click re-renders the table so the row's own expanded-row slot (which does not exist in the DOM
  until `selected_agent` resolves to it) gets created;
* the `#agents-table-section` self-poll re-emits the selected-highlight (`aria-current="true"` + the
  `ring-2 ring-blue-500` ring) on the row whose id matches `?agent=` (D-02), so the ring survives every
  5s `outerHTML` swap; an unknown/absent `?agent=` highlights nothing and NEVER 500s.

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
async def test_agent_row_trigger_markup(smoke: AsyncClient) -> None:
    """The agent row renders as a keyboard-accessible role=button drill-in trigger (DRILL-03 / D-09)."""
    response = await smoke.get("/admin/agents/_table")
    assert response.status_code == 200, response.text
    body = response.text

    assert f'id="agent-trigger-{AGENT_ID}"' in body
    assert 'role="button"' in body
    assert 'tabindex="0"' in body
    assert "aria-label=" in body
    assert 'aria-expanded="false"' in body
    # phaze-2u8v.6: the click re-fetches the WHOLE table under the CURRENT sort/order (a re-read, like
    # a poll), naming its own id via a plain (non-`js:`) hx-vals override, and swaps #agents-table-section
    # -- not the old shared #detail-pane -- because the expanded row's slot does not exist until this
    # agent is the one selected_agent resolves to.
    assert 'hx-get="/admin/agents/_table?sort=last_seen&amp;order=desc"' in body
    assert f'hx-vals=\'{{"agent": "{AGENT_ID}"}}\'' in body
    assert 'hx-target="#agents-table-section"' in body
    assert 'hx-swap="outerHTML"' in body
    assert f"/admin/agents?agent={AGENT_ID}" in body  # hx-push-url carries the ?agent= selection
    # Space activation for a role=button row is not native — the inline onkeydown handler is REQUIRED.
    assert "onkeydown" in body
    # The old side-panel target must be gone from this surface entirely.
    assert '"#detail-pane"' not in body


@pytest.mark.asyncio
async def test_agents_table_reemits_selected_ring(smoke: AsyncClient) -> None:
    """`GET /admin/agents/_table?agent={known}` re-emits aria-current + the ring (D-02 poll survival)."""
    response = await smoke.get("/admin/agents/_table", params={"agent": AGENT_ID})
    assert response.status_code == 200, response.text
    body = response.text
    assert 'aria-current="true"' in body
    assert "ring-2 ring-blue-500" in body
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
    assert 'aria-current="true"' not in response.text
    assert '<tr id="agent-detail-row-__nonexistent__" hx-preserve></tr>' in response.text
    assert 'id="agent-activity-__nonexistent__"' not in response.text
